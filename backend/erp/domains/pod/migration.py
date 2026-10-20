from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from erp.core.errors import AppError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.models import Asset, CatalogImportRun, CatalogProduct, IntegrationConnection
from erp.services.catalog import import_pod_database
from erp.services.jobs import JobContext, job_handler
from erp.domains.pod.models import (
    PodArtwork,
    PodArtworkCategory,
    PodCategoryRule,
    PodColorImageGroup,
    PodComposition,
    PodCompositionSide,
    PodDeletionTombstone,
    PodProductCategory,
    PodProductCodeHistory,
    PodProductVersion,
    PodRemoteGalleryAsset,
    PodSupplierCatalogSnapshot,
)


POD_ARTWORK_SOURCE = "pod_artwork_library"
POD_HISTORY_SOURCE = "pod_product_history"
POD_COMPOSITION_SOURCE = "pod_composition_history"
ACTIVE_ORDER_STATES = {"queued", "preparing_product", "checking", "submitting", "unknown", "submitted"}
Progress = Callable[[float, str], None]


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return fallback


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    names = [
        "product_categories",
        "category_product_type_rules",
        "product_deletion_tombstones",
        "products",
        "product_versions",
        "product_code_history",
        "product_color_images",
        "product_variants",
        "product_oss_assets",
    ]
    existing = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
    missing = [name for name in names if name not in existing]
    if missing:
        raise AppError("旧 POD 产品库结构不完整", code="pod_legacy_schema_invalid", details={"missing": missing})
    return {name: int(connection.execute(f"select count(*) from {name}").fetchone()[0]) for name in names}


def inspect_legacy_pod(root: Path) -> dict[str, Any]:
    root = root.resolve()
    data = root / "data"
    product_db = data / "product_library.db"
    orders_db = data / "orders.db"
    artwork_path = data / "artwork_library.json"
    jobs_path = data / "jobs.json"
    catalog_path = data / "hicustom_catalog.json"
    required = [product_db, orders_db, artwork_path, jobs_path, catalog_path, root / "图库" / "images"]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise NotFoundError("旧 POD 程序数据不完整", details={"missing": missing})

    with _readonly_connection(product_db) as connection:
        product_integrity = str(connection.execute("pragma integrity_check").fetchone()[0])
        product_counts = _table_counts(connection)
    with _readonly_connection(orders_db) as connection:
        orders_integrity = str(connection.execute("pragma integrity_check").fetchone()[0])
        order_counts = {
            row[0]: int(connection.execute(f'select count(*) from "{row[0]}"').fetchone()[0])
            for row in connection.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")
        }

    artwork = _read_json(artwork_path, {})
    uploads = artwork.get("uploads") or []
    assignments = artwork.get("assignments") or {}
    indexed_paths = {(root / "图库" / str(item.get("relative_path") or "")).resolve() for item in uploads}
    local_files = {path.resolve() for path in (root / "图库" / "images").rglob("*") if path.is_file()}
    jobs = _read_json(jobs_path, [])
    if isinstance(jobs, dict):
        jobs = jobs.get("jobs") or []
    catalog = _read_json(catalog_path, {})
    return {
        "source_root": str(root),
        "source_read_only": True,
        "database_integrity": {"product_library": product_integrity, "orders": orders_integrity},
        "product_counts": product_counts,
        "order_counts": order_counts,
        "artworks": {
            "uploads": len(uploads),
            "categories": len(artwork.get("categories") or []),
            "assignments": len(assignments),
            "indexed_files_present": len(indexed_paths & local_files),
            "indexed_files_missing": len(indexed_paths - local_files),
            "unindexed_files": len(local_files - indexed_paths),
            "recorded_oss_urls": sum(bool((item.get("oss_asset") or {}).get("oss_url")) for item in uploads),
        },
        "composition_jobs": {
            "total": len(jobs),
            "completed": sum(str(item.get("status") or "") == "completed" for item in jobs),
            "active": sum(str(item.get("status") or "") not in {"completed", "failed", "canceled"} for item in jobs),
        },
        "supplier_catalog": {
            "products": len(catalog.get("products") or []),
            "categories": len(catalog.get("categories") or []),
            "previews": len(catalog.get("previews") or {}),
            "details": len(catalog.get("details") or {}),
        },
    }


def _materialize_asset(source: Path, target: Path, expected_sha256: str) -> tuple[str, str]:
    if not source.is_file():
        raise AppError("旧图库原图不存在", code="pod_artwork_missing", details={"source": str(source)})
    actual = _sha256(source)
    if actual != expected_sha256:
        raise AppError(
            "旧图库原图校验失败",
            code="pod_artwork_hash_mismatch",
            details={"source": str(source), "expected": expected_sha256, "actual": actual},
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    method = "existing"
    if not target.exists():
        try:
            os.link(source, target)
            method = "hardlink"
        except OSError:
            shutil.copy2(source, target)
            method = "copy"
    if _sha256(target) != expected_sha256:
        raise AppError("ERP POD 资产写入后校验失败", code="pod_asset_verify_failed", details={"target": str(target)})
    return str(target), method


def _upsert_artwork_categories(session: Session, artwork: dict[str, Any]) -> dict[str, PodArtworkCategory]:
    result: dict[str, PodArtworkCategory] = {}
    values = [{"id": "uncategorized", "name": "未分类"}, *(artwork.get("categories") or [])]
    for position, raw in enumerate(values):
        legacy_id = str(raw.get("id") or "uncategorized")
        item = session.scalar(select(PodArtworkCategory).where(
            PodArtworkCategory.legacy_source == POD_ARTWORK_SOURCE,
            PodArtworkCategory.legacy_id == legacy_id,
        ))
        if not item:
            item = PodArtworkCategory(legacy_source=POD_ARTWORK_SOURCE, legacy_id=legacy_id, name=str(raw.get("name") or "未分类"))
            session.add(item)
        item.name = str(raw.get("name") or "未分类")
        item.sort_order = position
        item.is_system = legacy_id == "uncategorized"
        result[legacy_id] = item
    session.flush()
    return result


def _upsert_artworks(
    context: JobContext,
    root: Path,
    asset_root: Path,
    artwork: dict[str, Any],
) -> dict[str, Any]:
    session = context.session
    categories = _upsert_artwork_categories(session, artwork)
    assignments = artwork.get("assignments") or {}
    uploads = artwork.get("uploads") or []
    imported = reused = hardlinked = copied = 0
    by_legacy_id: dict[str, PodArtwork] = {}
    for position, raw in enumerate(uploads, 1):
        context.refresh()
        legacy_id = str(raw.get("id") or "")
        sha256 = str(raw.get("sha256") or "").lower()
        if not legacy_id or len(sha256) != 64:
            raise AppError("旧设计图库记录缺少 ID 或哈希", code="pod_artwork_record_invalid", details={"position": position})
        relative = str(raw.get("relative_path") or "")
        source = (root / "图库" / relative).resolve()
        suffix = source.suffix.casefold() or ".bin"
        target = asset_root / "artworks" / sha256[:2] / f"{sha256}{suffix}"
        local_path, method = _materialize_asset(source, target, sha256)
        hardlinked += int(method == "hardlink")
        copied += int(method == "copy")
        oss = raw.get("oss_asset") or {}

        asset = session.scalar(select(Asset).where(Asset.sha256 == sha256).order_by(Asset.created_at))
        if asset:
            reused += 1
        else:
            asset = Asset(legacy_source=POD_ARTWORK_SOURCE, legacy_id=legacy_id, name=str(raw.get("name") or source.name))
            session.add(asset)
        asset.kind = "image"
        asset.name = str(raw.get("name") or source.name)
        asset.source = "pod_artwork"
        asset.local_path = local_path
        asset.oss_bucket = str(oss.get("oss_bucket") or "")
        asset.oss_key = str(oss.get("oss_key") or "")
        asset.oss_url = str(oss.get("oss_url") or oss.get("oss_uri") or "")
        asset.sha256 = sha256
        asset.mime_type = str(raw.get("mime_type") or oss.get("content_type") or "application/octet-stream")
        asset.size_bytes = int(oss.get("size") or source.stat().st_size)
        asset.read_only = True
        asset.mirror_status = "mirrored" if asset.oss_url else "not_requested"
        asset.metadata_json = dumps({"pod_asset_kind": "artwork", "legacy_relative_path": relative, "materialization": method})
        session.flush()

        item = session.scalar(select(PodArtwork).where(
            PodArtwork.legacy_source == POD_ARTWORK_SOURCE,
            PodArtwork.legacy_id == legacy_id,
        ))
        if not item:
            item = PodArtwork(legacy_source=POD_ARTWORK_SOURCE, legacy_id=legacy_id, asset_id=asset.id, name=asset.name, sha256=sha256)
            session.add(item)
            imported += 1
        category_key = str(assignments.get(legacy_id) or raw.get("category_id") or "uncategorized")
        item.asset_id = asset.id
        item.category_id = categories.get(category_key, categories["uncategorized"]).id
        item.name = asset.name
        item.sha256 = sha256
        item.mime_type = asset.mime_type
        item.source = str(raw.get("source") or "legacy")
        item.source_relative_path = relative
        item.oss_sync_status = str(raw.get("oss_sync_status") or ("legacy" if asset.oss_url else "not_configured"))
        item.oss_sync_error = str(raw.get("oss_sync_error") or "")
        item.metadata_json = dumps({"oss_asset": oss})
        updated = _timestamp(raw.get("updated_at"))
        if updated:
            item.updated_at = updated
        by_legacy_id[legacy_id] = item
        if position % 25 == 0:
            session.commit()
            context.set_progress("artwork_assets", position * 100 / max(1, len(uploads)), f"已迁移并校验 {position}/{len(uploads)} 张设计原图")
    session.commit()

    indexed = {(root / "图库" / str(item.get("relative_path") or "")).resolve() for item in uploads}
    orphans = [path for path in (root / "图库" / "images").rglob("*") if path.is_file() and path.resolve() not in indexed]
    orphan_imported = 0
    for source in orphans:
        digest = _sha256(source)
        target = asset_root / "unassigned" / digest[:2] / f"{digest}{source.suffix.casefold()}"
        local_path, method = _materialize_asset(source, target, digest)
        asset = session.scalar(select(Asset).where(Asset.legacy_source == "pod_artwork_orphan", Asset.legacy_id == str(source.resolve()).casefold()))
        if not asset:
            asset = Asset(legacy_source="pod_artwork_orphan", legacy_id=str(source.resolve()).casefold(), name=source.name)
            session.add(asset)
            orphan_imported += 1
        asset.kind = "image"
        asset.source = "pod_artwork_unassigned"
        asset.local_path = local_path
        asset.sha256 = digest
        asset.mime_type = "image/png" if source.suffix.casefold() == ".png" else "image/jpeg"
        asset.size_bytes = source.stat().st_size
        asset.read_only = True
        asset.metadata_json = dumps({"pod_asset_kind": "unassigned", "legacy_path": str(source), "materialization": method})
    session.commit()
    context.complete_step("artwork_assets", details={
        "artworks": len(uploads), "inserted": imported, "asset_reused": reused,
        "hardlinked": hardlinked, "copied": copied, "unassigned_files": len(orphans), "unassigned_imported": orphan_imported,
    })
    return {"artworks_by_legacy_id": by_legacy_id, "artwork_count": len(uploads), "unassigned_count": len(orphans)}


def _upsert_product_history(context: JobContext, product_db: Path) -> dict[str, int]:
    session = context.session
    result = {"categories": 0, "rules": 0, "versions": 0, "codes": 0, "color_groups": 0, "tombstones": 0}
    with _readonly_connection(product_db) as connection:
        category_map: dict[str, PodProductCategory] = {}
        for raw in connection.execute("select * from product_categories order by sort_order,id"):
            legacy_id = str(raw["id"])
            item = session.scalar(select(PodProductCategory).where(PodProductCategory.legacy_source == POD_HISTORY_SOURCE, PodProductCategory.legacy_id == legacy_id))
            if not item:
                item = PodProductCategory(legacy_source=POD_HISTORY_SOURCE, legacy_id=legacy_id, name=str(raw["name"] or legacy_id))
                session.add(item); result["categories"] += 1
            item.name = str(raw["name"] or legacy_id); item.sort_order = int(raw["sort_order"] or 0); item.is_system = bool(raw["is_system"])
            category_map[legacy_id] = item
        session.flush()
        for raw in connection.execute("select * from category_product_type_rules"):
            legacy_id = f"{raw['product_type_id']}:{raw['category_id']}"
            item = session.scalar(select(PodCategoryRule).where(PodCategoryRule.legacy_source == POD_HISTORY_SOURCE, PodCategoryRule.legacy_id == legacy_id))
            if not item:
                item = PodCategoryRule(legacy_source=POD_HISTORY_SOURCE, legacy_id=legacy_id, supplier="hicustom", product_type_id=int(raw["product_type_id"]), category_id=category_map[str(raw["category_id"])].id)
                session.add(item); result["rules"] += 1
            item.category_id = category_map[str(raw["category_id"])].id
        session.flush()

        products = {str(row.legacy_id): row for row in session.scalars(select(CatalogProduct).where(CatalogProduct.legacy_source == "pod_product_library")).all()}
        current_versions = {str(row["current_version_id"]): str(row["id"]) for row in connection.execute("select id,current_version_id from products")}
        version_map: dict[str, PodProductVersion] = {}
        rows = list(connection.execute("select * from product_versions order by created_at,id"))
        for position, raw in enumerate(rows, 1):
            context.refresh()
            legacy_id = str(raw["id"])
            product = products.get(str(raw["product_id"]))
            if not product:
                raise AppError("POD 产品版本找不到已迁移产品", code="pod_product_history_orphan", details={"version_id": legacy_id, "product_id": raw["product_id"]})
            recipe = loads(str(raw["recipe_json"] or "{}"), {})
            item = session.scalar(select(PodProductVersion).where(PodProductVersion.legacy_source == POD_HISTORY_SOURCE, PodProductVersion.legacy_id == legacy_id))
            if not item:
                item = PodProductVersion(legacy_source=POD_HISTORY_SOURCE, legacy_id=legacy_id, catalog_product_id=product.id, version_no=int(raw["version_no"] or 1))
                session.add(item); result["versions"] += 1
            item.catalog_product_id = product.id
            item.version_no = int(raw["version_no"] or 1)
            item.is_current = current_versions.get(legacy_id) == str(raw["product_id"])
            item.legacy_job_id = str(raw["job_id"] or "")
            item.job_status = str(raw["job_status"] or "")
            item.supplier = str(recipe.get("supplier") or "hicustom")
            item.product_type_id = _safe_int(raw["product_type_id"])
            item.default_color_id = _safe_int(raw["default_color_id"])
            item.default_view_id = _safe_int(raw["default_view_id"])
            item.product_code = str(raw["product_code"] or "")
            item.product_code_status = str(raw["product_code_status"] or "")
            item.legacy_output_folder = str(raw["output_folder"] or "")
            item.recipe_json = str(raw["recipe_json"] or "{}")
            item.assets_json = str(raw["assets_json"] or "{}")
            created = _timestamp(raw["created_at"]); updated = _timestamp(raw["updated_at"])
            if created: item.created_at = created
            if updated: item.updated_at = updated
            version_map[legacy_id] = item
            if position % 250 == 0: session.commit()
        session.flush()

        for raw in connection.execute("select * from product_code_history order by first_seen_at,id"):
            version = version_map.get(str(raw["version_id"]))
            if not version: continue
            legacy_id = str(raw["id"])
            item = session.scalar(select(PodProductCodeHistory).where(PodProductCodeHistory.legacy_source == POD_HISTORY_SOURCE, PodProductCodeHistory.legacy_id == legacy_id))
            if not item:
                item = PodProductCodeHistory(legacy_source=POD_HISTORY_SOURCE, legacy_id=legacy_id, version_id=version.id, product_code=str(raw["product_code"] or ""))
                session.add(item); result["codes"] += 1
            item.version_id = version.id; item.product_code = str(raw["product_code"] or ""); item.status = str(raw["status"] or "")
            item.first_seen_at = str(raw["first_seen_at"] or ""); item.last_seen_at = str(raw["last_seen_at"] or "")
        for raw in connection.execute("select * from product_color_images order by created_at,id"):
            version = version_map.get(str(raw["version_id"]))
            if not version: continue
            legacy_id = str(raw["id"])
            item = session.scalar(select(PodColorImageGroup).where(PodColorImageGroup.legacy_source == POD_HISTORY_SOURCE, PodColorImageGroup.legacy_id == legacy_id))
            if not item:
                item = PodColorImageGroup(legacy_source=POD_HISTORY_SOURCE, legacy_id=legacy_id, version_id=version.id)
                session.add(item); result["color_groups"] += 1
            item.version_id = version.id; item.color_id = _safe_int(raw["color_id"]); item.color_name = str(raw["color_name"] or "")
            item.rendering_urls_json = str(raw["rendering_urls_json"] or "[]"); item.saved_files_json = str(raw["saved_files_json"] or "[]")
        for raw in connection.execute("select * from product_deletion_tombstones"):
            legacy_id = f"{raw['job_id']}:{raw['sku']}"
            item = session.scalar(select(PodDeletionTombstone).where(PodDeletionTombstone.legacy_source == POD_HISTORY_SOURCE, PodDeletionTombstone.legacy_id == legacy_id))
            if not item:
                item = PodDeletionTombstone(legacy_source=POD_HISTORY_SOURCE, legacy_id=legacy_id, sku=str(raw["sku"] or ""))
                session.add(item); result["tombstones"] += 1
            item.legacy_job_id = str(raw["job_id"] or ""); item.sku = str(raw["sku"] or ""); item.product_name = str(raw["product_name"] or ""); item.deleted_at = str(raw["deleted_at"] or "")
    session.commit()
    context.complete_step("product_history", details=result)
    return result


def _order_gallery_usage(orders_db: Path) -> dict[tuple[str, str], dict[str, Any]]:
    usage: dict[tuple[str, str], dict[str, Any]] = {}
    with _readonly_connection(orders_db) as connection:
        rows = connection.execute(
            "select id,sku,product_name,supplier,submission_status,fulfillment_gallery_codes,out_order_id,hicustom_order_id,tracking_number from batch_order_groups where fulfillment_gallery_codes not in ('','[]')"
        )
        for raw in rows:
            codes = loads(str(raw["fulfillment_gallery_codes"] or "[]"), [])
            for code in codes:
                key = (str(raw["supplier"] or "hicustom").lower(), str(code))
                entry = usage.setdefault(key, {"order_ids": [], "statuses": [], "protected": False, "sku": str(raw["sku"] or ""), "product_name": str(raw["product_name"] or "")})
                order_id = str(raw["out_order_id"] or raw["hicustom_order_id"] or raw["id"] or "")
                if order_id and order_id not in entry["order_ids"]: entry["order_ids"].append(order_id)
                status = str(raw["submission_status"] or "")
                if status and status not in entry["statuses"]: entry["statuses"].append(status)
                entry["protected"] = entry["protected"] or status in ACTIVE_ORDER_STATES or bool(raw["tracking_number"])
    return usage


def _upsert_compositions(context: JobContext, root: Path, artworks: dict[str, PodArtwork]) -> dict[str, int]:
    session = context.session
    jobs = _read_json(root / "data" / "jobs.json", [])
    if isinstance(jobs, dict): jobs = jobs.get("jobs") or []
    products = {row.sku: row for row in session.scalars(select(CatalogProduct).where(CatalogProduct.source_type == "pod")).all()}
    order_usage = _order_gallery_usage(root / "data" / "orders.db")
    state = _read_json(root / "data" / "supplier_gallery_state.json", {})
    deleted = state.get("remote_deleted") or {}
    result = {"compositions": 0, "sides": 0, "remote_gallery": 0}
    seen_gallery: dict[tuple[str, str], PodRemoteGalleryAsset] = {}
    for position, raw in enumerate(jobs, 1):
        context.refresh()
        legacy_id = str(raw.get("id") or "")
        item = session.scalar(select(PodComposition).where(PodComposition.legacy_source == POD_COMPOSITION_SOURCE, PodComposition.legacy_id == legacy_id))
        if not item:
            item = PodComposition(legacy_source=POD_COMPOSITION_SOURCE, legacy_id=legacy_id, supplier=str(raw.get("supplier") or "hicustom"), status=str(raw.get("status") or "completed"))
            session.add(item); result["compositions"] += 1
        supplier = str(raw.get("supplier") or "hicustom").lower()
        item.catalog_product_id = products.get(str(raw.get("library_sku") or "")).id if products.get(str(raw.get("library_sku") or "")) else None
        item.supplier = supplier; item.status = str(raw.get("status") or "completed"); item.stage_text = str(raw.get("stage_text") or "")
        item.product_name = str(raw.get("product_name") or ""); item.library_sku = str(raw.get("library_sku") or "")
        item.product_type_id = _safe_int(raw.get("product_type_id")); item.default_color_id = _safe_int(raw.get("default_color_id")); item.default_view_id = _safe_int(raw.get("default_view_id"))
        item.remote_product_id = str(raw.get("remote_product_id") or ""); item.product_code = str(raw.get("product_code") or ""); item.external_id = str(raw.get("external_id") or "")
        item.outcome_unknown = bool(raw.get("outcome_unknown")); item.error_message = str(raw.get("error") or "")
        item.selected_size_ids_json = dumps(raw.get("selected_size_ids") or []); item.rendering_groups_json = dumps(raw.get("rendering_groups") or [])
        item.oss_assets_json = dumps(raw.get("oss_assets") or []); item.config_snapshot_json = dumps({"supplier": supplier, "external_customer_id": raw.get("external_customer_id"), "legacy": True}); item.raw_json = dumps(raw)
        created = _timestamp(raw.get("created_at")); updated = _timestamp(raw.get("updated_at"))
        if created: item.created_at = created
        if updated: item.updated_at = updated
        session.flush()
        for side_index, side in enumerate(raw.get("sides") or [], 1):
            view_id = int(side.get("view_id") or side_index)
            side_legacy_id = f"{legacy_id}:{view_id}"
            target = session.scalar(select(PodCompositionSide).where(PodCompositionSide.legacy_source == POD_COMPOSITION_SOURCE, PodCompositionSide.legacy_id == side_legacy_id))
            if not target:
                target = PodCompositionSide(legacy_source=POD_COMPOSITION_SOURCE, legacy_id=side_legacy_id, composition_id=item.id, supplier=supplier, view_id=view_id)
                session.add(target); result["sides"] += 1
            placement = side.get("placement") or raw.get("placement") or {}
            artwork = artworks.get(str(side.get("artwork_id") or ""))
            target.composition_id = item.id; target.artwork_id = artwork.id if artwork else None; target.supplier = supplier
            target.view_id = view_id; target.view_name = str(side.get("view_name") or ""); target.gallery_code = str(side.get("gallery_code") or "")
            target.source_filename = str(side.get("source_filename") or ""); target.source_legacy_path = str(side.get("source_path") or ""); target.mime_type = str(side.get("mime_type") or "application/octet-stream")
            target.rotation = int(side.get("rotation") or 0); target.width = _safe_float(placement.get("width")); target.height = _safe_float(placement.get("height")); target.top_x = _safe_float(placement.get("top_x")); target.top_y = _safe_float(placement.get("top_y")); target.upload_status = "completed" if target.gallery_code else "missing"; target.raw_json = dumps(side)
            session.flush()
            if target.gallery_code:
                key = (supplier, target.gallery_code)
                gallery = session.scalar(select(PodRemoteGalleryAsset).where(PodRemoteGalleryAsset.supplier == supplier, PodRemoteGalleryAsset.gallery_code == target.gallery_code))
                if not gallery:
                    gallery = PodRemoteGalleryAsset(legacy_source=POD_COMPOSITION_SOURCE, legacy_id=f"{supplier}:{target.gallery_code}", supplier=supplier, gallery_code=target.gallery_code)
                    session.add(gallery); result["remote_gallery"] += 1
                gallery.artwork_id = target.artwork_id; gallery.composition_side_id = target.id; gallery.sku = item.library_sku; gallery.product_name = item.product_name; gallery.filename = target.source_filename
                seen_gallery[key] = gallery
        if position % 50 == 0:
            session.commit(); context.set_progress("compositions", position * 100 / max(1, len(jobs)), f"已导入 {position}/{len(jobs)} 个历史合成任务")
    session.flush()
    for key, order in order_usage.items():
        supplier, code = key
        gallery = seen_gallery.get(key) or session.scalar(select(PodRemoteGalleryAsset).where(PodRemoteGalleryAsset.supplier == supplier, PodRemoteGalleryAsset.gallery_code == code))
        if not gallery:
            gallery = PodRemoteGalleryAsset(legacy_source=POD_COMPOSITION_SOURCE, legacy_id=f"{supplier}:{code}", supplier=supplier, gallery_code=code)
            session.add(gallery); result["remote_gallery"] += 1
        gallery.usage = "ordered"; gallery.protected = bool(order["protected"]); gallery.cleanup_candidate = False
        gallery.sku = gallery.sku or order["sku"]; gallery.product_name = gallery.product_name or order["product_name"]
        gallery.order_ids_json = dumps(order["order_ids"]); gallery.order_statuses_json = dumps(order["statuses"])
        seen_gallery[key] = gallery
    for key, gallery in seen_gallery.items():
        if key not in order_usage:
            gallery.usage = "pending"; gallery.protected = False; gallery.cleanup_candidate = True
        deletion_key = f"{key[0]}:{key[1]}"
        gallery.remote_deleted_at = str(deleted.get(deletion_key) or ""); gallery.remote_deleted = bool(gallery.remote_deleted_at)
        gallery.raw_json = dumps({"legacy": True})
    session.commit()
    context.complete_step("compositions", details=result)
    return result


def _upsert_catalog_snapshot(session: Session, root: Path) -> dict[str, Any]:
    payload = _read_json(root / "data" / "hicustom_catalog.json", {})
    item = session.scalar(select(PodSupplierCatalogSnapshot).where(
        PodSupplierCatalogSnapshot.legacy_source == POD_HISTORY_SOURCE,
        PodSupplierCatalogSnapshot.legacy_id == "combined-current",
    ))
    if not item:
        item = PodSupplierCatalogSnapshot(legacy_source=POD_HISTORY_SOURCE, legacy_id="combined-current", provider="combined")
        session.add(item)
    item.is_current = True
    item.product_count = len(payload.get("products") or [])
    item.category_count = len(payload.get("categories") or [])
    item.fetched_at = str(payload.get("updated_at") or payload.get("synced_at") or "")
    item.payload_json = dumps(payload)
    session.commit()
    return {"products": item.product_count, "categories": item.category_count}


def ensure_pod_oss_connection(session: Session) -> IntegrationConnection | None:
    existing = session.scalar(select(IntegrationConnection).where(
        IntegrationConnection.legacy_source == POD_HISTORY_SOURCE,
        IntegrationConnection.legacy_id == "oss:hicustom-products",
    ))
    candidates = session.scalars(select(IntegrationConnection).where(
        IntegrationConnection.provider == "oss",
        IntegrationConnection.enabled.is_(True),
    ).order_by(IntegrationConnection.created_at)).all()
    source = next(
        (item for item in candidates if str(loads(item.config_json, {}).get("prefix") or "").strip("/") in {"hicustom-products", "hicustom-ozon"}),
        candidates[0] if candidates else None,
    )
    if not existing:
        if not source:
            return None
        existing = IntegrationConnection(
            provider="oss",
            name="POD 产品资产 OSS",
            base_url=source.base_url,
            config_json=source.config_json,
            secrets_encrypted=source.secrets_encrypted,
            enabled=True,
            status=source.status,
            legacy_source=POD_HISTORY_SOURCE,
            legacy_id="oss:hicustom-products",
        )
        session.add(existing)
    config = loads(source.config_json if source else existing.config_json, {})
    config["prefix"] = "hicustom-products"
    existing.config_json = dumps(config)
    if source and not existing.secrets_encrypted:
        existing.secrets_encrypted = source.secrets_encrypted
    session.commit()
    session.refresh(existing)
    return existing


def _normalize_product_asset_paths(session: Session, asset_root: Path) -> dict[str, int]:
    result = {"canonical_local": 0, "oss_only": 0}
    assets = session.scalars(select(Asset).where(Asset.legacy_source == "pod_product_library")).all()
    for asset in assets:
        suffix = Path(asset.name).suffix.casefold() or ".bin"
        canonical = asset_root / "artworks" / asset.sha256[:2] / f"{asset.sha256}{suffix}" if len(asset.sha256) == 64 else None
        if canonical and canonical.is_file():
            asset.local_path = str(canonical); asset.source = "pod_artwork"; result["canonical_local"] += 1
        elif asset.oss_url:
            asset.local_path = ""; asset.source = "pod_oss"; result["oss_only"] += 1
        metadata = loads(asset.metadata_json, {})
        metadata["local_state"] = "available" if asset.local_path else "oss_only"
        asset.metadata_json = dumps(metadata)
    session.commit()
    return result


@job_handler("pod_native_migration")
def pod_native_migration_job(context: JobContext) -> None:
    root = Path(str(context.payload.get("source_root") or "")).resolve()
    asset_root = Path(str(context.payload.get("asset_root") or "")).resolve()
    run = context.session.get(CatalogImportRun, str(context.payload.get("import_run_id") or ""))
    if not run:
        raise NotFoundError("POD 迁移记录不存在")
    run.status = "running"; context.session.commit()
    try:
        context.start_step("inspect")
        report = inspect_legacy_pod(root)
        run.report_json = dumps(report); run.counts_json = dumps(report.get("product_counts") or {})
        context.session.commit(); context.complete_step("inspect", details=report)
        if run.dry_run:
            run.status = "completed"; context.session.commit(); return

        import_pod_database(context, root / "data" / "product_library.db", run)
        run.status = "running"; context.session.commit()
        context.start_step("artwork_assets")
        artwork_payload = _read_json(root / "data" / "artwork_library.json", {})
        artwork_result = _upsert_artworks(context, root, asset_root, artwork_payload)
        context.start_step("product_history")
        history_result = _upsert_product_history(context, root / "data" / "product_library.db")
        context.start_step("compositions")
        composition_result = _upsert_compositions(context, root, artwork_result["artworks_by_legacy_id"])
        context.start_step("catalog_snapshot")
        catalog_result = _upsert_catalog_snapshot(context.session, root)
        pod_oss = ensure_pod_oss_connection(context.session)
        if pod_oss:
            catalog_result["pod_oss_connection_id"] = pod_oss.id
            catalog_result["pod_oss_connection_status"] = "ready"
        else:
            catalog_result["pod_oss_connection_status"] = "not_configured"
        context.complete_step("catalog_snapshot", details=catalog_result)
        context.start_step("normalize_assets")
        normalized = _normalize_product_asset_paths(context.session, asset_root)
        context.complete_step("normalize_assets", details=normalized)
        report.update({
            "native_import": {
                "artworks": artwork_result["artwork_count"], "unassigned": artwork_result["unassigned_count"],
                **history_result, **composition_result, "catalog": catalog_result, "asset_paths": normalized,
            },
            "source_modified": False,
        })
        run.report_json = dumps(report); run.status = "completed"; context.session.commit()
    except Exception as error:
        run.status = "failed"; run.errors_json = dumps([{"message": str(error), "type": type(error).__name__}]); context.session.commit()
        raise
