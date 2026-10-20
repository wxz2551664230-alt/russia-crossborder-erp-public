from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from erp.core.errors import AppError
from erp.core.jsonutil import dumps, loads
from erp.domains.pod.integrations.registry import supplier_client
from erp.domains.pod.models import PodSupplierCatalogSnapshot
from erp.services.jobs import JobContext, job_handler


PROVIDERS = ("hicustom", "s2b")


def empty_catalog() -> dict[str, Any]:
    return {"version": 2, "synced_at": None, "products": [], "categories": [], "previews": {}, "details": {}, "provider_sync": {}}


def current_snapshot(session: Session) -> PodSupplierCatalogSnapshot | None:
    return session.scalar(
        select(PodSupplierCatalogSnapshot).where(PodSupplierCatalogSnapshot.is_current.is_(True), PodSupplierCatalogSnapshot.provider != "amazon_us")
        .order_by(PodSupplierCatalogSnapshot.created_at.desc())
    )


def load_catalog(session: Session) -> tuple[PodSupplierCatalogSnapshot | None, dict[str, Any]]:
    snapshot = current_snapshot(session)
    payload = loads(snapshot.payload_json, {}) if snapshot else {}
    result = empty_catalog()
    if isinstance(payload, dict):
        result.update(payload)
    return snapshot, result


def _decorate_category(raw: dict[str, Any], supplier: str) -> dict[str, Any]:
    item = dict(raw)
    source_id = str(item.get("source_id") or item.get("id") or item.get("category_id") or "")
    children = item.get("children") or item.get("sub_cg") or []
    item.update({
        "source_id": source_id,
        "supplier": supplier,
        "category_key": f"{supplier}:{source_id}",
        "children": [_decorate_category(child, supplier) for child in children if isinstance(child, dict)],
    })
    return item


def catalog_view(session: Session) -> dict[str, Any]:
    _, data = load_catalog(session)
    previews = dict(data.get("previews") or {})
    products: list[dict[str, Any]] = []
    for raw in data.get("products") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        supplier = str(item.get("supplier") or "hicustom")
        source_id = str(item.get("source_id") or item.get("id") or item.get("product_type_id") or item.get("code") or "")
        item.update({
            "supplier": supplier,
            "supplier_name": "S2B 货盘" if supplier == "s2b" else "指纹科技",
            "source_id": source_id,
            "catalog_key": f"{supplier}:{source_id}",
        })
        direct_preview = str(item.get("view_image_src") or item.get("design_product_image") or item.get("preview_url") or "")
        if direct_preview.startswith(("https://", "http://")):
            previews[item["catalog_key"]] = direct_preview
        products.append(item)
    grouped_categories = []
    for supplier, name in (("hicustom", "指纹科技"), ("s2b", "S2B 货盘")):
        children = [
            _decorate_category(item, supplier) for item in data.get("categories") or []
            if isinstance(item, dict) and str(item.get("supplier") or "hicustom") == supplier
        ]
        if children or any(item["supplier"] == supplier for item in products):
            grouped_categories.append({
                "id": f"{supplier}:all", "category_key": f"{supplier}:all",
                "supplier": supplier, "name": name, "children": children,
            })
    return {
        "cached": bool(data.get("synced_at")),
        "synced_at": data.get("synced_at"),
        "product_count": len(products),
        "provider_counts": {provider: sum(item["supplier"] == provider for item in products) for provider in PROVIDERS},
        "products": products,
        "categories": grouped_categories,
        "previews": previews,
        "provider_sync": dict(data.get("provider_sync") or {}),
    }


def _product_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("items", "list", "product_types", "products", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _product_items(value)
            if nested:
                return nested
    return []


def _product_total(payload: dict[str, Any]) -> int | None:
    for key in ("total", "total_count", "total_num", "records"):
        value = payload.get(key)
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    for value in payload.values():
        if isinstance(value, dict):
            nested = _product_total(value)
            if nested is not None:
                return nested
    return None


def _save_catalog(session: Session, data: dict[str, Any], *, commit: bool = True) -> PodSupplierCatalogSnapshot:
    for current in session.scalars(select(PodSupplierCatalogSnapshot).where(PodSupplierCatalogSnapshot.is_current.is_(True), PodSupplierCatalogSnapshot.provider != "amazon_us")).all():
        current.is_current = False
    item = PodSupplierCatalogSnapshot(
        provider="all",
        is_current=True,
        product_count=len(data.get("products") or []),
        category_count=len(data.get("categories") or []),
        fetched_at=str(data.get("synced_at") or ""),
        payload_json=dumps(data),
    )
    session.add(item)
    if commit: session.commit()
    else: session.flush()
    return item


def accept_personal_catalog(session: Session, job, result: dict[str, Any]) -> dict[str, Any]:
    """Merge frozen PC supplier facts without executing supplier code on the server."""
    from erp.core.errors import ConflictError
    payload = loads(job.payload_json, {})
    current, data = load_catalog(session)
    if (current.id if current else None) != payload.get('base_snapshot_id'):
        raise ConflictError('供应商货盘已被其他同步更新，请重新同步')
    providers = result.get('providers')
    if not isinstance(providers, list) or len(providers) != len(PROVIDERS):
        raise ConflictError('供应商货盘结果未覆盖全部来源')
    if {row.get('supplier') for row in providers if isinstance(row, dict)} != set(PROVIDERS):
        raise ConflictError('供应商货盘来源不匹配')
    provider_sync = dict(data.get('provider_sync') or {})
    successes = 0
    errors: list[str] = []
    for row in providers:
        supplier = row['supplier']
        products, categories, error = row.get('products'), row.get('categories'), row.get('error')
        if not isinstance(error, str) or len(error) > 500:
            raise ConflictError('供应商货盘错误结果无效')
        if error:
            if products is not None or categories is not None:
                raise ConflictError('失败来源不能覆盖商品资料')
            provider_sync[supplier] = {**dict(provider_sync.get(supplier) or {}), 'error': error}
            errors.append(f'{supplier.upper()}：{error}')
            continue
        if (not isinstance(products, list) or not isinstance(categories, list)
                or len(products) > 100_000 or len(categories) > 20_000
                or any(not isinstance(item, dict) or item.get('supplier') != supplier
                       or not str(item.get('source_id') or '')
                       or item.get('catalog_key') != f"{supplier}:{item.get('source_id')}"
                       for item in products)
                or any(not isinstance(item, dict) or item.get('supplier') != supplier
                       for item in categories)):
            raise ConflictError('供应商货盘字段与原规则不一致')
        data['products'] = [item for item in data.get('products') or []
                            if isinstance(item, dict) and str(item.get('supplier') or 'hicustom') != supplier] + products
        data['categories'] = [item for item in data.get('categories') or []
                              if isinstance(item, dict) and str(item.get('supplier') or 'hicustom') != supplier] + categories
        provider_sync[supplier] = {'synced_at': datetime.now(timezone.utc).isoformat(),
                                   'product_count': len(products), 'error': ''}
        successes += 1
    if not successes:
        raise ConflictError('全部供应商货盘同步失败，未覆盖原资料')
    data['version'] = 2
    data['synced_at'] = datetime.now(timezone.utc).isoformat()
    data['provider_sync'] = provider_sync
    snapshot = _save_catalog(session, data, commit=False)
    return {'snapshot_id': snapshot.id, 'product_count': snapshot.product_count, 'errors': errors}


def fetch_provider_catalog(client, provider: str,
                           progress: Callable[[float, str], None] | None = None,
                           position: int = 1) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep the original supplier pagination and field normalization on the PC."""
    categories = client.product_categories()
    products: dict[str, dict[str, Any]] = {}
    expected: int | None = None
    for page in range(1, 1001):
        payload = client.product_types(page, 100)
        values = _product_items(payload)
        page_count = int(payload.get("_page_item_count") or len(values))
        if expected is None:
            expected = _product_total(payload)
        for raw in values:
            identity = raw.get("id") or raw.get("product_type_id") or raw.get("code")
            if identity:
                products[str(identity)] = {**raw, "supplier": provider,
                                           "source_id": str(identity),
                                           "catalog_key": f"{provider}:{identity}"}
        if progress:
            provider_fraction = min(0.98, len(products) / expected) if expected else min(0.98, page / 100)
            progress((position - 1) * 50 + provider_fraction * 50,
                     f"{provider.upper()} 已读取 {len(products)}" +
                     (f"/{expected} 个商品" if expected else " 个商品"))
        if page_count == 0 or page_count < 100 or (expected is not None and len(products) >= expected):
            break
    return list(products.values()), [{**item, "supplier": provider} for item in categories]


def sync_catalog(
    session: Session,
    progress: Callable[[float, str], None] | None = None,
    connection_ids: dict[str, str] | None = None,
) -> dict[str, Any]:
    _, data = load_catalog(session)
    provider_sync = dict(data.get("provider_sync") or {})
    successes = 0
    errors: list[str] = []
    for position, provider in enumerate(PROVIDERS, 1):
        if progress:
            progress((position - 1) * 50, f"正在同步 {provider.upper()} 货盘")
        try:
            with supplier_client(session, provider, str((connection_ids or {}).get(provider) or "")) as client:
                products, categories = fetch_provider_catalog(client, provider, progress, position)
            data["products"] = [item for item in data.get("products") or [] if isinstance(item, dict) and str(item.get("supplier") or "hicustom") != provider] + products
            data["categories"] = [item for item in data.get("categories") or [] if isinstance(item, dict) and str(item.get("supplier") or "hicustom") != provider] + categories
            provider_sync[provider] = {"synced_at": datetime.now(timezone.utc).isoformat(), "product_count": len(products), "error": ""}
            successes += 1
        except AppError as error:
            provider_sync[provider] = {**dict(provider_sync.get(provider) or {}), "error": str(error)}
            errors.append(f"{provider.upper()}：{error}")
    if not successes:
        raise AppError("；".join(errors) or "供应商货盘同步失败", code="pod_catalog_sync_failed", status_code=502)
    data["version"] = 2
    data["synced_at"] = datetime.now(timezone.utc).isoformat()
    data["provider_sync"] = provider_sync
    snapshot = _save_catalog(session, data)
    if progress:
        progress(100, "供应商货盘同步完成")
    return {"snapshot_id": snapshot.id, "product_count": snapshot.product_count, "errors": errors}


def parse_catalog_key(value: str) -> tuple[str, int, str]:
    text = str(value or "").strip()
    supplier, source_id = text.split(":", 1) if ":" in text else ("hicustom", text)
    supplier = supplier.casefold()
    if supplier not in PROVIDERS or not source_id.isdigit():
        raise AppError("供应商商品编号无效", code="pod_catalog_key_invalid", status_code=422)
    return supplier, int(source_id), f"{supplier}:{int(source_id)}"


def product_detail(session: Session, product_key: str) -> dict[str, Any]:
    snapshot, data = load_catalog(session)
    supplier, product_id, cache_key = parse_catalog_key(product_key)
    details = data.setdefault("details", {})
    cached = details.get(cache_key) or (details.get(str(product_id)) if supplier == "hicustom" else None)
    if isinstance(cached, dict):
        return cached
    with supplier_client(session, supplier) as client:
        detail = client.product_type(product_id)
    detail.update({"supplier": supplier, "catalog_key": cache_key})
    details[cache_key] = detail
    if supplier == "hicustom":
        details[str(product_id)] = detail
    if snapshot:
        snapshot.payload_json = dumps(data)
        session.commit()
    else:
        data["synced_at"] = datetime.now(timezone.utc).isoformat()
        _save_catalog(session, data)
    return detail


def refresh_product_detail(session: Session, product_key: str) -> dict[str, Any]:
    """Read a fresh supplier detail and replace only this product's cache."""
    snapshot, data = load_catalog(session)
    supplier, product_id, cache_key = parse_catalog_key(product_key)
    with supplier_client(session, supplier) as client:
        detail = client.product_type(product_id)
    detail.update({"supplier": supplier, "catalog_key": cache_key})
    details = data.setdefault("details", {})
    details[cache_key] = detail
    if supplier == "hicustom":
        details[str(product_id)] = detail
    data["synced_at"] = datetime.now(timezone.utc).isoformat()
    if snapshot:
        snapshot.payload_json = dumps(data)
        snapshot.fetched_at = data["synced_at"]
        session.commit()
    else:
        _save_catalog(session, data)
    return detail


def product_previews(session: Session, product_keys: list[str]) -> dict[str, str | None]:
    snapshot, data = load_catalog(session)
    previews = data.setdefault("previews", {})
    requested = list(dict.fromkeys(str(value) for value in product_keys[:24]))
    changed = False
    for key in requested:
        if key in previews:
            continue
        supplier, _, cache_key = parse_catalog_key(key)
        try:
            detail = product_detail(session, cache_key)
            with supplier_client(session, supplier) as client:
                previews[cache_key] = client.preview_url(detail)
        except AppError:
            previews[cache_key] = None
        changed = True
    if changed:
        snapshot, latest = load_catalog(session)
        latest.setdefault("previews", {}).update(previews)
        if snapshot:
            snapshot.payload_json = dumps(latest)
            session.commit()
    return {value: previews.get(value) for value in requested}


@job_handler("pod_supplier_catalog_sync")
def supplier_catalog_sync_job(context: JobContext) -> None:
    context.start_step("sync")
    result = sync_catalog(
        context.session,
        lambda value, message: context.set_progress("sync", value, message),
        dict(context.payload.get("connection_ids") or {}),
    )
    context.complete_step("sync", details=result)
