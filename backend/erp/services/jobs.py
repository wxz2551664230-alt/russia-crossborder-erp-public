from __future__ import annotations

import json
import hashlib
import socket
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from erp.core.config import get_settings
from erp.core.errors import AppError, ConflictError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.models import Job, JobArtifact, JobEvent, JobStep, WorkerNode, utc_now
from erp.core.task_identity import task_identity


TERMINAL_STATUSES = {"completed", "failed", "cancelled", "action_required"}
ACTIVE_STATUSES = {"queued", "running", "paused", "retrying"}
COMPLETED_JOB_RETENTION_DAYS = 30
COMPLETED_JOB_KEEP_LATEST = 500
_last_cleanup_monotonic = 0.0

# Queues are deliberately disjoint.  Slow external calls in one product area
# must not head-of-line block unrelated work, while the Ozon write queue stays
# single-consumer to preserve submission ordering.
JOB_QUEUE_TYPES: dict[str, frozenset[str]] = {
    "ai": frozenset({"ozon_ai_prepare", "ozon_image_review_action", "catalog_publication_image_action", "catalog_ozon_prepare", "pod_ai_design_plan", "pod_ai_design_generate", "pod_ai_design_upscale"}),
    "ozon": frozenset({"ozon_listing_pipeline", "catalog_ozon_publication", "catalog_ozon_followup", "catalog_ozon_options"}),
    "sourcing": frozenset({"sourcing_search"}),
    "orders": frozenset({"pod_order_sync", "pod_order_image_sync", "pod_purchase_submit", "pod_purchase_sync", "pod_tms_submit"}),
    "pod": frozenset({
        "pod_artwork_upload", "pod_composition", "pod_native_composition", "pod_us_composition",
    }),
    "wb": frozenset({"pod_wb_excel_export", "pod_wb_api_publish"}),
    "wb_operations": frozenset({"pod_wb_history_reconcile", "pod_wb_stock", "pod_wb_price"}),
    "pod_sync": frozenset({
        "pod_supplier_catalog_sync", "pod_supplier_us_catalog_sync","pod_supplier_us_freight_quote", "pod_native_migration",
        "pod_library_restore", "pod_supplier_gallery_sync", "pod_supplier_gallery_cleanup",
    }),
    "ads": frozenset({
        "ad_optimization_sync", "ad_optimization_analyze",
          "ad_optimization_backtest", "ad_optimization_reports", "ad_optimization_preview", "ad_optimization_execute", "ad_optimization_reconcile",
        "ad_optimization_import",
    }),
    "general": frozenset({
        "legacy_migration", "asset_index", "seerfar_import",
        "catalog_pod_import", "catalog_self_selected_import",
        "store_connection_test",
    }),
    "self_listing": frozenset({"self_listing_pipeline","amazon_metadata","amazon_prepare","amazon_prepare_codex","amazon_validate","amazon_submit","amazon_reconcile"}),
}

LOCAL_WORKER_QUEUES = ("ai",) * 5 + tuple(name for name in JOB_QUEUE_TYPES if name != "ai")


def job_types_for_queue(queue_name: str) -> frozenset[str] | None:
    """Return the allow-list for a worker queue; `all` keeps CLI compatibility."""
    normalized = str(queue_name or "all").strip().casefold()
    if normalized == "all":
        return None
    if normalized not in JOB_QUEUE_TYPES:
        raise ValueError(f"未知任务队列：{queue_name}")
    return JOB_QUEUE_TYPES[normalized]


def cleanup_completed_jobs(
    session: Session,
    *,
    retention_days: int = COMPLETED_JOB_RETENTION_DAYS,
    keep_latest: int = COMPLETED_JOB_KEEP_LATEST,
) -> int:
    """Remove old completed run history without deleting business data or assets."""
    cutoff = utc_now() - timedelta(days=max(1, int(retention_days)))
    protected = select(Job.id).where(Job.status == "completed").order_by(Job.finished_at.desc(), Job.created_at.desc()).limit(max(0, int(keep_latest)))
    ids = list(session.scalars(
        select(Job.id).where(
            Job.status == "completed",
            Job.finished_at.is_not(None),
            Job.finished_at < cutoff,
            Job.id.not_in(protected),
        ).limit(1000)
    ))
    if not ids:
        return 0
    # Output assets and business ledgers are intentionally retained. Only the
    # task-center association, steps, and events are removed.
    session.execute(delete(JobEvent).where(JobEvent.job_id.in_(ids)))
    session.execute(delete(JobStep).where(JobStep.job_id.in_(ids)))
    session.execute(delete(JobArtifact).where(JobArtifact.job_id.in_(ids)))
    session.execute(delete(Job).where(Job.id.in_(ids)))
    session.commit()
    return len(ids)


def _periodic_cleanup(session: Session) -> None:
    global _last_cleanup_monotonic
    now = time.monotonic()
    if now - _last_cleanup_monotonic < 6 * 60 * 60:
        return
    cleanup_completed_jobs(session)
    _last_cleanup_monotonic = now


def get_job(session: Session, job_id: str) -> Job:
    job = session.scalar(
        select(Job)
        .where(Job.id == job_id)
        .options(selectinload(Job.steps), selectinload(Job.events), selectinload(Job.artifacts))
    )
    if not job:
        raise NotFoundError("任务不存在")
    return job


def enqueue_job(
    session: Session,
    *,
    job_type: str,
    payload: dict[str, Any],
    config_snapshot: str,
    idempotency_key: str,
    priority: int = 100,
    max_attempts: int = 3,
    steps: list[tuple[str, str]] | None = None,
    commit: bool = True,
) -> Job:
    actor_id, target_worker_id = "", ""
    if get_settings().multiuser_enabled:
        actor = task_identity.get() or {}
        actor_id, target_worker_id = actor.get("id", ""), actor.get("worker_id", "")
        if not actor_id or not target_worker_id:
            raise ConflictError("请先在“我的执行电脑”中选择当前电脑，再提交任务")
        device = session.get(WorkerNode, target_worker_id)
        if not device or device.owner_user_id != actor_id:
            raise ConflictError("当前执行电脑未绑定本人账号，请重新选择")
        # Prevent reassignment from racing with enqueue's ownership check.
        session.execute(update(WorkerNode).where(WorkerNode.id == target_worker_id).values(status=WorkerNode.status))
        session.refresh(device)
        if device.owner_user_id != actor_id:
            raise ConflictError("电脑归属已变化，请重新选择")
        if job_type not in {"self_listing_pipeline", "catalog_self_selected_import", "catalog_pod_import", "worker_probe", "image_prepare", "pod_library_restore", "pod_native_composition", "pod_us_composition", "pod_artwork_upload", "pod_ai_design_plan", "pod_ai_design_generate", "pod_ai_design_upscale", "pod_wb_excel_export", "pod_wb_api_publish", "pod_wb_excel_reconcile", "pod_wb_history_reconcile", "pod_wb_stock", "pod_wb_price", "pod_order_sync", "pod_order_image_sync", "pod_purchase_sync", "pod_purchase_submit", "pod_supplier_gallery_sync", "pod_supplier_catalog_sync", "pod_supplier_us_catalog_sync","pod_supplier_us_freight_quote", "amazon_metadata","amazon_prepare","amazon_prepare_codex","amazon_validate","amazon_submit","amazon_reconcile", "store_connection_test", "catalog_ozon_prepare", "catalog_ozon_publication", "catalog_ozon_followup", "catalog_ozon_options", "ad_optimization_analyze", "ad_optimization_backtest", "ad_optimization_sync", "ad_optimization_reports", "ad_optimization_preview", "ad_optimization_execute", "ad_optimization_reconcile"} or job_type not in loads(device.capabilities_json, []):
            raise ConflictError("此业务尚未接通本地执行端，本轮不会转到服务器执行")
        idempotency_key = "assigned:" + hashlib.sha256(f"{actor_id}:{target_worker_id}:{idempotency_key}".encode()).hexdigest()
    existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
    if existing:
        return existing
    job = Job(
        type=job_type,
        created_by=actor_id,
        target_worker_id=target_worker_id,
        payload_json=dumps(payload),
        config_snapshot_json=config_snapshot,
        idempotency_key=idempotency_key,
        priority=priority,
        max_attempts=max_attempts,
    )
    session.add(job)
    session.flush()
    for position, (key, name) in enumerate(steps or [(job_type, job_type)], 1):
        session.add(JobStep(job_id=job.id, key=key, name=name, position=position))
    session.add(JobEvent(job_id=job.id, message="任务已进入队列"))
    if commit:session.commit()
    else:session.flush()
    return get_job(session, job.id)


def _controllable_job(session: Session, job_id: str) -> Job:
    if not get_settings().multiuser_enabled:
        return get_job(session, job_id)
    actor = task_identity.get() or {}
    if actor.get('role') not in {'operator', 'admin'}:
        raise AppError('需要操作账号', code='job_control_forbidden', status_code=403)
    # Serialize controls with claims and completion; ownership cannot be inferred
    # from the shared task list or from the browser's current device selection.
    job = session.scalar(select(Job).where(Job.id == job_id).with_for_update().execution_options(populate_existing=True))
    if not job or job.created_by != actor.get('id') or not job.target_worker_id:
        raise AppError('只能控制本人创建的电脑任务', code='job_control_forbidden', status_code=403)
    if job.type not in {'worker_probe', 'pod_library_restore', 'pod_native_composition','pod_us_composition', 'pod_artwork_upload','pod_ai_design_plan','pod_ai_design_generate','pod_ai_design_upscale','pod_wb_excel_export','pod_wb_api_publish','pod_wb_excel_reconcile','pod_wb_history_reconcile','pod_wb_stock','pod_wb_price','pod_order_sync','pod_order_image_sync','pod_purchase_sync','pod_purchase_submit','pod_supplier_gallery_sync','pod_supplier_catalog_sync','pod_supplier_us_catalog_sync','pod_supplier_us_freight_quote','amazon_metadata','amazon_prepare','amazon_prepare_codex','amazon_validate','amazon_submit','amazon_reconcile','self_listing_pipeline','catalog_self_selected_import','catalog_pod_import','catalog_ozon_prepare','catalog_ozon_publication','catalog_ozon_followup','catalog_ozon_options','ad_optimization_analyze','ad_optimization_backtest','ad_optimization_sync','ad_optimization_reports','ad_optimization_preview','ad_optimization_execute','ad_optimization_reconcile'}:
        raise ConflictError('此类任务的多人控制尚未接通')
    device = session.get(WorkerNode, job.target_worker_id)
    if not device or device.owner_user_id != actor['id']:
        raise ConflictError('原执行电脑归属已变化，不能继续任务')
    return job


def _check_composition_outcome(session: Session, job: Job) -> None:
    if loads(job.payload_json, {}).get('wb_shop_sync_released_by'):
        raise ConflictError('原库存或价格任务已由强制刷新释放，请在刷新后的商品页面重新提交')
    if loads(job.payload_json, {}).get('amazon_batch_removed'):
        raise ConflictError('Amazon 批次已删除，原任务不能恢复或重试，请重新建立批次')
    if job.type=='pod_purchase_submit' and job.error_code=='purchase_write_outcome_unknown':
        from erp.domains.orders.service import manager
        value=loads(job.payload_json,{})
        orders=manager(session)
        target=(orders.get_batch_group(value['target_id']) if value['target_type']=='group' else orders.get_order(value['target_id']))
        if target.get('submission_status')!='submitted' or not target.get('hicustom_order_id'):
            raise ConflictError('采购写入结果未知，请先同步核对原供应商订单，禁止重复下单')
    if job.error_code=='ai_write_outcome_unknown' and job.type!='catalog_ozon_prepare':raise ConflictError('AI调用结果未知，请核对后再恢复，禁止重复计费请求')
    if job.type=='pod_wb_api_publish' and job.error_code=='wb_cards_outcome_unknown':
        raise ConflictError('WB 建品结果尚未确认，请先核对原任务与平台商品卡，禁止重复提交')
    if get_settings().multiuser_enabled and job.type in {'pod_native_composition','pod_us_composition'}:
        from erp.domains.pod.models import PodComposition
        item=session.get(PodComposition,loads(job.payload_json,{}).get('composition_id'))
        if not item or item.outcome_unknown:
            raise ConflictError('供应商写入结果待核对，禁止直接重试建品')


def request_pause(session: Session, job_id: str) -> Job:
    job = _controllable_job(session, job_id)
    if job.status in TERMINAL_STATUSES:
        raise ConflictError("已结束的任务不能暂停")
    job.pause_requested = True
    if job.status in {"queued", "retrying"}:
        job.status = "paused"
    session.add(JobEvent(job_id=job.id, message="已请求暂停"))
    session.commit()
    return get_job(session, job_id)


def request_resume(session: Session, job_id: str) -> Job:
    job = _controllable_job(session, job_id)
    _check_composition_outcome(session, job)
    if job.status != "paused":
        raise ConflictError("只有已暂停的任务可以继续")
    job.pause_requested = False
    job.status = "queued"
    session.add(JobEvent(job_id=job.id, message="任务已继续"))
    session.commit()
    return get_job(session, job_id)


def request_cancel(session: Session, job_id: str) -> Job:
    job = _controllable_job(session, job_id)
    if job.status in TERMINAL_STATUSES:
        return job
    job.cancel_requested = True
    if job.status in {"queued", "paused", "retrying"}:
        job.status = "cancelled"
        job.finished_at = utc_now()
        if job.type == 'pod_wb_price':
            from erp.domains.pod.wb_price_state import release_interrupted
            release_interrupted(session, job)
        if job.type=='pod_purchase_submit':
            from erp.domains.orders.desktop_purchase_submit import release_unwritten
            release_unwritten(session,job)
    session.add(JobEvent(job_id=job.id, level="warning", message="已请求停止"))
    session.commit()
    return get_job(session, job_id)


def retry_job(session: Session, job_id: str) -> Job:
    job = _controllable_job(session, job_id)
    _check_composition_outcome(session, job)
    if job.type in {"pod_wb_api_publish", "pod_wb_excel_export"}:
        session.execute(update(Job).where(Job.id == job.id).values(status=Job.status))
        session.refresh(job)
    if loads(job.payload_json, {}).get("wb_queue_removed"):
        raise ConflictError("该 WB 任务已有商品从队列移除，请重新创建上架任务")
    if job.status not in ({"failed", "cancelled", "action_required"} if job.type == 'pod_wb_price' else {"failed", "cancelled"}):
        raise ConflictError("只有失败或已停止的任务可以重试")
    if job.type == 'pod_purchase_submit':
        # Procurement has no automatic retry (max_attempts=1). A deliberate
        # retry of this same fenced job must grant exactly one new attempt.
        job.max_attempts = max(job.max_attempts, job.attempts + 1)
    if job.type in {'pod_wb_stock', 'pod_wb_price'} or job.type.startswith('amazon_'):
        # Only a deliberate user retry grants another attempt; WB rate limits
        # must never keep consuming attempts automatically.
        job.max_attempts = max(job.max_attempts, job.attempts + 1)
    if job.type == 'pod_wb_price':
        from erp.domains.pod.wb_price_recovery import prepare_manual_retry
        prepare_manual_retry(session, job)
    job.status = "queued"
    job.cancel_requested = False
    job.pause_requested = False
    job.error_code = ""
    job.error_message = ""
    job.finished_at = None
    for step in job.steps:
        if step.status != "completed":
            step.status = "pending"
            step.error_message = ""
            step.finished_at = None
    session.add(JobEvent(job_id=job.id, message="任务已重新进入队列"))
    session.commit()
    return get_job(session, job_id)


class JobCancelled(RuntimeError):
    pass


class JobPaused(RuntimeError):
    pass


class JobActionRequired(RuntimeError):
    """The automatic phase ended safely and a later explicit action is required."""

    pass


@dataclass
class JobContext:
    session: Session
    job: Job

    @property
    def payload(self) -> dict[str, Any]:
        return loads(self.job.payload_json, {})

    def refresh(self) -> None:
        self.session.refresh(self.job)
        if self.job.cancel_requested:
            raise JobCancelled()
        if self.job.pause_requested:
            raise JobPaused()

    def event(self, message: str, *, level: str = "info", data: dict[str, Any] | None = None) -> None:
        self.session.add(JobEvent(job_id=self.job.id, level=level, message=message, data_json=dumps(data or {})))
        self.job.heartbeat_at = utc_now()
        self.job.lease_expires_at = utc_now() + timedelta(seconds=get_settings().worker_lease_seconds)
        self.session.commit()

    def start_step(self, key: str) -> JobStep:
        self.refresh()
        step = next((item for item in self.job.steps if item.key == key), None)
        if not step:
            step = JobStep(job_id=self.job.id, key=key, name=key, position=len(self.job.steps) + 1)
            self.session.add(step)
            self.session.flush()
            self.job.steps.append(step)
        if step.status == "completed":
            return step
        step.status = "running"
        step.started_at = step.started_at or utc_now()
        step.attempts += 1
        self.session.commit()
        return step

    def complete_step(self, key: str, *, details: dict[str, Any] | None = None) -> None:
        step = self.start_step(key)
        step.status = "completed"
        step.progress = 100
        step.details_json = dumps(details or {})
        step.error_message = ""
        step.finished_at = utc_now()
        completed = sum(1 for item in self.job.steps if item.status == "completed")
        self.job.progress = round(completed * 100 / max(1, len(self.job.steps)), 2)
        self.session.commit()

    def set_progress(self, key: str, progress: float, message: str = "") -> None:
        step = self.start_step(key)
        step.progress = max(0, min(100, float(progress)))
        overall = ((step.position - 1) + step.progress / 100) * 100 / max(1, len(self.job.steps))
        self.job.progress = round(max(self.job.progress, overall), 2)
        if message:
            self.session.add(JobEvent(job_id=self.job.id, message=message))
        self.job.heartbeat_at = utc_now()
        self.job.lease_expires_at = utc_now() + timedelta(seconds=get_settings().worker_lease_seconds)
        self.session.commit()


JobHandler = Callable[[JobContext], None]
HANDLERS: dict[str, JobHandler] = {}


def job_handler(job_type: str) -> Callable[[JobHandler], JobHandler]:
    def register(handler: JobHandler) -> JobHandler:
        HANDLERS[job_type] = handler
        return handler
    return register


def acquire_job(
    session: Session,
    owner: str | None = None,
    *,
    allowed_types: frozenset[str] | set[str] | None = None,
    target_worker_id: str = "",
    require_assignment: bool = False,
    assigned_user_id: str = "",
) -> Job | None:
    if not require_assignment:
        _periodic_cleanup(session)
    now = utc_now()
    owner = owner or f"{socket.gethostname()}:{id(session)}"
    assignment = (Job.target_worker_id == target_worker_id) if require_assignment or not target_worker_id else Job.target_worker_id.in_(["", target_worker_id])
    if require_assignment:
        assignment = and_(assignment, Job.created_by == assigned_user_id)
    expired_status = case(
        (Job.cancel_requested.is_(True), "cancelled"),
        (Job.pause_requested.is_(True), "paused"),
        (Job.attempts < Job.max_attempts, "retrying"),
        else_="failed",
    )
    # Check expiry in the UPDATE itself: a concurrent heartbeat or claimant
    # must not be overwritten using an earlier, stale ORM snapshot.
    expired_ids = list(session.scalars(select(Job.id).where(
        Job.status == "running", Job.lease_expires_at < now, assignment,
    ).limit(100).with_for_update(skip_locked=True)))
    stale_ids = list(session.scalars(
        update(Job).where(Job.id.in_(expired_ids), Job.status == "running", Job.lease_expires_at < now)
        .values(
            status=expired_status, lease_owner="", lease_expires_at=None,
            heartbeat_at=now,
            finished_at=case((expired_status.in_(["cancelled", "failed"]), now), else_=None),
            error_code="worker_lease_expired", error_message="工作进程中断，任务租约已过期",
        ).returning(Job.id)
    )) if expired_ids else []
    for job_id in stale_ids:
        stale = session.get(Job, job_id)
        if stale and stale.type == 'pod_wb_price':
            session.refresh(stale)
            from erp.domains.pod.wb_price_recovery import interrupted
            if stale.status in {'failed', 'retrying'} and interrupted(session, stale):
                from erp.domains.pod.wb_price_state import release_interrupted
                release_interrupted(session, stale)
                continue
            from erp.domains.pod.wb_price_state import release_interrupted
            release_interrupted(session, stale)
        running_steps = session.scalars(
            select(JobStep).where(JobStep.job_id == job_id, JobStep.status == "running")
        ).all()
        for step in running_steps:
            step.status = "failed"
            step.error_message = "工作进程中断，任务租约已过期"
            step.finished_at = now
        session.add(JobEvent(job_id=job_id, level="warning", message="租约到期已释放执行位置，并保留停止或暂停指令"))
    session.commit()
    normal_backoff = or_(*(
        and_(Job.attempts == attempt, Job.heartbeat_at <= now - timedelta(seconds=min(30, 2 ** attempt)))
        for attempt in range(1, 5)
    ), and_(Job.attempts >= 5, Job.heartbeat_at <= now - timedelta(seconds=30)))
    conditions = [
        assignment,
        Job.status.in_(["queued", "retrying"]),
        Job.pause_requested.is_(False),
        Job.cancel_requested.is_(False),
        # heartbeat_at records the failed attempt's end, shared by every
        # consumer. Backoff excludes only this job, never sleeps a worker.
        or_(Job.status == "queued", Job.heartbeat_at.is_(None), and_(
            Job.type == 'pod_wb_price', Job.error_code == 'wb_price_recovery_queued',
            Job.heartbeat_at <= now - timedelta(seconds=60)), and_(
            Job.error_code != 'wb_price_recovery_queued', normal_backoff)),
    ]
    if allowed_types is not None:
        if not allowed_types:
            return None
        conditions.append(Job.type.in_(allowed_types))
    candidate = (
        select(Job.id).where(*conditions)
        .order_by(Job.priority.asc(), case(
            (Job.status == "retrying", func.coalesce(Job.heartbeat_at, Job.created_at)),
            else_=Job.created_at,
        ).asc(), Job.id.asc()).limit(1)
        # A control/result transaction must not hold up polling consumers.
        # PostgreSQL skips locked candidates; SQLite ignores this clause.
        .with_for_update(skip_locked=True).scalar_subquery()
    )
    # Claim with a single conditional UPDATE. This prevents two workers from
    # selecting the same queued row before either one commits its lease.
    claimed_id = session.scalar(
        update(Job)
        .where(Job.id == candidate, *conditions)
        .values(
            status="running", lease_owner=owner,
            lease_expires_at=now + timedelta(seconds=get_settings().worker_lease_seconds),
            heartbeat_at=now, started_at=func.coalesce(Job.started_at, now),
            attempts=Job.attempts + 1,
        )
        .returning(Job.id)
    )
    if not claimed_id:
        session.rollback()
        return None
    session.commit()
    job = session.scalar(
        select(Job).where(Job.id == claimed_id).options(selectinload(Job.steps))
    )
    if not job:
        return None
    session.add(JobEvent(job_id=job.id, message=f"工作进程开始执行（第 {job.attempts} 次）"))
    session.commit()
    return job


def execute_job(session: Session, job: Job) -> None:
    if get_settings().runtime_mode == "server":
        raise AppError("服务器不执行业务任务，请由使用者电脑的执行端处理", code="server_execution_forbidden", status_code=409)
    handler = HANDLERS.get(job.type)
    if not handler:
        job.status = "failed"
        job.error_code = "unknown_job_type"
        job.error_message = f"未注册任务类型：{job.type}"
        job.finished_at = utc_now()
        job.lease_owner = ""
        job.lease_expires_at = None
        session.commit()
        return
    context = JobContext(session, job)
    try:
        from erp.core.ai_usage import task_scope
        with task_scope(job.id, job.type, job.attempts):
            handler(context)
        context.refresh()
    except JobPaused:
        job.status = "paused"
        session.add(JobEvent(job_id=job.id, message="任务已暂停"))
    except JobCancelled:
        job.status = "cancelled"
        job.finished_at = utc_now()
        session.add(JobEvent(job_id=job.id, level="warning", message="任务已停止"))
    except JobActionRequired as notice:
        job.status = "action_required"
        job.finished_at = utc_now()
        job.error_code = ""
        job.error_message = ""
        session.add(JobEvent(job_id=job.id, level="info", message=str(notice) or "自动阶段已完成，等待后续操作"))
    except Exception as error:
        job.status = "retrying" if job.attempts < job.max_attempts and getattr(error,"code","") != "wb_copy_not_distinct" else "failed"
        job.error_code = getattr(error, "code", error.__class__.__name__)
        job.error_message = str(error)[:4000]
        if job.status == "failed":
            job.finished_at = utc_now()
        running = next((step for step in job.steps if step.status == "running"), None)
        if running:
            running.status = "failed"
            running.error_message = str(error)[:4000]
            running.finished_at = utc_now()
        session.add(JobEvent(
            job_id=job.id,
            level="error",
            message=job.error_message or "任务失败",
            data_json=dumps({"traceback": traceback.format_exc(limit=8)}),
        ))
    else:
        job.status = "completed"
        job.progress = 100
        job.error_code = ""
        job.error_message = ""
        job.finished_at = utc_now()
        session.add(JobEvent(job_id=job.id, message="任务已完成"))
    finally:
        job.heartbeat_at = utc_now()
        job.lease_owner = ""
        job.lease_expires_at = None
        session.commit()
