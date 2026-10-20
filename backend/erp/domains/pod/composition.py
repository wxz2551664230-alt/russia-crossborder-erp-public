from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from erp.core.config import get_settings
from erp.core.errors import AppError, ConflictError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.domains.pod.integrations.oss import PodOssService
from erp.domains.pod.integrations.registry import supplier_client
from erp.domains.pod.models import (
    PodArtwork, PodColorImageGroup, PodComposition, PodCompositionSide, PodProductCategory,
    PodProductCodeHistory, PodProductVersion, PodRemoteGalleryAsset,
)
from erp.models import Asset, CatalogProduct, CatalogProductAsset, CatalogVariant, Job, JobArtifact
from erp.services.catalog import link_catalog_asset, refresh_duplicate_suggestions
from erp.services.assets import resolve_asset_path
from erp.services.jobs import JobContext, JobPaused, enqueue_job, job_handler


SOURCE_KIND = "pod_native_composition"
IMAGE_LIMIT = 30 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _composition_payload(item: PodComposition) -> dict[str, Any]:
    raw = loads(item.raw_json, {})
    sides = list(raw.get("sides") or [])
    return {
        "market": str(raw.get("market") or "CN"),
        "id": item.id, "job_id": item.job_id, "supplier": item.supplier,
        "status": item.status, "stage_text": item.stage_text, "product_name": item.product_name,
        "library_sku": item.library_sku, "product_type_id": item.product_type_id,
        "default_color_id": item.default_color_id, "default_view_id": item.default_view_id,
        "remote_product_id": item.remote_product_id, "product_code": item.product_code,
        "external_id": item.external_id, "outcome_unknown": item.outcome_unknown,
        "error": item.error_message, "selected_size_ids": loads(item.selected_size_ids_json, []),
        "rendering_groups": loads(item.rendering_groups_json, []),
        "oss_assets": loads(item.oss_assets_json, []), "saved_files": list(raw.get("saved_files") or []),
        "sides": sides, "folder": str(raw.get("folder") or ""),
        "folder_name": item.library_sku or item.id[:10].upper(),
        "progress_current": int(raw.get("progress_current") or 0),
        "progress_total": int(raw.get("progress_total") or 0),
        "created_at": item.created_at.isoformat(), "updated_at": item.updated_at.isoformat(),
    }


def composition_dict(item: PodComposition) -> dict[str, Any]:
    return _composition_payload(item)


def list_compositions(session: Session, *, limit: int = 500) -> dict[str, Any]:
    items = session.scalars(
        select(PodComposition)
        .outerjoin(Job, Job.id == PodComposition.job_id)
        .where(PodComposition.status != "cleared")
        .order_by(
            case((Job.status == "failed", 0), (PodComposition.status == "failed", 0), else_=1),
            PodComposition.created_at.desc(),
        )
        .limit(limit)
    ).all()
    jobs = []
    for item in items:
        value = composition_dict(item)
        job = session.get(Job, item.job_id) if item.job_id else None
        if job:
            running = next((step.key for step in job.steps if step.status == "running"), "")
            stage_status = {"upload_gallery": "uploading", "create_product": "creating", "wait_rendering": "rendering", "download": "downloading", "oss_archive": "archiving"}
            value["status"] = stage_status.get(running, job.status) if job.status == "running" else job.status
            if value["status"] == "cancelled":
                value["status"] = "canceled"
            value["progress_current"] = int(job.progress)
            value["progress_total"] = 100
            value["error"] = job.error_message or value["error"]
            if job.status == "failed" and "No query results for model" in job.error_message and item.remote_product_id:
                value["error"] = f"S2B 供应商产品 {item.remote_product_id} 已不存在；恢复前需核对并重新上传设计图。"
        jobs.append(value)
    counts = {status: sum(item["status"] == status for item in jobs) for status in ("queued", "running", "paused", "completed", "failed", "canceled")}
    counts.update({"total": len(jobs), "active": sum(item["status"] in {"running", "uploading", "creating", "rendering", "downloading", "archiving"} for item in jobs)})
    return {"jobs": jobs, "counts": counts}


def clear_finished_compositions(session: Session) -> int:
    """Hide terminal queue cards while retaining jobs and all product assets for audit."""
    items = session.scalars(
        select(PodComposition).where(PodComposition.status != "cleared")
    ).all()
    removed = 0
    for item in items:
        job = session.get(Job, item.job_id) if item.job_id else None
        effective_status = job.status if job else item.status
        if effective_status not in {"completed", "failed", "cancelled", "canceled"}:
            continue
        item.status = "cleared"
        item.stage_text = "已从合成队列清除"
        removed += 1
    session.commit()
    return removed


def _find_artwork(session: Session, identity: str, *, require_local: bool = True) -> PodArtwork:
    item = session.get(PodArtwork, identity) or session.scalar(
        select(PodArtwork).where(PodArtwork.legacy_id == identity).order_by(PodArtwork.created_at)
    )
    if not item:
        raise NotFoundError("印刷面引用的设计图不存在")
    if require_local: resolve_asset_path(item.asset)
    return item


def _save_uploaded_asset(session: Session, composition_id: str, view_id: int, filename: str, content: bytes) -> Asset:
    if not content or len(content) > IMAGE_LIMIT:
        raise AppError("印刷图片为空或超过 30MB", code="pod_image_invalid", status_code=413)
    try:
        with Image.open(__import__("io").BytesIO(content)) as image:
            image.verify(); image_format = str(image.format or "").upper()
        with Image.open(__import__("io").BytesIO(content)) as image:
            width, height = image.size
    except Exception as error:
        raise AppError("印刷面文件不是有效图片", code="pod_image_invalid") from error
    suffix = {"PNG": ".png", "JPEG": ".jpg"}.get(image_format)
    if not suffix:
        raise AppError("印刷面只支持 PNG 或 JPG", code="pod_image_format_invalid")
    digest = hashlib.sha256(content).hexdigest()
    folder = get_settings().data_dir / "pod" / "compositions" / composition_id / "sources" / f"view-{view_id}"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"source-{digest[:12]}{suffix}"
    if not target.exists():
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(content); os.replace(temporary, target)
    asset = Asset(
        kind="image", name=Path(filename or target.name).name, source="pod_composition_upload",
        local_path=str(target), sha256=digest, mime_type="image/png" if suffix == ".png" else "image/jpeg",
        size_bytes=len(content), width=width, height=height, read_only=True,
        metadata_json=dumps({"composition_id": composition_id, "view_id": view_id}),
    )
    session.add(asset); session.flush()
    return asset


def _prepare_source_asset(
    session: Session, *, asset: Asset, composition_id: str, view_id: int, rotation: int, supplier: str,
) -> Asset:
    source = resolve_asset_path(asset)
    limit = 20 * 1024 * 1024 if supplier == "s2b" else IMAGE_LIMIT
    if rotation == 0 and source.stat().st_size <= limit:
        return asset
    folder = get_settings().data_dir / "pod" / "compositions" / composition_id / "sources" / f"view-{view_id}"
    folder.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as opened:
        image = opened.rotate(-rotation, expand=True) if rotation else opened.copy()
        has_alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info
        suffix = ".png" if has_alpha else ".jpg"
        target = folder / f"prepared-r{rotation}{suffix}"
        for _attempt in range(8):
            temporary = target.with_suffix(target.suffix + ".tmp")
            if suffix == ".png": image.save(temporary, "PNG", optimize=True)
            else: image.convert("RGB").save(temporary, "JPEG", quality=90, optimize=True, subsampling=0)
            os.replace(temporary, target)
            if target.stat().st_size <= limit: break
            image.thumbnail((max(512, int(image.width * .82)), max(512, int(image.height * .82))), Image.Resampling.LANCZOS)
        if target.stat().st_size > limit:
            raise AppError(f"处理后的印刷面仍超过 {limit // 1024 // 1024}MB", code="pod_image_too_large", status_code=413)
        width, height = image.size
    prepared = Asset(
        kind="image", name=f"{Path(asset.name).stem}-r{rotation}{suffix}", source="pod_composition_prepared",
        local_path=str(target), sha256=_sha256(target), mime_type="image/png" if suffix == ".png" else "image/jpeg",
        size_bytes=target.stat().st_size, width=width, height=height, read_only=True,
        metadata_json=dumps({"composition_id": composition_id, "view_id": view_id, "rotation": rotation, "source_asset_id": asset.id, "supplier_limit": limit}),
    )
    session.add(prepared); session.flush()
    return prepared


def create_native_composition(
    session: Session, *, request: dict[str, Any], uploads: list[tuple[str, bytes, str]] | None = None,
    defer_source_preparation: bool = False, job_idempotency_key: str | None = None,
) -> PodComposition:
    defer_source_preparation = defer_source_preparation or get_settings().multiuser_enabled
    if get_settings().multiuser_enabled and session.bind.dialect.name=='postgresql':
        from sqlalchemy import text
        session.execute(text('SELECT pg_advisory_xact_lock(90232301)'))
    supplier = str(request.get("supplier") or "hicustom").casefold()
    market = str(request.get("market") or "CN").upper()
    if market not in {"CN", "US"}:
        raise ConflictError("合成货盘国家无效")
    if market == "US":
        facts = request.get("variant_snapshot") or {}
        if (supplier != "s2b" or str(facts.get("produce_country") or "").upper() != "US"
                or str(facts.get("id") or "") != str(request.get("product_type_id") or "")):
            raise ConflictError("美国合成必须使用已核实的 S2B 美国货盘规格")
    if supplier not in {"hicustom", "s2b"}:
        raise AppError("货盘来源无效", code="pod_supplier_unsupported")
    placements = list(request.get("placements") or [])
    if not 1 <= len(placements) <= 12:
        raise AppError("每个任务必须配置 1 到 12 个印刷面", code="pod_composition_sides_invalid")
    view_ids = [int(item.get("view_id") or 0) for item in placements]
    if any(value <= 0 for value in view_ids) or len(view_ids) != len(set(view_ids)):
        raise AppError("印刷面无效或存在重复", code="pod_composition_views_invalid")
    if int(request.get("default_view_id") or 0) not in set(view_ids):
        raise ConflictError("默认印刷面必须已配置图案")
    composition = PodComposition(
        supplier=supplier, status="queued", stage_text="等待处理",
        product_name=str(request.get("product_name") or "")[:600],
        library_sku=_next_sku(session, supplier),
        product_type_id=int(request.get("product_type_id") or 0),
        default_color_id=int(request.get("default_color_id") or 0),
        default_view_id=int(request.get("default_view_id") or 0),
        external_id=f"erp-pod-{supplier}-{__import__('uuid').uuid4().hex}",
        selected_size_ids_json=dumps(request.get("selected_size_ids") or []),
        config_snapshot_json=dumps(request.get("config_snapshot") or {}),
    )
    if not composition.product_name or min(composition.product_type_id or 0, composition.default_color_id or 0) <= 0:
        raise AppError("产品、名称或默认颜色无效", code="pod_composition_product_invalid")
    session.add(composition); session.flush()
    upload_values = iter(uploads or [])
    side_payloads: list[dict[str, Any]] = []
    for placement in placements:
        view_id = int(placement["view_id"])
        rotation = int(placement.get("rotation") or 0) % 360
        if rotation not in {0, 90, 180, 270}:
            raise AppError("印刷面旋转角度无效", code="pod_composition_placement_invalid")
        artwork_identity = str(placement.get("artwork_id") or "").strip()
        if artwork_identity:
            artwork = _find_artwork(session, artwork_identity,require_local=not defer_source_preparation); asset = artwork.asset
        else:
            try: filename, content, _content_type = next(upload_values)
            except StopIteration as error: raise AppError("本地图片数量与印刷面不一致", code="pod_composition_upload_count") from error
            artwork = None; asset = _save_uploaded_asset(session, composition.id, view_id, filename, content)
        if not defer_source_preparation:
            asset = _prepare_source_asset(session, asset=asset, composition_id=composition.id, view_id=view_id, rotation=rotation, supplier=supplier)
        values = {
            "view_id": view_id, "view_name": str(placement.get("view_name") or "")[:200],
            "width": float(placement.get("width") or 0), "height": float(placement.get("height") or 0),
            "top_x": float(placement.get("top_x")), "top_y": float(placement.get("top_y")),
            "rotation": rotation,
        }
        if values["width"] <= 0 or values["height"] <= 0 or values["rotation"] not in {0, 90, 180, 270}:
            raise AppError("印刷面的尺寸、坐标或旋转角度无效", code="pod_composition_placement_invalid")
        side = PodCompositionSide(
            composition_id=composition.id, artwork_id=artwork.id if artwork else None, asset_id=asset.id,
            supplier=supplier, source_filename=asset.name, source_legacy_path=asset.local_path,
            mime_type=asset.mime_type, upload_status="pending", **values,
        )
        session.add(side); session.flush()
        side_payloads.append({**values, "side_id": side.id, "artwork_id": artwork.id if artwork else "", "asset_id": asset.id,
                              "source_path": asset.local_path, "source_filename": asset.name, "mime_type": asset.mime_type,
                              "source_prepared": not defer_source_preparation, "gallery_code": ""})
    try: next(upload_values); raise AppError("上传图片数量多于印刷面", code="pod_composition_upload_count")
    except StopIteration: pass
    folder = get_settings().data_dir / "pod" / "compositions" / composition.id
    folder.mkdir(parents=True, exist_ok=True)
    raw = dict(request); raw.update({"folder": str(folder), "sides": side_payloads, "saved_files": [], "progress_current": 0, "progress_total": 0})
    composition.raw_json = dumps(raw)
    connection_ids = dict((request.get("config_snapshot") or {}).get("connection_ids") or {})
    job = enqueue_job(
        session, job_type="pod_us_composition" if market == "US" else "pod_native_composition", payload={"composition_id": composition.id},
        config_snapshot=dumps({"supplier": supplier, "connection_ids": connection_ids, "product_type_id": composition.product_type_id}),
        idempotency_key=job_idempotency_key or f"pod_native_composition:{composition.id}", max_attempts=1 if get_settings().multiuser_enabled else 3,
        steps=[("prepare", "校验产品与规格"), ("upload_gallery", "上传印刷素材"),
               ("create_product", "创建供应商产品"), ("wait_rendering", "等待效果图"),
               ("download", "下载效果图"), ("oss_archive", "归档产品资产"),
               ("sync_catalog", "写入 POD 产品库")],
    )
    composition.job_id = job.id; session.commit(); session.refresh(composition)
    return composition


def _update_raw(item: PodComposition, **changes: Any) -> dict[str, Any]:
    raw = loads(item.raw_json, {}); raw.update(changes); item.raw_json = dumps(raw); return raw


def _pause_unknown(context: JobContext, item: PodComposition, error: AppError) -> None:
    if error.code != "pod_write_outcome_unknown" and not (isinstance(error.details, dict) and error.details.get("outcome_unknown")):
        raise error
    item.outcome_unknown = True; item.status = "paused"; item.stage_text = "外部写入结果未知，等待人工绑定结果"
    item.error_message = str(error); context.job.pause_requested = True; context.session.commit()
    raise JobPaused()


def _next_sku(session: Session, supplier: str) -> str:
    prefix = "S2B" if supplier == "s2b" else "HC"
    maximum = 0
    product_skus = session.scalars(
        select(CatalogProduct.sku).where(CatalogProduct.source_type.in_(["pod", "pod_amazon"]))
    ).all()
    reserved_skus = session.scalars(
        select(PodComposition.library_sku).where(PodComposition.library_sku != "")
    ).all()
    for sku in [*product_skus, *reserved_skus]:
        match = re.match(r"^(?:HC|S2B)-(\d{6})-(\d{2,4})$", str(sku), re.I)
        if match: maximum = max(maximum, int(match.group(1)))
    return f"{prefix}-{maximum + 1:06d}-01"


def _variant_sku(root_sku: str, position: int) -> str:
    match = re.fullmatch(r"(HC|S2B)-(\d{6})-(\d{2,4})", root_sku, re.I)
    if not match:
        raise AppError("POD 产品 SKU 格式无效", code="pod_sku_invalid")
    if position == 1:
        return root_sku
    return f"{match.group(1).upper()}-{int(match.group(2)):06d}-{position:02d}"


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result >= 0 else None
    except (TypeError, ValueError):
        return None


def _stock_price(snapshot: dict[str, Any], stock: dict[str, Any]) -> float | None:
    prices = snapshot.get("prices") if isinstance(snapshot.get("prices"), dict) else {}
    stock_id = str(stock.get("id") or stock.get("stock_item_id") or stock.get("stock_sku_item_id") or "")
    color_id, size_id = str(stock.get("color_id") or ""), str(stock.get("size_id") or "")
    for group in prices.get("wholesale_price") or []:
        if not isinstance(group, dict):
            continue
        matched = any(
            str(row.get("id") or row.get("stock_item_id") or row.get("stock_sku_item_id") or "") == stock_id
            or (str(row.get("color_id") or "") == color_id and str(row.get("size_id") or "") == size_id)
            for row in group.get("stock_info") or [] if isinstance(row, dict)
        )
        if matched:
            tier = str(prices.get("membership_price") or prices.get("accumulated_quarter_price") or "")
            selected = group.get(tier) if isinstance(group.get(tier), dict) else None
            retail = group.get("retail_price") if isinstance(group.get("retail_price"), dict) else None
            return _number((selected or retail or {}).get("price")) or _number(group.get("price"))
    return _number(prices.get("price"))


def apply_supplier_facts(product: CatalogProduct, snapshot: dict[str, Any]) -> dict[str, int]:
    """Map deterministic supplier facts to ERP fields; supplier dimensions are cm."""
    stocks = [row for row in snapshot.get("stock_info") or [] if isinstance(row, dict)]
    by_stock = {str(row.get("id") or row.get("stock_item_id") or row.get("stock_sku_item_id") or ""): row for row in stocks}
    updated = 0
    is_us = str(snapshot.get("produce_country") or "").upper() == "US"
    cny_verified = not is_us or (snapshot.get('supplier') == 's2b' and snapshot.get('currency') == 'CNY')
    dimensions_verified = not is_us or (snapshot.get('supplier') == 's2b' and snapshot.get('supplier_dimension_unit') == 'cm')
    weight_verified = not is_us or (snapshot.get('supplier') == 's2b' and snapshot.get('supplier_weight_unit') == 'g')
    for variant in product.variants:
        stock = by_stock.get(str(variant.supplier_sku_id or ""))
        if not stock:
            props = loads(variant.properties_json, {})
            stock = next((row for row in stocks if int(row.get("color_id") or 0) == int(props.get("color_id") or 0) and int(row.get("size_id") or 0) == int(props.get("size_id") or 0)), None)
        if not stock:
            continue
        variant.purchase_price_cny = _stock_price(snapshot, stock) if cny_verified else None
        variant.package_length_mm = ((_number(stock.get("length")) or 0) * 10 or None) if dimensions_verified else None
        variant.package_width_mm = ((_number(stock.get("width")) or 0) * 10 or None) if dimensions_verified else None
        variant.package_height_mm = ((_number(stock.get("height")) or 0) * 10 or None) if dimensions_verified else None
        variant.weight_grams = _number(stock.get("weight")) if weight_verified else None
        variant.specification = str(stock.get("code") or stock.get("stock_code") or variant.specification or "")
        variant.properties_json = dumps({**loads(variant.properties_json, {}), **stock, "supplier_dimension_unit": "cm" if dimensions_verified else "unverified",
                                        **({"supplier_price_raw": _stock_price(snapshot, stock), "supplier_currency": str(snapshot.get("currency") or "")} if is_us else {})})
        updated += 1
    active = [row for row in product.variants if row.active]
    for product_key, variant_key in (("purchase_price_cny", "purchase_price_cny"), ("package_length_mm", "package_length_mm"), ("package_width_mm", "package_width_mm"), ("package_height_mm", "package_height_mm"), ("package_weight_g", "weight_grams")):
        values = [getattr(row, variant_key) for row in active]
        setattr(product, product_key, values[0] if values and values[0] is not None and all(value == values[0] for value in values) else None)
    product.currency = str(snapshot.get("currency") or "") if is_us else "CNY"
    product.source_snapshot_json = dumps(snapshot)
    return {"variants_updated": updated}


def _sync_product(session: Session, item: PodComposition, *, commit: bool = True) -> CatalogProduct:
    raw = loads(item.raw_json, {}); snapshot = dict(raw.get("variant_snapshot") or {})
    is_us = raw.get("market") == "US"
    if is_us and (item.supplier != "s2b" or str(snapshot.get("produce_country") or "").upper() != "US"):
        raise ConflictError("美国合成结果缺少已核实的美国货盘事实")
    product = session.get(CatalogProduct, item.catalog_product_id) if item.catalog_product_id else None
    if product and product.source_type != ("pod_amazon" if is_us else "pod"):
        raise ConflictError("合成结果不能覆盖另一货盘国家的产品")
    if not product:
        item.library_sku = item.library_sku or _next_sku(session, item.supplier)
        product = CatalogProduct(sku=item.library_sku, group_code=item.library_sku.rsplit("-", 1)[0], source_type="pod_amazon" if is_us else "pod")
        session.add(product); session.flush(); item.catalog_product_id = product.id
    product.name = item.product_name; product.supplier = "S2B" if item.supplier == "s2b" else "HICUSTOM"
    if is_us and not product.category:
        product.category = "Amazon 美国 POD"
    category_id = str(raw.get("category_id") or "")
    if category_id and (not is_us or product.category == "Amazon 美国 POD"):
        category = session.get(PodProductCategory, category_id)
        if category: product.category = category.name
    product.supplier_product_id = str(item.product_type_id or ""); product.status = "ready"
    product.production_json = dumps({"market": "US" if is_us else "CN", "fulfillment": "FBM" if is_us else "", "supplier": item.supplier, "product_type_id": item.product_type_id,
        "product_code": item.product_code, "remote_product_id": item.remote_product_id,
        "default_color_id": item.default_color_id, "default_view_id": item.default_view_id,
        "selected_size_ids": loads(item.selected_size_ids_json, []), "composition_id": item.id})
    product.source_snapshot_json = dumps(snapshot)
    selected = set(loads(item.selected_size_ids_json, [])); color_id = int(item.default_color_id or 0)
    stocks = [row for row in snapshot.get("stock_info") or [] if isinstance(row, dict)
              and int(row.get("color_id") or 0) == color_id and (not selected or int(row.get("size_id") or 0) in selected)]
    sizes = {int(row.get("id") or row.get("size_id") or 0): str(row.get("cn_name") or row.get("name") or "") for row in snapshot.get("sizes") or [] if isinstance(row, dict)}
    colors = {int(row.get("id") or row.get("color_id") or 0): str(row.get("cn_name") or row.get("name") or "") for row in snapshot.get("colors") or [] if isinstance(row, dict)}
    for old in list(product.variants): session.delete(old)
    for position, stock in enumerate(stocks or [{"color_id": color_id, "size_id": value} for value in sorted(selected)], 1):
        size_id = int(stock.get("size_id") or 0); stock_id = str(stock.get("id") or stock.get("stock_sku_item_id") or "")
        variant = CatalogVariant(product_id=product.id, sku=_variant_sku(item.library_sku, position),
            supplier_product_id=str(item.product_type_id or ""), supplier_sku_id=stock_id,
            specification=str(stock.get("stock_code") or ""), color=colors.get(color_id, ""), size=sizes.get(size_id, ""),
            properties_json=dumps({**stock, "color_id": color_id, "size_id": size_id}), active=True)
        session.add(variant)
    session.flush()
    session.expire(product, ["variants"])
    apply_supplier_facts(product, snapshot)
    version_no = int(session.scalar(select(func.max(PodProductVersion.version_no)).where(PodProductVersion.catalog_product_id == product.id)) or 0) + 1
    for current in session.scalars(select(PodProductVersion).where(PodProductVersion.catalog_product_id == product.id, PodProductVersion.is_current.is_(True))).all(): current.is_current = False
    version = PodProductVersion(catalog_product_id=product.id, version_no=version_no, is_current=True,
        legacy_job_id=item.job_id or "", job_status="completed", supplier=item.supplier,
        product_type_id=item.product_type_id, default_color_id=item.default_color_id, default_view_id=item.default_view_id,
        product_code=item.product_code, product_code_status="active", legacy_output_folder=str(raw.get("folder") or ""),
        recipe_json=dumps({
            "market": "US" if is_us else "CN", "supplier": item.supplier, "product_type_id": item.product_type_id,
            "remote_product_id": int(item.remote_product_id) if str(item.remote_product_id).isdigit() else 0,
            "product_name": item.product_name, "default_color_id": item.default_color_id,
            "default_view_id": item.default_view_id, "selected_size_ids": list(selected),
            "sides": raw.get("sides") or [],
        }),
        assets_json=dumps({"oss_assets": loads(item.oss_assets_json, []), "saved_files": raw.get("saved_files") or []}))
    session.add(version); session.flush()
    now = datetime.now(timezone.utc).isoformat()
    session.add(PodProductCodeHistory(version_id=version.id, product_code=item.product_code, status="active", first_seen_at=now, last_seen_at=now))
    for link in session.scalars(select(CatalogProductAsset).where(CatalogProductAsset.product_id == product.id, CatalogProductAsset.source_kind == SOURCE_KIND)).all(): session.delete(link)
    for position, side in enumerate(session.scalars(select(PodCompositionSide).where(PodCompositionSide.composition_id == item.id).order_by(PodCompositionSide.view_id)).all()):
        if side.asset_id:
            link_catalog_asset(session, product=product, asset=session.get(Asset, side.asset_id), role="artwork", position=position, source_kind=SOURCE_KIND, metadata={"view_id": side.view_id, "rotation": side.rotation})
    groups = loads(item.rendering_groups_json, [])
    rendering_position = 0
    for group in groups:
        session.add(PodColorImageGroup(version_id=version.id, color_id=int(group.get("color_id") or 0), color_name=str(group.get("color_name") or ""), rendering_urls_json=dumps(group.get("rendering_urls") or []), saved_files_json=dumps(group.get("saved_files") or [])))
        for asset_id in group.get("asset_ids") or []:
            asset = session.get(Asset, asset_id)
            if asset:
                link_catalog_asset(session, product=product, asset=asset, role="main" if rendering_position == 0 else "mockup", position=rendering_position, source_kind=SOURCE_KIND, metadata={"color_id": group.get("color_id")})
                rendering_position += 1
    session.flush()
    session.expire(product, ["asset_links", "variants"])
    refresh_duplicate_suggestions(session, product.id)
    if commit: session.commit()
    return product


@job_handler("pod_native_composition")
@job_handler("pod_us_composition")
def native_composition_job(context: JobContext) -> None:
    item = context.session.get(PodComposition, str(context.payload.get("composition_id") or ""))
    if not item: raise NotFoundError("POD 合成记录不存在")
    if item.outcome_unknown and not item.product_code:
        context.job.pause_requested = True; context.session.commit(); raise JobPaused()
    raw = loads(item.raw_json, {}); sides = list(raw.get("sides") or [])
    context.start_step("prepare")
    for side in sides:
        source_asset = context.session.get(Asset, str(side.get("asset_id") or ""))
        if not source_asset:
            raise AppError("印刷面原图记录不存在", code="pod_source_missing")
        # Resolve by stable asset ID on this machine, never trust a snapshot's
        # absolute path from the computer that originally created the task.
        side["source_path"] = str(resolve_asset_path(source_asset))
        if not side.get("source_prepared"):
            prepared = _prepare_source_asset(
                context.session, asset=source_asset, composition_id=item.id,
                view_id=int(side.get("view_id") or 0), rotation=int(side.get("rotation") or 0), supplier=item.supplier,
            )
            side_row = context.session.get(PodCompositionSide, str(side.get("side_id") or ""))
            if not side_row:
                raise AppError("印刷面任务记录不存在", code="pod_source_missing")
            side_row.asset_id = prepared.id
            side_row.source_filename = prepared.name
            side_row.source_legacy_path = prepared.local_path
            side_row.mime_type = prepared.mime_type
            side.update({
                "asset_id": prepared.id, "source_path": str(resolve_asset_path(prepared)),
                "source_filename": prepared.name, "mime_type": prepared.mime_type,
                "source_prepared": True,
            })
            _update_raw(item, sides=sides)
            context.session.commit()
    context.complete_step("prepare", details={"side_count": len(sides), "selected_size_ids": loads(item.selected_size_ids_json, [])})
    connection_ids = loads(item.config_snapshot_json, {}).get("connection_ids") or {}
    connection_id = str(connection_ids.get(item.supplier) or "")
    with supplier_client(context.session, item.supplier, connection_id) as client:
        context.start_step("upload_gallery")
        for index, side in enumerate(sides, 1):
            if side.get("gallery_code"): continue
            try:
                result = client.upload_gallery(filename=str(side["source_filename"]), content=Path(side["source_path"]).read_bytes(), mime_type=str(side["mime_type"]), external_id=f"{item.external_id}-view-{side['view_id']}", external_customer_id=str(loads(item.config_snapshot_json, {}).get("external_customer_id") or ""))
            except AppError as error: _pause_unknown(context, item, error)
            side["gallery_code"] = str(result["code"]); side_row = context.session.get(PodCompositionSide, side["side_id"])
            side_row.gallery_code = side["gallery_code"]; side_row.upload_status = "completed"
            if not context.session.scalar(select(PodRemoteGalleryAsset).where(PodRemoteGalleryAsset.supplier == item.supplier, PodRemoteGalleryAsset.gallery_code == side["gallery_code"])):
                context.session.add(PodRemoteGalleryAsset(artwork_id=side_row.artwork_id, composition_side_id=side_row.id, supplier=item.supplier, gallery_code=side["gallery_code"], usage="product", protected=True, cleanup_candidate=False, product_name=item.product_name, filename=side_row.source_filename))
            _update_raw(item, sides=sides); context.session.commit(); context.set_progress("upload_gallery", index * 100 / len(sides), f"已上传 {index}/{len(sides)} 个印刷面")
        context.complete_step("upload_gallery", details={"gallery_codes": [side["gallery_code"] for side in sides]})
        context.start_step("create_product")
        if not item.product_code:
            try:
                product = client.create_product({**_composition_payload(item), "sides": sides, "external_customer_id": loads(item.config_snapshot_json, {}).get("external_customer_id", "")})
            except AppError as error: _pause_unknown(context, item, error)
            item.product_code = str(product.get("code") or ""); item.remote_product_id = str(product.get("id") or product.get("product_id") or "")
            item.outcome_unknown = False; context.session.commit()
        else: product = {}
        context.complete_step("create_product", details={"product_code": item.product_code, "remote_product_id": item.remote_product_id})
        context.start_step("wait_rendering")
        groups = client.rendering_groups(product, int(item.default_color_id or 0))
        for attempt in range(90):
            if groups: break
            context.refresh(); product = client.custom_product(item.remote_product_id or item.product_code)
            groups = client.rendering_groups(product, int(item.default_color_id or 0))
            if not groups: time.sleep(2)
            if attempt % 5 == 0: context.set_progress("wait_rendering", min(95, attempt * 100 / 90), "等待供应商生成效果图")
        if not groups: raise AppError("等待效果图超时，可稍后从原产品继续读取", code="pod_rendering_timeout")
        item.rendering_groups_json = dumps(groups); context.session.commit(); context.complete_step("wait_rendering", details={"group_count": len(groups)})
        context.start_step("download")
        folder = Path(str(loads(item.raw_json, {}).get("folder") or "")); total = sum(len(group.get("rendering_urls") or []) for group in groups); completed = 0; saved_files = []
        for group in groups:
            color_id = int(group.get("color_id") or item.default_color_id or 0); target_folder = folder / "renderings" / f"color-{color_id}"; target_folder.mkdir(parents=True, exist_ok=True)
            group.setdefault("saved_files", []); group.setdefault("asset_ids", [])
            for position, url in enumerate(group.get("rendering_urls") or [], 1):
                existing = next((value for value in group["saved_files"] if Path(value).name.startswith(f"effect-{position:02d}")), "")
                if existing and (folder / existing).is_file(): completed += 1; continue
                content, content_type = client.download_rendering(url); suffix = {"image/png": ".png", "image/webp": ".webp"}.get(content_type, ".jpg")
                target = target_folder / f"effect-{position:02d}{suffix}"; temporary = target.with_suffix(target.suffix + ".tmp"); temporary.write_bytes(content); os.replace(temporary, target)
                relative = target.relative_to(folder).as_posix(); group["saved_files"].append(relative); saved_files.append(relative)
                asset = Asset(kind="image", name=target.name, source="pod_rendering", local_path=str(target), sha256=_sha256(target), mime_type=content_type, size_bytes=len(content), read_only=True, metadata_json=dumps({"composition_id": item.id, "color_id": color_id, "position": position, "remote_url": url}))
                context.session.add(asset); context.session.flush(); group["asset_ids"].append(asset.id); context.session.add(JobArtifact(job_id=context.job.id, asset_id=asset.id, role="rendering"))
                completed += 1; item.rendering_groups_json = dumps(groups); _update_raw(item, saved_files=saved_files, progress_current=completed, progress_total=total); context.session.commit(); context.set_progress("download", completed * 100 / max(1, total), f"已下载效果图 {completed}/{total}")
        context.complete_step("download", details={"saved_files": saved_files})
    context.start_step("oss_archive")
    oss_assets = loads(item.oss_assets_json, []); service = PodOssService.from_session(context.session)
    if service:
        archived_ids = {str(value.get("asset_id") or "") for value in oss_assets}
        for position, side in enumerate(sides, 1):
            asset_id = str(side.get("asset_id") or "")
            if not asset_id or asset_id in archived_ids:
                continue
            asset = context.session.get(Asset, asset_id)
            if not asset:
                raise AppError(f"印刷面 {side.get('view_id')} 原图无法归档到 OSS", code="pod_source_archive_missing")
            if asset.oss_url and asset.oss_key:
                cloud = {
                    "oss_bucket": asset.oss_bucket, "oss_key": asset.oss_key,
                    "oss_url": asset.oss_url, "sha256": asset.sha256,
                    "content_type": asset.mime_type, "size": asset.size_bytes,
                    "asset_type": "source", "view_id": int(side.get("view_id") or 0),
                }
            else:
                cloud = service.upload_product_asset(
                    resolve_asset_path(asset), sku=item.library_sku or item.id, asset_type="source",
                    view_id=int(side.get("view_id") or 0), position=position,
                )
                asset.oss_bucket = str(cloud["oss_bucket"]); asset.oss_key = str(cloud["oss_key"])
                asset.oss_url = str(cloud["oss_url"]); asset.mirror_status = "mirrored"
            oss_assets.append({**cloud, "asset_id": asset.id, "local_path": asset.local_path,
                               "source_filename": side.get("source_filename")})
            item.oss_assets_json = dumps(oss_assets); context.session.commit()
        for group in groups:
            for position, asset_id in enumerate(group.get("asset_ids") or [], 1):
                if asset_id in archived_ids: continue
                asset = context.session.get(Asset, asset_id); cloud = service.upload_product_asset(resolve_asset_path(asset), sku=item.library_sku or item.id, asset_type="rendering", color_id=int(group.get("color_id") or 0), position=position)
                asset.oss_bucket = str(cloud["oss_bucket"]); asset.oss_key = str(cloud["oss_key"]); asset.oss_url = str(cloud["oss_url"]); asset.mirror_status = "mirrored"
                oss_assets.append({**cloud, "asset_id": asset.id}); item.oss_assets_json = dumps(oss_assets); context.session.commit()
        context.complete_step("oss_archive", details={"asset_count": len(oss_assets)})
    else:
        context.complete_step("oss_archive", details={"status": "not_configured", "local_preserved": True})
    context.start_step("sync_catalog"); product = _sync_product(context.session, item)
    item.status = "completed"; item.stage_text = "合成完成并已进入 POD 产品库"; item.error_message = ""; context.session.commit()
    side_ids = select(PodCompositionSide.id).where(PodCompositionSide.composition_id == item.id)
    for gallery in context.session.scalars(select(PodRemoteGalleryAsset).where(PodRemoteGalleryAsset.composition_side_id.in_(side_ids))).all():
        if gallery.usage != "ordered":
            gallery.usage = "pending"; gallery.protected = False; gallery.cleanup_candidate = True
    context.session.commit()
    context.complete_step("sync_catalog", details={"catalog_product_id": product.id, "library_sku": item.library_sku})


def bind_unknown_result(session: Session, composition_id: str, *, product_code: str, remote_product_id: str = "") -> PodComposition:
    item = session.get(PodComposition, composition_id)
    if not item: raise NotFoundError("POD 合成记录不存在")
    if not item.outcome_unknown: raise ConflictError("当前任务没有待绑定的未知结果")
    if not product_code.strip(): raise AppError("产品编号不能为空", code="pod_product_code_empty")
    item.product_code = product_code.strip(); item.remote_product_id = remote_product_id.strip(); item.outcome_unknown = False
    item.error_message = ""; item.stage_text = "已绑定原建品结果，可继续读取效果图"; session.commit()
    return item


def bind_unknown_gallery(session: Session, composition_id: str, *, view_id: int, gallery_code: str) -> PodComposition:
    item = session.get(PodComposition, composition_id)
    if not item: raise NotFoundError("POD 合成记录不存在")
    if not item.outcome_unknown: raise ConflictError("当前任务没有待绑定的未知结果")
    side = session.scalar(select(PodCompositionSide).where(
        PodCompositionSide.composition_id == item.id, PodCompositionSide.view_id == view_id,
    ))
    if not side: raise NotFoundError("指定印刷面不存在")
    if side.gallery_code: raise ConflictError("该印刷面已有供应商素材编号")
    code = gallery_code.strip()
    if not code: raise AppError("供应商素材编号不能为空", code="pod_gallery_code_empty")
    side.gallery_code = code; side.upload_status = "completed"
    raw = loads(item.raw_json, {}); sides = list(raw.get("sides") or [])
    for value in sides:
        if int(value.get("view_id") or 0) == view_id: value["gallery_code"] = code
    raw["sides"] = sides; item.raw_json = dumps(raw); item.outcome_unknown = False
    item.error_message = ""; item.stage_text = "已绑定原素材上传结果，可继续处理"
    if not session.scalar(select(PodRemoteGalleryAsset).where(
        PodRemoteGalleryAsset.supplier == item.supplier, PodRemoteGalleryAsset.gallery_code == code,
    )):
        session.add(PodRemoteGalleryAsset(
            artwork_id=side.artwork_id, composition_side_id=side.id, supplier=item.supplier,
            gallery_code=code, usage="product", protected=True, cleanup_candidate=False,
            product_name=item.product_name, filename=side.source_filename,
        ))
    session.commit(); return item
