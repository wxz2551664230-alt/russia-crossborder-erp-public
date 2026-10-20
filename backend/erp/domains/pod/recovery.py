from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from erp.core.errors import AppError, ConflictError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.domains.pod.models import PodComposition, PodCompositionSide, PodRemoteGalleryAsset
from erp.models import Job, JobEvent
from erp.services.jobs import get_job, retry_job


def retry_deleted_s2b_product(session: Session, composition_id: str, client: Any) -> str:
    """Rebuild one verified-missing supplier product using its original local design."""
    item = session.get(PodComposition, composition_id)
    if not item:
        raise NotFoundError("POD 合成记录不存在")
    job = get_job(session, item.job_id or "")
    if (item.supplier != "s2b" or job.type != "pod_native_composition" or job.status != "failed"
            or item.outcome_unknown or item.catalog_product_id or not item.remote_product_id or not item.product_code):
        raise ConflictError("当前合成记录不符合供应商缺失产品的安全恢复条件")
    raw = loads(item.raw_json, {})
    sides = list(raw.get("sides") or [])
    if not sides or any(not Path(str(side.get("source_path") or "")).is_file() for side in sides):
        raise ConflictError("本地印刷原图不完整，不能恢复")
    old_product_id = item.remote_product_id
    try:
        existing = client.custom_product(old_product_id)
    except AppError as error:
        if "No query results for model" not in str(error):
            raise
    else:
        if existing:
            raise ConflictError("供应商原产品仍存在，不能重新建品")
    old_gallery_codes = [str(side.get("gallery_code") or "") for side in sides]
    history = list(raw.get("deleted_remote_recovery_history") or [])
    history.append({"checked_at": datetime.now(timezone.utc).isoformat(),
                    "remote_product_id": old_product_id, "product_code": item.product_code,
                    "gallery_codes": old_gallery_codes})
    raw["deleted_remote_recovery_history"] = history
    for side in sides:
        old_code = str(side.get("gallery_code") or "")
        if old_code:
            gallery = session.scalar(select(PodRemoteGalleryAsset).where(
                PodRemoteGalleryAsset.supplier == "s2b", PodRemoteGalleryAsset.gallery_code == old_code))
            if gallery:
                gallery.remote_deleted = True
                gallery.remote_deleted_at = datetime.now(timezone.utc).isoformat()
                gallery.protected = False
                gallery.cleanup_candidate = False
        side["gallery_code"] = ""
        side_row = session.get(PodCompositionSide, str(side.get("side_id") or ""))
        if not side_row or side_row.composition_id != item.id:
            raise ConflictError("印刷面记录不完整，不能恢复")
        side_row.gallery_code = ""
        side_row.upload_status = "pending"
    raw["sides"] = sides
    item.raw_json = dumps(raw)
    item.remote_product_id = ""
    item.product_code = ""
    item.rendering_groups_json = "[]"
    item.status = "queued"
    item.stage_text = "供应商素材已删除，重新上传并建品"
    item.error_message = ""
    for step in job.steps:
        if step.key != "prepare":
            step.status = "pending"
            step.progress = 0
            step.error_message = ""
            step.finished_at = None
    job.progress = 0
    job.max_attempts = max(job.max_attempts, job.attempts + 3)
    session.add(JobEvent(job_id=job.id, message=f"已核实供应商原产品 {old_product_id} 不存在，开始恢复本地设计图"))
    retry_job(session, job.id)
    return job.id


def _resume_existing_product(session: Session, item: PodComposition) -> str:
    job = get_job(session, item.job_id or "")
    item.status = "queued"
    item.stage_text = "继续读取供应商原产品结果"
    item.error_message = ""
    job.max_attempts = max(job.max_attempts, job.attempts + 3)
    session.add(JobEvent(job_id=job.id, message="供应商原产品仍存在，继续原任务且不重新建品"))
    retry_job(session, job.id)
    return job.id


def retry_failed_composition(session: Session, composition_id: str, s2b_client: Any, hicustom_client: Any = None) -> str:
    """Resume only failed jobs whose local source and external write state are known."""
    item = session.get(PodComposition, composition_id)
    if not item:
        raise NotFoundError("POD 合成记录不存在")
    job = get_job(session, item.job_id or "")
    if job.type != "pod_native_composition" or job.status != "failed" or item.outcome_unknown or item.catalog_product_id:
        raise ConflictError("当前任务状态不允许安全重试")
    raw = loads(item.raw_json, {})
    sides = list(raw.get("sides") or [])
    if not sides or any(not Path(str(side.get("source_path") or "")).is_file() for side in sides):
        raise ConflictError("本地印刷原图不完整，不能重试")
    if item.remote_product_id:
        if item.supplier != "s2b":
            raise ConflictError("供应商旧产品需要人工核对后再恢复")
        try:
            existing = s2b_client.custom_product(item.remote_product_id)
        except AppError as error:
            if "No query results for model" not in str(error):
                raise
        else:
            if existing:
                return _resume_existing_product(session, item)
        return retry_deleted_s2b_product(session, composition_id, s2b_client)
    if item.product_code and item.supplier == "hicustom" and hicustom_client is not None:
        if hicustom_client.custom_product(item.product_code):
            return _resume_existing_product(session, item)
    if item.product_code or any(side.get("gallery_code") for side in sides):
        raise ConflictError("外部写入结果尚不明确，不能自动重试")
    create_step = next((step for step in job.steps if step.key == "create_product"), None)
    if create_step and create_step.status != "pending":
        raise ConflictError("建品步骤曾执行，需先核对供应商结果")
    if item.supplier not in {"s2b", "hicustom"}:
        raise ConflictError("供应商类型不支持自动重试")
    item.status = "queued"
    item.stage_text = "从本地原图重新合成"
    item.error_message = ""
    for step in job.steps:
        if step.key != "prepare":
            step.status = "pending"
            step.progress = 0
            step.error_message = ""
            step.finished_at = None
    job.progress = 0
    job.max_attempts = max(job.max_attempts, job.attempts + 3)
    session.add(JobEvent(job_id=job.id, message="已确认尚未建品，从本地原图重新执行"))
    retry_job(session, job.id)
    return job.id


def retry_all_failed_compositions(session: Session, *, limit: int = 200) -> dict[str, Any]:
    from erp.domains.pod.integrations.registry import supplier_client

    candidates = session.execute(
        select(PodComposition.id, Job.id, PodComposition.supplier)
        .join(Job, Job.id == PodComposition.job_id)
        .where(Job.type == "pod_native_composition", Job.status == "failed")
        .order_by(PodComposition.created_at.desc())
        .limit(limit)
    ).all()
    queued: list[str] = []
    errors: list[dict[str, str]] = []
    suppliers = {supplier for _composition_id, _job_id, supplier in candidates}
    with ExitStack() as stack:
        client = stack.enter_context(supplier_client(session, "s2b")) if "s2b" in suppliers else None
        hicustom_client = stack.enter_context(supplier_client(session, "hicustom")) if "hicustom" in suppliers else None
        for composition_id, job_id, _supplier in candidates:
            try:
                queued.append(retry_failed_composition(session, composition_id, client, hicustom_client))
            except AppError as error:
                session.rollback()
                errors.append({"composition_id": composition_id, "job_id": job_id, "reason": str(error)})
    return {"total": len(candidates), "queued": len(queued), "job_ids": queued,
            "skipped": len(errors), "errors": errors}


def failed_composition_retry_preview(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(PodComposition, Job)
        .join(Job, Job.id == PodComposition.job_id)
        .where(Job.type == "pod_native_composition", Job.status == "failed")
    ).all()
    local_ready = 0
    hidden = 0
    remote_check = 0
    for item, _job in rows:
        sides = loads(item.raw_json, {}).get("sides") or []
        local_ready += bool(sides) and all(Path(str(side.get("source_path") or "")).is_file() for side in sides)
        hidden += item.status == "cleared"
        remote_check += bool(item.remote_product_id)
    return {"failed": len(rows), "local_ready": local_ready,
            "hidden_history": hidden, "remote_check_required": remote_check}
