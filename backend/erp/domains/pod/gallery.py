from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from erp.core.errors import AppError, ConflictError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.domains.pod.integrations.common import load_supplier_connection
from erp.domains.pod.integrations.registry import supplier_client
from erp.domains.pod.models import PodRemoteGalleryAsset
from erp.serializers import job_dict
from erp.services.jobs import JobContext, enqueue_job, job_handler


def _conditions(*, usage: str, supplier: str, search: str, include_deleted: bool) -> list[Any]:
    conditions: list[Any] = []
    if usage:
        conditions.append(PodRemoteGalleryAsset.usage == usage)
    if supplier:
        conditions.append(PodRemoteGalleryAsset.supplier == supplier)
    if not include_deleted:
        conditions.append(PodRemoteGalleryAsset.remote_deleted.is_(False))
    if search.strip():
        token = f"%{search.strip()}%"
        conditions.append(or_(
            PodRemoteGalleryAsset.gallery_code.ilike(token),
            PodRemoteGalleryAsset.filename.ilike(token),
            PodRemoteGalleryAsset.sku.ilike(token),
            PodRemoteGalleryAsset.product_name.ilike(token),
            PodRemoteGalleryAsset.order_ids_json.ilike(token),
        ))
    return conditions


def gallery_dict(item: PodRemoteGalleryAsset) -> dict[str, Any]:
    order_ids = loads(item.order_ids_json, [])
    order_statuses = loads(item.order_statuses_json, [])
    return {
        "id": item.id,
        "key": f"{item.supplier}:{item.gallery_code}",
        "supplier": item.supplier,
        "supplier_name": "指纹科技" if item.supplier == "hicustom" else "S2B",
        "code": item.gallery_code,
        "usage": item.usage,
        "usage_name": "已出单" if item.usage == "ordered" else "待出单",
        "protected": item.protected,
        "cleanup_candidate": item.cleanup_candidate,
        "remote_deleted": item.remote_deleted,
        "remote_deleted_at": item.remote_deleted_at,
        "sku": item.sku,
        "product_name": item.product_name,
        "filename": item.filename,
        "order_ids": order_ids if isinstance(order_ids, list) else [],
        "order_statuses": order_statuses if isinstance(order_statuses, list) else [],
        "thumbnail_url": f"/api/v1/pod/artworks/{item.artwork_id}/thumbnail" if item.artwork_id else "",
        "created_at": item.created_at.isoformat(),
        "last_used_at": item.updated_at.isoformat(),
    }


def list_gallery(
    session: Session,
    *,
    usage: str,
    supplier: str,
    search: str,
    include_deleted: bool,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    conditions = _conditions(usage=usage, supplier=supplier, search=search, include_deleted=include_deleted)
    total = int(session.scalar(select(func.count()).select_from(PodRemoteGalleryAsset).where(*conditions)) or 0)
    items = session.scalars(
        select(PodRemoteGalleryAsset).where(*conditions)
        .order_by(PodRemoteGalleryAsset.updated_at.desc(), PodRemoteGalleryAsset.id)
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    visible = [] if include_deleted else [PodRemoteGalleryAsset.remote_deleted.is_(False)]
    counts = {
        "total": int(session.scalar(select(func.count()).select_from(PodRemoteGalleryAsset).where(*visible)) or 0),
        "pending": int(session.scalar(select(func.count()).select_from(PodRemoteGalleryAsset).where(*visible, PodRemoteGalleryAsset.usage == "pending")) or 0),
        "ordered": int(session.scalar(select(func.count()).select_from(PodRemoteGalleryAsset).where(*visible, PodRemoteGalleryAsset.usage == "ordered")) or 0),
        "cleanup_candidates": int(session.scalar(select(func.count()).select_from(PodRemoteGalleryAsset).where(
            *visible, PodRemoteGalleryAsset.cleanup_candidate.is_(True), PodRemoteGalleryAsset.protected.is_(False)
        )) or 0),
        "protected": int(session.scalar(select(func.count()).select_from(PodRemoteGalleryAsset).where(*visible, PodRemoteGalleryAsset.protected.is_(True))) or 0),
    }
    return {"items": [gallery_dict(item) for item in items], "total": total, "counts": counts}


def mark_ordered_gallery_codes(
    session: Session, *, supplier: str, gallery_codes: list[str], sku: str,
    product_name: str, order_id: str, order_status: str = "submitted", commit: bool = True,
) -> int:
    updated = 0
    for code in dict.fromkeys(str(value).strip() for value in gallery_codes if str(value).strip()):
        item = session.scalar(select(PodRemoteGalleryAsset).where(
            PodRemoteGalleryAsset.supplier == supplier, PodRemoteGalleryAsset.gallery_code == code,
        ))
        if not item:
            item = PodRemoteGalleryAsset(
                supplier=supplier, gallery_code=code, usage="ordered", protected=True,
                cleanup_candidate=False, sku=sku, product_name=product_name,
            )
            session.add(item)
        order_ids = list(loads(item.order_ids_json, []))
        statuses = list(loads(item.order_statuses_json, []))
        if order_id and order_id not in order_ids:
            order_ids.append(order_id)
        if order_status and order_status not in statuses:
            statuses.append(order_status)
        item.usage = "ordered"; item.protected = True; item.cleanup_candidate = False
        item.remote_deleted = False; item.remote_deleted_at = ""
        item.sku = sku or item.sku; item.product_name = product_name or item.product_name
        item.order_ids_json = dumps(order_ids); item.order_statuses_json = dumps(statuses)
        updated += 1
    if commit: session.commit()
    return updated


def enqueue_gallery_sync(session: Session, supplier: str = "") -> dict[str, Any]:
    if supplier and supplier not in {"hicustom", "s2b"}:
        raise AppError("供应商无效", code="pod_supplier_unsupported", status_code=422)
    providers = [supplier] if supplier else ["hicustom", "s2b"]
    connection_ids = {}
    for provider in providers:
        try: connection_ids[provider] = load_supplier_connection(session, provider).connection_id
        except AppError: pass
    targets = [{"id": row.id, "supplier": row.supplier, "gallery_code": row.gallery_code}
               for row in session.scalars(select(PodRemoteGalleryAsset).where(
                   PodRemoteGalleryAsset.remote_deleted.is_(False),
                   PodRemoteGalleryAsset.supplier.in_(providers)).order_by(PodRemoteGalleryAsset.id))]
    job = enqueue_job(
        session, job_type="pod_supplier_gallery_sync",
        payload={"suppliers": providers, "connection_ids": connection_ids, "targets": targets},
        config_snapshot=dumps({"suppliers": providers, "connection_ids": connection_ids, "read_only": True}),
        idempotency_key=f"pod_gallery_sync:{__import__('uuid').uuid4().hex}",
        steps=[("sync", "核对供应商图库素材")], max_attempts=3,
    )
    return {"job": job_dict(job)}


def accept_personal_gallery_sync(session: Session, job, result: dict[str, Any]) -> dict[str, int]:
    """Apply only frozen, read-only remote existence facts from the initiating PC."""
    targets = loads(job.payload_json, {}).get("targets")
    if not isinstance(targets, list):
        raise ConflictError("供应商图库任务缺少冻结清单")
    expected = {row["id"]: row for row in targets}
    facts = result.get("facts")
    if (not isinstance(facts, list) or len(facts) != len(targets)
            or any(not isinstance(row, dict) or set(row) != {"id", "exists"}
                   or not isinstance(row["id"], str) for row in facts)
            or {row["id"] for row in facts} != set(expected)):
        raise ConflictError("供应商图库核对结果与任务清单不一致")
    checked = missing = skipped = 0
    for fact in facts:
        source = expected[fact["id"]]
        if source["supplier"] != "hicustom":
            if fact["exists"] is not None:
                raise ConflictError("S2B 不支持远端图库存在性查询")
            skipped += 1
            continue
        if type(fact["exists"]) is not bool:
            raise ConflictError("供应商图库存在性结果无效")
        item = session.get(PodRemoteGalleryAsset, fact["id"])
        if not item or item.supplier != source["supplier"] or item.gallery_code != source["gallery_code"]:
            raise ConflictError("供应商图库来源记录已变化")
        checked += 1
        if not fact["exists"] and not item.remote_deleted:
            item.remote_deleted = True
            item.remote_deleted_at = datetime.now(timezone.utc).isoformat()
            missing += 1
    return {"checked": checked, "missing": missing, "unsupported_skipped": skipped}


def enqueue_bulk_gallery_cleanup(session: Session, supplier: str = "") -> dict[str, Any]:
    conditions = [
        PodRemoteGalleryAsset.usage == "pending", PodRemoteGalleryAsset.protected.is_(False),
        PodRemoteGalleryAsset.cleanup_candidate.is_(True), PodRemoteGalleryAsset.remote_deleted.is_(False),
    ]
    if supplier: conditions.append(PodRemoteGalleryAsset.supplier == supplier)
    rows = session.scalars(select(PodRemoteGalleryAsset).where(*conditions)).all()
    queued = unsupported = 0
    job_ids = []
    for item in rows:
        if item.supplier != "hicustom":
            unsupported += 1; continue
        result = enqueue_gallery_cleanup(session, item.supplier, item.gallery_code)
        if result.get("job"):
            queued += 1; job_ids.append(result["job"]["id"])
    return {"queued": queued, "unsupported": unsupported, "job_ids": job_ids}


@job_handler("pod_supplier_gallery_sync")
def sync_supplier_gallery_job(context: JobContext) -> None:
    context.start_step("sync")
    rows = context.session.scalars(select(PodRemoteGalleryAsset).where(PodRemoteGalleryAsset.remote_deleted.is_(False))).all()
    suppliers = set(context.payload.get("suppliers") or [])
    checked = missing = skipped = 0
    for index, item in enumerate(rows, 1):
        if suppliers and item.supplier not in suppliers: continue
        if item.supplier != "hicustom":
            skipped += 1; continue
        with supplier_client(context.session, item.supplier, str((context.payload.get("connection_ids") or {}).get(item.supplier) or "")) as client:
            exists = client.gallery_exists(item.gallery_code)
        checked += 1
        if not exists:
            item.remote_deleted = True; item.remote_deleted_at = datetime.now(timezone.utc).isoformat(); missing += 1
        if index % 20 == 0:
            context.session.commit(); context.set_progress("sync", index * 100 / max(1, len(rows)), f"已核对 {index}/{len(rows)}")
    context.session.commit()
    context.complete_step("sync", details={"checked": checked, "missing": missing, "unsupported_skipped": skipped})


def get_gallery(session: Session, supplier: str, gallery_code: str) -> PodRemoteGalleryAsset:
    item = session.scalar(select(PodRemoteGalleryAsset).where(
        PodRemoteGalleryAsset.supplier == supplier,
        PodRemoteGalleryAsset.gallery_code == gallery_code,
    ))
    if not item:
        raise NotFoundError("供应商图库素材记录不存在")
    return item


def validate_cleanup(item: PodRemoteGalleryAsset) -> None:
    if item.usage != "pending" or item.protected or not item.cleanup_candidate:
        raise ConflictError("已出单或仍在生产链路中的素材禁止清理")


def mark_gallery_deleted(session: Session, supplier: str, gallery_code: str) -> dict[str, Any]:
    item = get_gallery(session, supplier, gallery_code)
    validate_cleanup(item)
    already_deleted = item.remote_deleted
    if not item.remote_deleted:
        item.remote_deleted = True
        item.remote_deleted_at = datetime.now(timezone.utc).isoformat()
        session.commit()
    return {"deleted": True, "already_deleted": already_deleted, "local_preserved": True}


def enqueue_gallery_cleanup(session: Session, supplier: str, gallery_code: str) -> dict[str, Any]:
    if supplier != "hicustom":
        raise ConflictError("S2B 没有远程删除接口，请在 S2B 后台删除后使用“标记已清理”")
    item = get_gallery(session, supplier, gallery_code)
    validate_cleanup(item)
    if item.remote_deleted:
        return {"deleted": True, "already_deleted": True, "local_preserved": True}
    connection = load_supplier_connection(session, supplier)
    job = enqueue_job(
        session,
        job_type="pod_supplier_gallery_cleanup",
        payload={"gallery_asset_id": item.id, "supplier": supplier, "gallery_code": gallery_code, "connection_id": connection.connection_id},
        config_snapshot=dumps({"supplier": supplier, "connection_id": connection.connection_id, "source_record_preserved": True}),
        idempotency_key=f"pod_gallery_cleanup:{item.id}",
        steps=[("validate", "检查素材用途与保护状态"), ("delete_remote", "清理供应商远程素材"), ("mark", "记录清理结果")],
        max_attempts=3,
    )
    job.external_ref = gallery_code
    session.commit()
    return {"job": job_dict(job), "local_preserved": True}


@job_handler("pod_supplier_gallery_cleanup")
def cleanup_supplier_gallery_job(context: JobContext) -> None:
    item = context.session.get(PodRemoteGalleryAsset, str(context.payload.get("gallery_asset_id") or ""))
    if not item:
        raise NotFoundError("供应商图库素材记录不存在")
    context.start_step("validate")
    validate_cleanup(item)
    context.complete_step("validate", details={"supplier": item.supplier, "gallery_code": item.gallery_code})
    context.start_step("delete_remote")
    if item.remote_deleted:
        context.complete_step("delete_remote", details={"already_deleted": True})
    else:
        if item.supplier != "hicustom":
            raise AppError("当前供应商不支持 API 删除素材", code="pod_gallery_delete_unsupported", status_code=409)
        with supplier_client(context.session, item.supplier, str(context.payload.get("connection_id") or "")) as client:
            existed = client.gallery_exists(item.gallery_code)
            if existed:
                client.delete_gallery(item.gallery_code)
        context.complete_step("delete_remote", details={"existed_before_delete": existed})
    context.start_step("mark")
    item.remote_deleted = True
    item.remote_deleted_at = datetime.now(timezone.utc).isoformat()
    context.session.commit()
    context.complete_step("mark", details={"local_preserved": True})
