from __future__ import annotations

import hashlib
import io
import os
import re
import uuid
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp.core.config import get_settings
from erp.core.errors import AppError, ConflictError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.domains.pod.integrations.oss import PodOssService
from erp.domains.pod.models import PodArtwork, PodArtworkCategory, PodComposition, PodCompositionSide
from erp.domains.pod.supplier_catalog import catalog_view
from erp.models import Asset, Job, JobEvent, JobStep
from erp.services.jobs import JobContext, enqueue_job, job_handler


MAX_ARTWORK_BYTES = 30 * 1024 * 1024
MAX_ARTWORK_PIXELS = 300_000_000
Image.MAX_IMAGE_PIXELS = MAX_ARTWORK_PIXELS


def _safe_name(value: str, fallback: str = "图片") -> str:
    cleaned = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", str(value or "").strip())
    return cleaned[:500] or fallback


def _category_payload(session: Session) -> list[dict[str, Any]]:
    counts = dict(session.execute(
        select(PodArtwork.category_id, func.count(PodArtwork.id)).group_by(PodArtwork.category_id)
    ).all())
    rows = session.scalars(select(PodArtworkCategory).order_by(PodArtworkCategory.sort_order, PodArtworkCategory.name)).all()
    return [{"id": row.id, "name": row.name, "count": int(counts.get(row.id, 0)), "is_system": row.is_system} for row in rows]


def _system_category(session: Session) -> PodArtworkCategory:
    category = session.scalar(select(PodArtworkCategory).where(PodArtworkCategory.is_system.is_(True)))
    if category:
        return category
    category = session.scalar(select(PodArtworkCategory).where(PodArtworkCategory.name == "未分类"))
    if category:
        category.is_system = True
        return category
    category = PodArtworkCategory(
        name="未分类",
        sort_order=0,
        is_system=True,
        legacy_source="pod_artwork_library",
        legacy_id="uncategorized",
    )
    session.add(category)
    session.flush()
    return category


def artwork_dict(item: PodArtwork) -> dict[str, Any]:
    asset = item.asset
    return {
        "id": item.id,
        "legacy_id": item.legacy_id,
        "name": item.name,
        "category_id": item.category_id or "",
        "category_name": item.category.name if item.category else "未分类",
        "mime_type": item.mime_type,
        "sha256": item.sha256,
        "file_size": asset.size_bytes,
        "width": asset.width,
        "height": asset.height,
        "source": item.source,
        "oss_sync_status": item.oss_sync_status,
        "oss_sync_error": item.oss_sync_error,
        "oss_uploaded": bool(asset.oss_url),
        "oss_url": asset.oss_url,
        "thumbnail_url": f"/api/v1/pod/artworks/{item.id}/thumbnail",
        "file_url": f"/api/v1/pod/artworks/{item.id}/file",
        "usable_url": f"/api/v1/pod/artworks/{item.id}/usable",
        "uploaded_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def _product_key(value: str) -> tuple[str, int] | None:
    if not value:
        return None
    supplier, separator, source_id = value.partition(":")
    if not separator or supplier not in {"hicustom", "s2b", "sds"} or not source_id.isdigit() or int(source_id) <= 0:
        raise AppError("请选择有效的供应商产品", code="pod_artwork_product_invalid", status_code=422)
    return supplier, int(source_id)


def _market(value: str) -> str:
    if value not in {"CN", "US"}:
        raise AppError("请选择有效的合成货盘", code="pod_artwork_market_invalid", status_code=422)
    return value


def _usage_market(raw_json: str, job_type: str | None = None) -> str:
    return "US" if loads(raw_json, {}).get("market") == "US" or job_type == "pod_us_composition" else "CN"


def artwork_product_options(session: Session, *, search: str = "", limit: int = 60, market: str = "CN") -> dict[str, Any]:
    market = _market(market)
    if market == "US":
        from erp.domains.pod.us_supplier_catalog import read
        _, data = read(session)
    else:
        data = catalog_view(session)
    token = search.strip().casefold()
    products = []
    for item in data["products"]:
        if item.get("supplier") not in {"hicustom", "s2b"}:
            continue
        if market == "US" and (item.get("supplier") != "s2b" or str(item.get("produce_country") or "").upper() != "US"):
            continue
        key = str(item.get("catalog_key") or "")
        name = str(item.get("cn_name") or item.get("name") or item.get("en_name") or key)
        label = f"{item.get('supplier_name') or item.get('supplier')} · {name} · {item.get('source_id')}"
        if market == "US":
            label = f"S2B · 美国 · {name} · {item.get('code') or item.get('source_id')}"
        if token and token not in " ".join([label, key, str(item.get('source_id') or ''), str(item.get('en_name') or '')]).casefold():
            continue
        products.append({"key": key, "name": name, "supplier": item.get("supplier"), "source_id": item.get("source_id"), "label": label,
                         "market": market, "detail_cached": key in data.get("details", {}) if market == "US" else True})
        if len(products) >= limit:
            break
    return {"items": products, "market": market}


def _recorded_usage_rows(session: Session, artwork_ids: list[str], asset_ids: list[str]) -> list[tuple[PodCompositionSide, PodComposition, Job | None]]:
    if not artwork_ids:
        return []
    return session.execute(
        select(PodCompositionSide, PodComposition, Job)
        .join(PodComposition, PodComposition.id == PodCompositionSide.composition_id)
        .outerjoin(Job, Job.id == PodComposition.job_id)
        .where(or_(PodCompositionSide.artwork_id.in_(artwork_ids), PodCompositionSide.asset_id.in_(asset_ids)))
        .order_by(PodComposition.created_at.desc())
    ).all()


def _usage_is_active(composition: PodComposition, job: Job | None) -> bool:
    return composition.status not in {"failed", "cancelled"} and (job is None or job.status not in {"failed", "cancelled"})


def used_artwork_ids(session: Session, *, artwork_ids: list[str], supplier: str, product_type_id: int, market: str = "CN") -> set[str]:
    market = _market(market)
    rows = session.execute(
        select(PodCompositionSide.artwork_id, PodComposition.raw_json, Job.type)
        .join(PodComposition, PodComposition.id == PodCompositionSide.composition_id)
        .outerjoin(Job, Job.id == PodComposition.job_id)
        .where(PodCompositionSide.artwork_id.in_(artwork_ids), PodComposition.supplier == supplier,
               PodComposition.product_type_id == product_type_id,
               PodComposition.status.notin_(["failed", "cancelled"]),
               or_(Job.id.is_(None), Job.status.notin_(["failed", "cancelled"])))
    ).all()
    return {artwork_id for artwork_id, raw_json, job_type in rows if _usage_market(raw_json, job_type) == market}


def artwork_usages(session: Session, artwork_id: str) -> dict[str, Any]:
    artwork = session.get(PodArtwork, artwork_id)
    if not artwork:
        raise NotFoundError("设计图不存在")
    rows = _recorded_usage_rows(session, [artwork.id], [artwork.asset_id])
    items = []
    for side, composition, job in rows:
        status = job.status if job else composition.status
        items.append({"composition_id": composition.id, "catalog_product_id": composition.catalog_product_id,
                      "product_name": composition.product_name, "supplier": composition.supplier,
                      "market": _usage_market(composition.raw_json, job.type if job else None),
                      "product_type_id": composition.product_type_id, "product_key": f"{composition.supplier}:{composition.product_type_id}",
                      "product_code": composition.product_code, "status": status,
                      "view_name": side.view_name, "created_at": composition.created_at.isoformat(),
                      "counts_as_used": _usage_is_active(composition, job)})
    return {"artwork_id": artwork.id, "items": items, "total": len(items)}


def list_artworks(
    session: Session, *, search: str, category_id: str, page: int, page_size: int,
    uploaded_from: str = "", uploaded_to: str = "", product_key: str = "", usage: str = "all", market: str = "CN",
) -> dict[str, Any]:
    market = _market(market)
    selected_product = _product_key(product_key)
    if usage not in {"all", "available", "used_selected", "unused_any"}:
        raise AppError("设计图使用状态无效", code="pod_artwork_usage_invalid", status_code=422)
    if usage in {"available", "used_selected"} and selected_product is None:
        raise AppError("请先选择产品", code="pod_artwork_product_required", status_code=422)
    conditions = []
    if search.strip():
        token = f"%{search.strip()}%"
        conditions.append(or_(PodArtwork.name.ilike(token), PodArtwork.sha256.ilike(token)))
    if category_id:
        conditions.append(PodArtwork.category_id == category_id)
    try:
        if uploaded_from:
            conditions.append(PodArtwork.created_at >= datetime.combine(datetime.fromisoformat(uploaded_from).date(), time.min))
        if uploaded_to:
            conditions.append(PodArtwork.created_at < datetime.combine(datetime.fromisoformat(uploaded_to).date() + timedelta(days=1), time.min))
    except ValueError as error:
        raise AppError("上传日期格式无效", code="pod_artwork_date_invalid", status_code=422) from error
    if usage != "all":
        used = session.execute(
            select(PodCompositionSide.artwork_id, PodCompositionSide.asset_id, PodComposition.raw_json, Job.type)
            .join(PodComposition, PodComposition.id == PodCompositionSide.composition_id)
            .outerjoin(Job, Job.id == PodComposition.job_id)
            .where(PodComposition.status.notin_(["failed", "cancelled"]), or_(Job.id.is_(None), Job.status.notin_(["failed", "cancelled"])),
                   *([PodComposition.supplier == selected_product[0], PodComposition.product_type_id == selected_product[1]] if usage != "unused_any" and selected_product else []))
        ).all()
        if usage != "unused_any":
            used = [row for row in used if _usage_market(row[2], row[3]) == market]
        used_ids = {row[0] for row in used if row[0]}
        used_assets = {row[1] for row in used if row[1]}
        match = or_(PodArtwork.id.in_(used_ids), PodArtwork.asset_id.in_(used_assets))
        conditions.append(match if usage == "used_selected" else ~match)
    total = int(session.scalar(select(func.count()).select_from(PodArtwork).where(*conditions)) or 0)
    items = session.scalars(
        select(PodArtwork).where(*conditions).order_by(PodArtwork.updated_at.desc(), PodArtwork.id)
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    usage_rows = _recorded_usage_rows(session, [item.id for item in items], [item.asset_id for item in items])
    by_id = {item.id: set() for item in items}
    by_asset = {item.asset_id: item.id for item in items}
    selected_used = set()
    for side, composition, job in usage_rows:
        artwork_id = side.artwork_id if side.artwork_id in by_id else by_asset.get(side.asset_id)
        if not artwork_id or not _usage_is_active(composition, job):
            continue
        key = f"{composition.supplier}:{composition.product_type_id}"
        used_market = _usage_market(composition.raw_json, job.type if job else None)
        by_id[artwork_id].add((used_market, key))
        if used_market == market and selected_product == (composition.supplier, composition.product_type_id):
            selected_used.add(artwork_id)
    payload = [{**artwork_dict(item), "used_product_count": len(by_id[item.id]),
                "used_by_selected": item.id in selected_used} for item in items]
    return {"items": payload, "total": total, "categories": _category_payload(session)}


def _validate_image(content: bytes, filename: str, content_type: str) -> tuple[str, str, int, int]:
    if not content:
        raise AppError("请选择需要上传的图片", code="pod_artwork_empty")
    if len(content) > MAX_ARTWORK_BYTES:
        raise AppError("设计图不能超过 30MB", code="pod_artwork_too_large")
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
            image_format = str(image.format or "").upper()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise AppError("图片无法识别或像素尺寸过大", code="pod_artwork_invalid") from error
    if width * height > MAX_ARTWORK_PIXELS:
        raise AppError("设计图像素尺寸过大", code="pod_artwork_pixels_exceeded")
    suffix = {"PNG": ".png", "JPEG": ".jpg"}.get(image_format)
    if not suffix:
        raise AppError("设计图库只支持 PNG 或 JPG", code="pod_artwork_format_unsupported")
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    return suffix, mime, width, height


def create_artwork(session: Session, *, content: bytes, filename: str, content_type: str, category_id: str) -> dict[str, Any]:
    suffix, mime_type, width, height = _validate_image(content, filename, content_type)
    digest = hashlib.sha256(content).hexdigest()
    existing = session.scalar(select(PodArtwork).where(PodArtwork.sha256 == digest).order_by(PodArtwork.created_at))
    if existing:
        return {**artwork_dict(existing), "duplicate": True}
    category = (
        _system_category(session)
        if category_id == "uncategorized"
        else session.get(PodArtworkCategory, category_id) if category_id else None
    )
    if category_id and not category:
        raise NotFoundError("图库分类不存在")
    asset_root = get_settings().data_dir / "pod" / "assets" / "artworks" / digest[:2]
    asset_root.mkdir(parents=True, exist_ok=True)
    target = asset_root / f"{digest}{suffix}"
    if not target.exists():
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, target)
    asset = Asset(
        kind="image", name=_safe_name(filename, target.name), source="pod_artwork", local_path=str(target),
        sha256=digest, mime_type=mime_type, size_bytes=len(content), width=width, height=height,
        read_only=True, metadata_json=dumps({"pod_asset_kind": "artwork", "uploaded_in_erp": True}),
    )
    session.add(asset); session.flush()
    item = PodArtwork(
        asset_id=asset.id, category_id=category.id if category else None, name=asset.name, sha256=digest,
        mime_type=mime_type, source="erp_upload", source_relative_path="", oss_sync_status="pending",
    )
    session.add(item); session.flush()
    try:
        service = PodOssService.from_session(session)
        if service:
            cloud = service.upload_artwork(target, digest)
            asset.oss_bucket = str(cloud["oss_bucket"]); asset.oss_key = str(cloud["oss_key"]); asset.oss_url = str(cloud["oss_url"])
            asset.mirror_status = "mirrored"; item.oss_sync_status = "synced"; item.metadata_json = dumps({"oss_asset": cloud})
        else:
            item.oss_sync_status = "not_configured"
        session.commit()
    except Exception as error:
        item.oss_sync_status = "failed"; item.oss_sync_error = str(error)[:500]; asset.mirror_status = "failed"; session.commit()
    session.refresh(item)
    return artwork_dict(item)


def stage_artwork_upload(
    session: Session, *, content: bytes, filename: str, content_type: str, category_id: str, batch_id: str,
) -> str:
    """Durably accept bytes without doing image decoding, hashing or OSS work in the request."""
    if not content:
        raise AppError("请选择需要上传的图片", code="pod_artwork_empty")
    if len(content) > MAX_ARTWORK_BYTES:
        raise AppError("设计图不能超过 30MB", code="pod_artwork_too_large", status_code=413)
    folder = get_settings().data_dir / "pod" / "upload-staging" / batch_id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{uuid.uuid4().hex}.upload"
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, target)
    job = enqueue_job(
        session,
        job_type="pod_artwork_upload",
        payload={
            "staged_path": str(target), "filename": _safe_name(filename),
            "content_type": content_type, "category_id": category_id, "batch_id": batch_id,
        },
        config_snapshot=dumps({"category_id": category_id, "batch_id": batch_id}),
        idempotency_key=f"pod_artwork_upload:{batch_id}:{target.stem}",
        max_attempts=3,
        steps=[("validate", "校验设计图"), ("save", "保存并去重"), ("oss", "同步 OSS")],
    )
    return job.id


def stage_artwork_upload_batch(
    session: Session, *, uploads: list[tuple[bytes, str, str]], category_id: str, batch_id: str,
) -> list[dict[str, str]]:
    """Stage a whole browser selection with one SQLite transaction."""
    folder = get_settings().data_dir / "pod" / "upload-staging" / batch_id
    folder.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, str, str]] = []
    for content, filename, content_type in uploads:
        if not content:
            raise AppError(f"{filename}：图片为空", code="pod_artwork_empty")
        if len(content) > MAX_ARTWORK_BYTES:
            raise AppError(f"{filename}：设计图不能超过 30MB", code="pod_artwork_too_large", status_code=413)
        target = folder / f"{uuid.uuid4().hex}.upload"
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(content)
        os.replace(temporary, target)
        staged.append((target, _safe_name(filename), content_type))
    results: list[dict[str, str]] = []
    try:
        for target, filename, content_type in staged:
            job = Job(
                type="pod_artwork_upload", payload_json=dumps({
                    "staged_path": str(target), "filename": filename, "content_type": content_type,
                    "category_id": category_id, "batch_id": batch_id,
                }),
                config_snapshot_json=dumps({"category_id": category_id, "batch_id": batch_id}),
                idempotency_key=f"pod_artwork_upload:{batch_id}:{target.stem}", max_attempts=3,
            )
            session.add(job); session.flush()
            for position, (key, name) in enumerate([
                ("validate", "校验设计图"), ("save", "保存并去重"), ("oss", "同步 OSS"),
            ], 1):
                session.add(JobStep(job_id=job.id, key=key, name=name, position=position))
            session.add(JobEvent(job_id=job.id, message="任务已进入队列"))
            results.append({"filename": filename, "status": "queued", "job_id": job.id})
        session.commit()
    except Exception:
        session.rollback()
        for target, _, _ in staged:
            target.unlink(missing_ok=True)
        raise
    return results


@job_handler("pod_artwork_upload")
def artwork_upload_job(context: JobContext) -> None:
    path = Path(str(context.payload.get("staged_path") or ""))
    if not path.is_file():
        raise AppError("待上传的设计图文件不存在", code="pod_artwork_staging_missing")
    content = path.read_bytes()
    context.start_step("validate")
    _validate_image(content, str(context.payload.get("filename") or ""), str(context.payload.get("content_type") or ""))
    context.complete_step("validate", details={"size": len(content)})
    context.start_step("save")
    result = create_artwork(
        context.session, content=content,
        filename=str(context.payload.get("filename") or "artwork.png"),
        content_type=str(context.payload.get("content_type") or "application/octet-stream"),
        category_id=str(context.payload.get("category_id") or "uncategorized"),
    )
    context.complete_step("save", details={"artwork_id": result["id"], "duplicate": bool(result.get("duplicate"))})
    context.start_step("oss")
    context.complete_step("oss", details={"status": result.get("oss_sync_status", "")})
    # Staging is disposable only after the durable library record exists.
    try:
        path.unlink(missing_ok=True)
        path.parent.rmdir()
    except OSError:
        pass


def create_category(session: Session, name: str) -> PodArtworkCategory:
    normalized = _safe_name(name, "")
    if not normalized:
        raise AppError("分类名称不能为空", code="pod_artwork_category_empty")
    existing = session.scalar(select(PodArtworkCategory).where(func.lower(PodArtworkCategory.name) == normalized.casefold()))
    if existing:
        return existing
    item = PodArtworkCategory(name=normalized, sort_order=int(session.scalar(select(func.count()).select_from(PodArtworkCategory)) or 0))
    session.add(item)
    try: session.commit()
    except IntegrityError as error:
        session.rollback(); raise ConflictError("图库分类已存在") from error
    session.refresh(item); return item


def rename_category(session: Session, category_id: str, name: str) -> PodArtworkCategory:
    item = session.get(PodArtworkCategory, category_id)
    if not item: raise NotFoundError("图库分类不存在")
    if item.is_system: raise ConflictError("系统分类不能重命名")
    normalized = _safe_name(name, "")
    if not normalized: raise AppError("分类名称不能为空", code="pod_artwork_category_empty")
    duplicate = session.scalar(select(PodArtworkCategory).where(
        func.lower(PodArtworkCategory.name) == normalized.casefold(), PodArtworkCategory.id != item.id,
    ))
    if duplicate: raise ConflictError("图库分类已存在")
    item.name = normalized
    try: session.commit()
    except IntegrityError as error:
        session.rollback(); raise ConflictError("图库分类已存在") from error
    session.refresh(item); return item


def delete_category(session: Session, category_id: str) -> None:
    item = session.get(PodArtworkCategory, category_id)
    if not item: raise NotFoundError("图库分类不存在")
    if item.is_system: raise ConflictError("“未分类”是系统分类，不能删除")
    used = int(session.scalar(select(func.count()).select_from(PodArtwork).where(PodArtwork.category_id == item.id)) or 0)
    if used: raise ConflictError(f"分类“{item.name}”下还有 {used} 张图片，请先批量移动分类")
    session.delete(item); session.commit()


def assign_category(session: Session, artwork_ids: list[str], category_id: str) -> int:
    category = (
        _system_category(session)
        if category_id == "uncategorized"
        else session.get(PodArtworkCategory, category_id)
    )
    if not category: raise NotFoundError("图库分类不存在")
    unique_ids = list(dict.fromkeys(value for value in artwork_ids if value))
    items = session.scalars(select(PodArtwork).where(PodArtwork.id.in_(unique_ids))).all()
    if len(items) != len(unique_ids): raise NotFoundError("部分图库图片不存在，请刷新后重试")
    for item in items: item.category_id = category.id
    session.commit(); return len(items)


def artwork_file(session: Session, artwork_id: str, kind: str) -> tuple[Path, str]:
    item = session.get(PodArtwork, artwork_id)
    if not item: raise NotFoundError("图库图片不存在")
    from erp.services.assets import resolve_asset_path
    source = resolve_asset_path(item.asset)
    if kind == "file": return source, item.mime_type
    cache = get_settings().data_dir / "pod" / "cache" / ("thumbnails" if kind == "thumbnail" else "usable")
    cache.mkdir(parents=True, exist_ok=True)
    if kind == "thumbnail":
        target = cache / f"{item.sha256}.jpg"
        if not target.is_file() or target.stat().st_mtime < source.stat().st_mtime:
            with Image.open(source) as image:
                image.draft("RGB", (960, 720)); image.thumbnail((480, 360))
                if image.mode not in {"RGB", "L"}:
                    background = Image.new("RGB", image.size, "white")
                    background.paste(image.convert("RGBA"), mask=image.convert("RGBA").getchannel("A")); image = background
                image.convert("RGB").save(target, "JPEG", quality=82, optimize=True)
        return target, "image/jpeg"
    if source.stat().st_size <= 7 * 1024 * 1024: return source, item.mime_type
    target = cache / f"{item.sha256}.jpg"
    if not target.is_file() or target.stat().st_mtime < source.stat().st_mtime:
        with Image.open(source) as image:
            rgb = image.convert("RGB")
            rgb.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
            rgb.save(target, "JPEG", quality=88, subsampling=0, optimize=True)
    return target, "image/jpeg"
