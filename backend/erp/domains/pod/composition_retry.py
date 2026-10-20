"""Requeue existing desktop compositions without executing supplier work on the server."""
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from erp.core.errors import AppError, ConflictError
from erp.core.jsonutil import loads
from erp.core.task_identity import task_identity
from erp.domains.pod.models import PodComposition, PodCompositionSide
from erp.models import Asset, Job, WorkerNode
from erp.services.jobs import retry_job


TYPES = {"pod_native_composition", "pod_us_composition"}


def _scope(session: Session) -> dict:
    actor = task_identity.get() or {}
    if actor.get("role") not in {"admin", "operator"}:
        raise AppError("需要操作账号", code="job_control_forbidden", status_code=403)
    worker = session.get(WorkerNode, actor.get("worker_id", ""))
    if not actor.get("id") or not worker or worker.owner_user_id != actor["id"]:
        raise ConflictError("请先在我的执行电脑中选择本人当前电脑")
    return actor


def _candidates(session: Session, actor: dict):
    return session.execute(
        select(Job.id, PodComposition.id)
        .join(PodComposition, PodComposition.job_id == Job.id)
        .where(Job.type.in_(TYPES), Job.status == "failed",
               Job.created_by == actor["id"], Job.target_worker_id == actor["worker_id"])
        .order_by(Job.created_at, Job.id)
    ).all()


def _blocked(session: Session, job: Job, item: PodComposition) -> str:
    if loads(job.payload_json, {}).get("composition_id") != item.id:
        return "任务与合成记录不一致，请查看任务详情"
    if item.catalog_product_id or loads(item.raw_json, {}).get("desktop_receipt"):
        return "已有产品入库回执，请查看产品库核对"
    if item.outcome_unknown or job.error_code == "pod_write_outcome_unknown":
        return "供应商写入结果待核对，请查看原任务详情，核对后继续"
    sides = list(session.scalars(select(PodCompositionSide).where(PodCompositionSide.composition_id == item.id)))
    raw_sides = loads(item.raw_json, {}).get("sides") or []
    if (not sides or not isinstance(raw_sides, list) or len(sides) != len(raw_sides)
            or any(not isinstance(s, dict) for s in raw_sides)
            or {s.id for s in sides} != {s.get("side_id") for s in raw_sides}):
        return "合成原图记录不完整，请查看任务详情补齐素材"
    asset_ids = {s.asset_id for s in sides} | {s.get("asset_id") for s in raw_sides}
    available = set(session.scalars(select(Asset.id).where(Asset.id.in_(asset_ids), Asset.mirror_status != "deleted")))
    if not all(asset_ids) or available != asset_ids:
        return "合成原图记录不完整，请查看任务详情补齐素材"
    if (loads(item.raw_json, {}).get("market") == "US") != (job.type == "pod_us_composition"):
        return "任务与货盘国家不一致，请查看任务详情"
    return ""


def preview(session: Session) -> dict:
    actor = _scope(session)
    rows = _candidates(session, actor)
    reasons = Counter()
    hidden = 0
    for job_id, item_id in rows:
        job, item = session.get(Job, job_id), session.get(PodComposition, item_id)
        reason = _blocked(session, job, item)
        if reason:
            reasons[reason] += 1
        hidden += item.status == "cleared"
    return {"execution_mode": "original_pc", "failed": len(rows),
            "eligible": len(rows) - sum(reasons.values()), "hidden_history": hidden,
            "blocked": sum(reasons.values()), "reasons": dict(reasons),
            "target_worker_id": actor["worker_id"]}


def retry_failed(session: Session) -> dict:
    actor = _scope(session)
    rows = _candidates(session, actor)
    queued, errors = [], []
    # End the read transaction before taking the same Job -> composition locks
    # as the task controls. Each existing job is committed independently.
    session.rollback()
    for job_id, item_id in rows:
        try:
            job = session.scalar(select(Job).where(Job.id == job_id)
                                 .with_for_update().execution_options(populate_existing=True))
            if (not job or job.status != "failed" or job.type not in TYPES
                    or job.created_by != actor["id"] or job.target_worker_id != actor["worker_id"]):
                session.rollback()
                continue
            item = session.scalar(select(PodComposition).where(PodComposition.id == item_id)
                                  .with_for_update().execution_options(populate_existing=True))
            if not item or item.job_id != job.id:
                raise ConflictError("合成记录已变化，请刷新后重试")
            reason = _blocked(session, job, item)
            if reason:
                raise ConflictError(reason)
            # Retain supplier IDs, source IDs, completed steps, local journals,
            # raw checkpoints and original assignment. Never reset/rebuild here.
            item.status = "queued"
            item.stage_text = "等待原电脑从已有进度重试"
            item.error_message = ""
            job.max_attempts = max(job.max_attempts, job.attempts + 1)
            session.flush()
            retry_job(session, job.id)
            queued.append(job_id)
        except AppError as error:
            session.rollback()
            errors.append({"job_id": job_id, "composition_id": item_id, "message": str(error)})
    return {"execution_mode": "original_pc", "queued": len(queued), "skipped": len(errors),
            "job_ids": queued, "errors": errors,
            "reasons": dict(Counter(e["message"] for e in errors)),
            "target_worker_id": actor["worker_id"]}
