from __future__ import annotations

import mimetypes
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import Integer, and_, case, cast, exists, func, not_, or_, select
from sqlalchemy.orm import Session

from erp.core.config import get_settings
from erp.core.errors import AppError, ConflictError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.domains.pod.integrations.oss import PodOssService
from erp.domains.pod.models import PodColorImageGroup, PodComposition, PodProductCategory, PodProductVersion
from erp.domains.pod.product_categories import category_expression, category_label
from erp.models import Asset, CatalogProduct, CatalogProductAsset, CatalogVariant, IntegrationConnection, Job, WbPublicationItem
from erp.serializers import catalog_product_dict, catalog_variant_dict, job_dict
from erp.services.catalog import get_catalog_product, product_query
from erp.services.jobs import JobContext, enqueue_job, job_handler


def refresh_supplier_facts(session: Session, product_id: str) -> dict[str, Any]:
    product = get_catalog_product(session, product_id)
    if product.source_type not in {"pod", "pod_amazon"}: raise ConflictError("该产品不属于 POD 产品库")
    original_product_id = product.id
    original_variant_ids = {row.id for row in product.variants}
    version = session.scalar(select(PodProductVersion).where(
        PodProductVersion.catalog_product_id == product.id,
        PodProductVersion.is_current.is_(True),
    ).order_by(PodProductVersion.version_no.desc()))
    recipe = loads(version.recipe_json, {}) if version else {}
    recorded_supplier = str(recipe.get("supplier") or (version.supplier if version else "")).casefold()
    if recorded_supplier not in {"hicustom", "s2b"}:
        recorded_supplier = "s2b" if product.sku.upper().startswith("S2B-") else "hicustom"
    supplier = recorded_supplier
    if not str(product.supplier_product_id or "").isdigit():
        raise AppError("产品缺少有效的供应商商品 ID", code="pod_supplier_product_id_invalid")
    from erp.domains.pod.composition import apply_supplier_facts
    from erp.domains.pod.supplier_catalog import refresh_product_detail
    if product.source_type == "pod_amazon":
        from erp.domains.pod.us_supplier_catalog import composition_detail
        detail = composition_detail(session, f"{supplier}:{product.supplier_product_id}")
    else:
        detail = refresh_product_detail(session, f"{supplier}:{product.supplier_product_id}")
    result = apply_supplier_facts(product, detail)
    session.flush()
    current_variant_ids = set(session.scalars(select(CatalogVariant.id).where(CatalogVariant.product_id == original_product_id)).all())
    if product.id != original_product_id or current_variant_ids != original_variant_ids:
        session.rollback()
        raise ConflictError("供应商资料更新不得新增、删除或替换 SKU，操作已回滚")
    session.commit()
    return {**result, "created_products": 0, "created_variants": 0,
            "preserved_variant_ids": sorted(current_variant_ids),
            "product": library_product_detail(session, product.id)}


def _pod_conditions(search: str = "", supplier: str = "", category: str = "", status: str = "") -> list[Any]:
    conditions: list[Any] = [CatalogProduct.source_type.in_(["pod", "pod_amazon"])]
    if search.strip():
        token = f"%{search.strip()}%"
        conditions.append(or_(CatalogProduct.sku.ilike(token), CatalogProduct.group_code.ilike(token), CatalogProduct.name.ilike(token), CatalogProduct.supplier_product_id.ilike(token)))
    if supplier: conditions.append(CatalogProduct.supplier == supplier)
    if category: conditions.append(category_expression() == category)
    if status: conditions.append(CatalogProduct.status == status)
    return conditions


def library_summary(session: Session) -> dict[str, Any]:
    product_ids = select(CatalogProduct.id).where(CatalogProduct.source_type.in_(["pod", "pod_amazon"]))
    products = int(session.scalar(select(func.count()).select_from(CatalogProduct).where(CatalogProduct.source_type.in_(["pod", "pod_amazon"]))) or 0)
    variants = int(session.scalar(select(func.count()).select_from(CatalogVariant).where(CatalogVariant.product_id.in_(product_ids))) or 0)
    current_links = (
        CatalogProductAsset.product_id.in_(product_ids),
        CatalogProductAsset.is_current.is_(True),
    )
    current_assets = int(session.scalar(select(func.count()).select_from(CatalogProductAsset).where(*current_links)) or 0)
    # The legacy POD library contains tens of thousands of files.  A dashboard
    # request must not stat every path on the Windows filesystem.  local_path is
    # the durable index; actual availability is verified only when the file is
    # opened/restored.
    local_assets = int(session.scalar(
        select(func.count()).select_from(CatalogProductAsset).join(Asset, Asset.id == CatalogProductAsset.asset_id)
        .where(*current_links, Asset.local_path != "")
    ) or 0)
    oss_assets = int(session.scalar(
        select(func.count()).select_from(CatalogProductAsset).join(Asset, Asset.id == CatalogProductAsset.asset_id)
        .where(*current_links, or_(Asset.oss_url != "", Asset.oss_key != ""))
    ) or 0)
    return {
        "products": products, "variants": variants, "current_assets": current_assets,
        "local_assets": local_assets, "oss_assets": oss_assets,
        "versions": int(session.scalar(select(func.count()).select_from(PodProductVersion)) or 0),
        "compositions": int(session.scalar(select(func.count()).select_from(PodComposition)) or 0),
        "by_status": {str(key): int(value) for key, value in session.execute(select(CatalogProduct.status, func.count()).where(CatalogProduct.source_type.in_(["pod", "pod_amazon"])).group_by(CatalogProduct.status)).all()},
        "by_supplier": {str(key): int(value) for key, value in session.execute(select(CatalogProduct.supplier, func.count()).where(CatalogProduct.source_type.in_(["pod", "pod_amazon"])).group_by(CatalogProduct.supplier)).all()},
    }


def list_library_products(session: Session, *, page: int, page_size: int, search: str, supplier: str, category: str, status: str, wb_store_id: str = "", wb_state: str = "", market: str = "CN") -> dict[str, Any]:
    conditions = _pod_conditions(search, supplier, category, status)
    conditions.append(CatalogProduct.source_type == ("pod_amazon" if market == "US" else "pod"))
    if wb_store_id and wb_state:
        terminal_failure=or_(Job.id.is_(None),Job.status.in_(["failed","cancelled"]))
        active_job=or_(Job.id.is_(None),Job.status.not_in(["failed","cancelled"]))
        fixed_active=WbPublicationItem.status.in_([
            "exported_pending_confirmation","submitted",
            "pending_stock","stock_processing","pending_price","price_processing",
            "completed","paused","stock_failed","price_failed","sync_conflict","sync_unknown",
        ])
        live_active=and_(WbPublicationItem.status.in_(["queued","running"]),active_job)
        base=select(WbPublicationItem.id).outerjoin(Job,Job.id==WbPublicationItem.job_id).where(WbPublicationItem.product_id==CatalogProduct.id,WbPublicationItem.store_id==wb_store_id)
        confirmed_card=WbPublicationItem.external_product_id!=''
        blocking=exists(base.where(or_(fixed_active,live_active,confirmed_card)))
        if wb_state=="available":conditions.append(not_(blocking))
        elif wb_state=="listed":conditions.append(exists(base.where(WbPublicationItem.status=="completed")))
        elif wb_state=="processing":conditions.append(exists(base.where(or_(WbPublicationItem.status.in_(["exported_pending_confirmation","submitted","pending_stock","stock_processing","pending_price","price_processing"]),live_active))))
        elif wb_state=="retryable":conditions.append(and_(not_(blocking),exists(base.where(or_(WbPublicationItem.status.in_(["failed","cancelled"]),and_(WbPublicationItem.status.in_(["queued","running"]),terminal_failure))))))
    total = int(session.scalar(select(func.count()).select_from(CatalogProduct).where(*conditions)) or 0)
    sku_number = case(
        (CatalogProduct.sku.ilike("S2B-%"), cast(func.substr(CatalogProduct.sku, 5, 6), Integer)),
        else_=cast(func.substr(CatalogProduct.sku, 4, 6), Integer),
    )
    rows = session.scalars(product_query().where(*conditions).order_by(
        sku_number.desc(), CatalogProduct.sku.desc(), CatalogProduct.id,
    ).offset((page - 1) * page_size).limit(page_size)).all()
    version_map: dict[str, list[PodProductVersion]] = {}
    if rows:
        for version in session.scalars(select(PodProductVersion).where(
            PodProductVersion.catalog_product_id.in_([row.id for row in rows])
        ).order_by(PodProductVersion.version_no.desc())).all():
            version_map.setdefault(version.catalog_product_id, []).append(version)
    items = []
    for row in rows:
        value = catalog_product_dict(row, include_details=False)
        value["product_category"] = category_label(row)
        current = [link for link in row.asset_links if link.is_current]
        thumbnail = next((link.asset.oss_url or f"/api/v1/assets/{link.asset.id}/content" for link in sorted(current, key=lambda item: (item.role not in {"main", "mockup"}, item.position)) if link.asset.oss_url or link.asset.local_path), "")
        versions = version_map.get(row.id, [])
        publication = _publication_readiness(row, versions)
        production = _production_readiness(row, versions, verify_local=False)
        value.update({"thumbnail_url": thumbnail, "variants": [catalog_variant_dict(variant) for variant in row.variants if variant.active], "local_asset_count": sum(bool(link.asset.local_path) for link in current), "oss_asset_count": sum(bool(link.asset.oss_url or link.asset.oss_key) for link in current), "publication_ready": publication["ready_for_publication"], "publication_completeness": publication["completeness_percent"], "production_ready": production["ready_for_production"], "production_completeness": production["completeness_percent"], "library_state": _library_state(row, production, publication)})
        items.append(value)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def _publication_readiness(product: CatalogProduct, versions: list[PodProductVersion]) -> dict[str, Any]:
    active = [variant for variant in product.variants if variant.active]
    targets: list[CatalogVariant | None] = active or [None]

    def effective(variant: CatalogVariant | None, variant_key: str, product_key: str) -> Any:
        value = getattr(variant, variant_key, None) if variant else None
        return value if value is not None and value != "" else getattr(product, product_key)

    checks = [
        ("name", "中文名称", bool(product.name.strip())),
        ("sku", "产品 SKU", bool(product.sku.strip())),
        ("variants", "至少一个启用 SKU", bool(active)),
        ("cost", "每个 SKU 的精确拿货价", all(effective(row, "purchase_price_cny", "purchase_price_cny") is not None for row in targets)),
        ("length", "每个 SKU 的包装长度", all(float(effective(row, "package_length_mm", "package_length_mm") or 0) > 0 for row in targets)),
        ("width", "每个 SKU 的包装宽度", all(float(effective(row, "package_width_mm", "package_width_mm") or 0) > 0 for row in targets)),
        ("height", "每个 SKU 的包装高度", all(float(effective(row, "package_height_mm", "package_height_mm") or 0) > 0 for row in targets)),
        ("weight", "每个 SKU 的包装重量", all(float(effective(row, "weight_grams", "package_weight_g") or 0) > 0 for row in targets)),
        ("images", "至少一张 HTTPS OSS 产品图", any(str(link.asset.oss_url or "").startswith("https://") for link in product.asset_links if link.is_current)),
    ]
    current_version = next((item for item in versions if item.is_current), versions[0] if versions else None)
    warnings: list[str] = []
    if product.source_type == "pod_amazon":
        warnings.append("Amazon 类目、属性、变体主题和 FBM 配送模板在美国站发布页面核验")
    elif not product.description_category_id or not product.type_id:
        warnings.append("OZON 类目和商品类型尚未固定，可在每次发布时读取实时类目后选择")
    if not current_version:
        warnings.append("没有生产版本，后续补货时无法复用生产配方")
    elif not current_version.product_code:
        warnings.append("当前生产版本缺少供应商产品编码")
    if active and any(not row.supplier_sku_id for row in active):
        warnings.append("部分 SKU 缺少供应商 SKU ID，后续生产下单前需要补齐")
    blocking = [label for _, label, passed in checks if not passed]
    return {
        "ready_for_publication": not blocking,
        "completeness_percent": round(sum(1 for _, _, passed in checks if passed) * 100 / len(checks)),
        "checks": [{"key": key, "label": label, "passed": passed} for key, label, passed in checks],
        "blocking": blocking,
        "warnings": warnings,
        "generated_at_publication": (["英文标题与描述", "卖点与搜索词", "Amazon 类目属性与变体关系", "店铺独立价格与 SKU"]
                                     if product.source_type == "pod_amazon" else
                                     ["俄文标题与描述", "卖点与标签", "OZON 实时属性与字典值", "店铺独立价格与 Offer ID"]),
        "counts": {
            "active_variants": len(active),
            "https_oss_images": sum(str(link.asset.oss_url or "").startswith("https://") for link in product.asset_links if link.is_current),
            "current_versions": sum(bool(item.is_current) for item in versions),
        },
    }


def _production_readiness(product: CatalogProduct, versions: list[PodProductVersion], *, verify_local: bool) -> dict[str, Any]:
    active = [variant for variant in product.variants if variant.active]
    current = next((item for item in versions if item.is_current), versions[0] if versions else None)
    recipe = loads(current.recipe_json, {}) if current else {}
    sides = [item for item in recipe.get("sides") or [] if isinstance(item, dict)]
    source_links = [link for link in product.asset_links if link.is_current and link.role in {"artwork", "source"}]
    recoverable_sources = [link for link in source_links if (link.asset.oss_url or link.asset.oss_key) or (
        link.asset.local_path and (not verify_local or Path(link.asset.local_path).is_file())
    )]
    supplier = str(recipe.get("supplier") or (current.supplier if current else "") or product.supplier).casefold()
    supplier_identity = bool(str(product.supplier_product_id or "").strip() or (current and current.product_type_id))
    checks = [
        ("supplier", "供应商身份明确", supplier in {"hicustom", "s2b"}),
        ("supplier_product", "供应商基础商品 ID", supplier_identity),
        ("version", "当前生产版本", current is not None),
        ("recipe", "印刷面与排版配方", bool(sides)),
        ("source", "至少一份可恢复的印花原图", bool(recoverable_sources)),
        ("variants", "至少一个启用的生产 SKU", bool(active)),
        ("supplier_skus", "每个启用 SKU 的供应商规格 ID", bool(active) and all(bool(str(item.supplier_sku_id or "").strip()) for item in active)),
    ]
    blocking = [label for _, label, passed in checks if not passed]
    return {
        "ready_for_production": not blocking,
        "completeness_percent": round(sum(1 for _, _, passed in checks if passed) * 100 / len(checks)),
        "checks": [{"key": key, "label": label, "passed": passed} for key, label, passed in checks],
        "blocking": blocking,
        "can_rebuild_supplier_product": bool(sides and recoverable_sources and supplier_identity),
        "current_product_code": current.product_code if current else "",
        "recoverable_source_count": len(recoverable_sources),
    }


def _library_state(product: CatalogProduct, production: dict[str, Any], publication: dict[str, Any]) -> str:
    if product.status == "archived": return "archived"
    if production["ready_for_production"] and publication["ready_for_publication"]: return "ready"
    if not production["ready_for_production"]: return "production_incomplete"
    return "publication_incomplete"


def library_product_detail(session: Session, product_id: str) -> dict[str, Any]:
    product = get_catalog_product(session, product_id)
    if product.source_type not in {"pod", "pod_amazon"}: raise ConflictError("该产品不属于 POD 产品库")
    value = catalog_product_dict(product)
    value["product_category"] = category_label(product)
    versions = session.scalars(select(PodProductVersion).where(PodProductVersion.catalog_product_id == product.id).order_by(PodProductVersion.version_no.desc())).all()
    version_ids = [version.id for version in versions]
    groups_by_version: dict[str, list[PodColorImageGroup]] = {}
    if version_ids:
        for group in session.scalars(select(PodColorImageGroup).where(PodColorImageGroup.version_id.in_(version_ids)).order_by(PodColorImageGroup.version_id, PodColorImageGroup.color_id, PodColorImageGroup.id)).all():
            groups_by_version.setdefault(group.version_id, []).append(group)
    version_values = []
    for version in versions:
        groups = groups_by_version.get(version.id, [])
        version_values.append({
            "id": version.id, "version_no": version.version_no, "is_current": version.is_current,
            "job_status": version.job_status, "supplier": version.supplier, "product_type_id": version.product_type_id,
            "default_color_id": version.default_color_id, "default_view_id": version.default_view_id,
            "product_code": version.product_code, "product_code_status": version.product_code_status,
            "output_folder": version.legacy_output_folder, "local_folder_exists": bool(version.legacy_output_folder and Path(version.legacy_output_folder).is_dir()),
            "recipe": loads(version.recipe_json, {}), "asset_snapshot": loads(version.assets_json, {}),
            "color_groups": [{"id": group.id, "color_id": group.color_id, "color_name": group.color_name, "rendering_urls": loads(group.rendering_urls_json, []), "saved_files": loads(group.saved_files_json, [])} for group in groups],
            "created_at": version.created_at.isoformat(),
        })
    compositions = session.scalars(select(PodComposition).where(PodComposition.catalog_product_id == product.id).order_by(PodComposition.created_at.desc()).limit(100)).all()
    value.update({
        "publication_readiness": _publication_readiness(product, versions),
        "production_readiness": _production_readiness(product, versions, verify_local=True),
        "versions": version_values,
        "compositions": [{"id": item.id, "job_id": item.job_id, "status": item.status, "stage_text": item.stage_text, "supplier": item.supplier, "product_code": item.product_code, "outcome_unknown": item.outcome_unknown, "error_message": item.error_message, "created_at": item.created_at.isoformat()} for item in compositions],
    })
    return value


def list_categories(session: Session, market: str = "") -> dict[str, Any]:
    stored = session.scalars(select(PodProductCategory).order_by(PodProductCategory.sort_order, PodProductCategory.name)).all()
    expression = category_expression() if market else CatalogProduct.category
    condition = (CatalogProduct.source_type == ("pod_amazon" if market == "US" else "pod")) if market else CatalogProduct.source_type.in_(["pod", "pod_amazon"])
    counts = {str(name or ""): int(count) for name, count in session.execute(select(expression, func.count()).where(condition).group_by(expression)).all()}
    known = {item.name for item in stored}
    items = [{"id": item.id, "name": item.name, "count": counts.get(item.name, 0), "is_system": item.is_system} for item in stored if not market or counts.get(item.name, 0)]
    items.extend({"id": f"legacy:{name}", "name": name, "count": count, "is_system": False} for name, count in sorted(counts.items()) if name and name not in known)
    return {"items": items, "uncategorized": counts.get("", 0)}


def create_category(session: Session, name: str) -> dict[str, Any]:
    clean = str(name or "").strip()[:200]
    if not clean: raise AppError("分类名称不能为空", code="pod_category_empty")
    existing = session.scalar(select(PodProductCategory).where(func.lower(PodProductCategory.name) == clean.casefold()))
    if not existing:
        existing = PodProductCategory(name=clean, sort_order=int(session.scalar(select(func.count()).select_from(PodProductCategory)) or 0)); session.add(existing); session.commit(); session.refresh(existing)
    return {"id": existing.id, "name": existing.name}


def rename_category(session: Session, category_id: str, name: str) -> dict[str, Any]:
    item = session.get(PodProductCategory, category_id)
    if not item: raise NotFoundError("POD 产品分类不存在")
    clean = str(name or "").strip()[:200]
    if not clean: raise AppError("分类名称不能为空", code="pod_category_empty")
    old = item.name; item.name = clean
    for product in session.scalars(select(CatalogProduct).where(CatalogProduct.source_type.in_(["pod", "pod_amazon"]), CatalogProduct.category == old)).all(): product.category = clean
    session.commit(); return {"id": item.id, "name": item.name}


def delete_category(session: Session, category_id: str) -> dict[str, Any]:
    item = session.get(PodProductCategory, category_id)
    if not item: raise NotFoundError("POD 产品分类不存在")
    if item.is_system: raise ConflictError("系统分类不能删除")
    moved = 0
    for product in session.scalars(select(CatalogProduct).where(CatalogProduct.source_type.in_(["pod", "pod_amazon"]), CatalogProduct.category == item.name)).all(): product.category = ""; moved += 1
    session.delete(item); session.commit(); return {"deleted": True, "moved_products": moved}


def assign_category(session: Session, product_ids: list[str], category: str) -> int:
    ids = list(dict.fromkeys(value for value in product_ids if value))
    products = session.scalars(select(CatalogProduct).where(CatalogProduct.id.in_(ids), CatalogProduct.source_type.in_(["pod", "pod_amazon"]))).all()
    if len(products) != len(ids): raise NotFoundError("部分 POD 产品不存在")
    for product in products: product.category = category.strip()[:300]
    session.commit(); return len(products)


def product_folder(session: Session, product_id: str) -> Path:
    product = get_catalog_product(session, product_id)
    if product.source_type not in {"pod", "pod_amazon"}: raise ConflictError("该产品不属于 POD 产品库")
    version = session.scalar(select(PodProductVersion).where(PodProductVersion.catalog_product_id == product.id, PodProductVersion.is_current.is_(True)).order_by(PodProductVersion.version_no.desc()))
    candidates = [Path(version.legacy_output_folder)] if version and version.legacy_output_folder else []
    candidates.extend(Path(link.asset.local_path).parent for link in product.asset_links if link.is_current and link.asset.local_path)
    restored = get_settings().data_dir / "pod" / "restored" / product.sku
    candidates.append(restored)
    folder = next((path for path in candidates if path.is_dir()), None)
    if not folder: raise NotFoundError("产品本地文件夹不存在；如资产在 OSS，请先执行“恢复到本机”")
    return folder


def open_product_folder(session: Session, product_id: str) -> dict[str, Any]:
    folder = product_folder(session, product_id)
    if sys.platform == "win32": os.startfile(str(folder))  # type: ignore[attr-defined]
    else: subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"opened": True, "path": str(folder)}


def enqueue_restore(session: Session, product_id: str) -> dict[str, Any]:
    product = get_catalog_product(session, product_id)
    if product.source_type not in {"pod", "pod_amazon"}: raise ConflictError("该产品不属于 POD 产品库")
    if get_settings().multiuser_enabled:
        # Freeze only the requested product's current assets; no workstation paths or credentials.
        from urllib.parse import urlsplit
        assets = {link.asset.id: link.asset for link in product.asset_links if link.is_current}
        files = []
        for asset in assets.values():
            url = urlsplit(asset.oss_url or '')
            if asset.mirror_status == 'deleted' or url.scheme != 'https' or url.hostname != 'example-assets.oss-cn-chengdu.aliyuncs.com' or url.username or url.password or url.port not in (None,443):
                raise ConflictError('当前产品存在尚未归档到 OSS 的图片，不能完整恢复')
            files.append({'id':asset.id,'url':asset.oss_url,'sha256':asset.sha256,'size_bytes':asset.size_bytes,'mime_type':asset.mime_type})
        if not files:raise ConflictError('当前产品没有可恢复的图片')
        job=enqueue_job(session,job_type='pod_library_restore',payload={'product_id':product.id,'sku':product.sku,'files':files},config_snapshot=dumps({'product_id':product.id}),idempotency_key=f'pod_library_restore:{product.id}:{__import__("uuid").uuid4().hex}',steps=[('restore','从 OSS 恢复产品文件'),('verify','校验恢复文件')],max_attempts=3)
        return {'job':job_dict(job)}
    service = PodOssService.from_session(session)
    if not service: raise AppError("POD 产品资产 OSS 未配置", code="pod_oss_not_configured", status_code=503)
    job = enqueue_job(session, job_type="pod_library_restore", payload={"product_id": product.id, "oss_connection_id": service.connection_id}, config_snapshot=dumps({"product_id": product.id, "sku": product.sku, "oss_connection_id": service.connection_id}), idempotency_key=f"pod_library_restore:{product.id}:{__import__('uuid').uuid4().hex}", steps=[("restore", "从 OSS 恢复产品文件"), ("verify", "校验恢复文件")], max_attempts=3)
    return {"job": job_dict(job)}


@job_handler("pod_library_restore")
def restore_product_job(context: JobContext) -> None:
    product = get_catalog_product(context.session, str(context.payload.get("product_id") or ""))
    service = PodOssService.from_session(context.session, str(context.payload.get("oss_connection_id") or ""))
    if not service: raise AppError("任务快照中的 POD OSS 连接不可用", code="pod_oss_not_configured", status_code=503)
    links = [link for link in product.asset_links if link.is_current]
    missing = [link for link in links if not (link.asset.local_path and Path(link.asset.local_path).is_file())]
    context.start_step("restore")
    restored = skipped = 0
    for index, link in enumerate(missing, 1):
        context.refresh(); asset = link.asset
        if not asset.oss_key: skipped += 1; continue
        suffix = Path(asset.name).suffix or Path(asset.oss_key).suffix or mimetypes.guess_extension(asset.mime_type) or ".bin"
        target = get_settings().data_dir / "pod" / "restored" / product.sku / f"{link.role}-{link.position:03d}-{asset.id[:10]}{suffix}"
        result = service.download_asset(key=asset.oss_key, target=target, expected_sha256=asset.sha256)
        asset.local_path = str(target); asset.size_bytes = int(result["size"]); asset.sha256 = str(result["sha256"]); asset.read_only = True
        restored += 1; context.session.commit(); context.set_progress("restore", index * 100 / max(1, len(missing)), f"已恢复 {index}/{len(missing)} 个文件")
    context.complete_step("restore", details={"restored": restored, "already_local": len(links) - len(missing), "skipped_without_oss_key": skipped})
    context.start_step("verify")
    available = sum(bool(link.asset.local_path and Path(link.asset.local_path).is_file()) for link in links)
    context.complete_step("verify", details={"available": available, "total": len(links)})
