from __future__ import annotations

import hmac
import hashlib
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from erp.core.config import get_settings
from erp.core.database import get_db
from erp.core.errors import ConflictError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.models import Asset, CatalogProduct, Job, JobArtifact, JobEvent, WorkerNode, utc_now
from erp.schemas import WorkerClaim, WorkerHeartbeat, WorkerJobFailure, WorkerJobResult, WorkerRegistration
from erp.serializers import job_dict
from erp.services.jobs import acquire_job
from erp.services.catalog import link_catalog_asset
from erp.auth_models import ErpUser


router = APIRouter(prefix="/workers", tags=["workers"])
REMOTE_JOB_TYPES = frozenset({"worker_probe", "image_prepare"})


def require_worker_token(request: Request, authorization: str = Header(default="")) -> str | None:
    settings = get_settings()
    if settings.multiuser_enabled and settings.runtime_mode == "server" and not (settings.worker_token_hashes or settings.worker_require_device_identity):
        raise HTTPException(status_code=503, detail="多人服务器必须配置独立设备凭据")
    provided = authorization[7:] if authorization.startswith("Bearer ") else ""
    if settings.worker_token_hashes or settings.worker_require_device_identity:
        digest = hashlib.sha256(provided.encode()).hexdigest()
        identities = [identity for identity, expected in settings.worker_token_hashes.items()
                      if provided and hmac.compare_digest(digest, expected.lower())]
        if len(identities) != 1:
            raise HTTPException(status_code=401, detail="Worker 身份验证失败或设备已停用")
        identity = identities[0]
        if request.path_params.get("worker_id", identity) != identity:
            raise HTTPException(status_code=403, detail="Worker 凭据不属于当前设备")
        return identity
    configured = settings.worker_token
    if not configured:
        raise HTTPException(status_code=503, detail="Worker 通信尚未配置")
    if not provided or not hmac.compare_digest(provided, configured):
        raise HTTPException(status_code=401, detail="Worker 身份验证失败")
    return None


def _worker(session: Session, worker_id: str) -> WorkerNode:
    worker = session.get(WorkerNode, worker_id)
    if not worker:
        raise NotFoundError("Worker 尚未注册")
    return worker


@router.post("/register")
def register_worker(request: WorkerRegistration, session: Session = Depends(get_db), identity: str | None = Depends(require_worker_token)) -> dict:
    if identity is not None and identity != request.id:
        raise HTTPException(status_code=403, detail="Worker 凭据不属于注册设备")
    worker = session.get(WorkerNode, request.id) or WorkerNode(id=request.id)
    worker.name = request.name or request.id
    worker.status = "online"
    worker.capabilities_json = dumps(request.capabilities)
    worker.metadata_json = dumps(request.metadata)
    worker.last_seen_at = utc_now()
    session.add(worker)
    session.commit()
    return {"id": worker.id, "name": worker.name, "status": worker.status, "capabilities": request.capabilities}


@router.post("/{worker_id}/heartbeat", dependencies=[Depends(require_worker_token)])
def heartbeat(worker_id: str, request: WorkerHeartbeat, session: Session = Depends(get_db)) -> dict:
    worker = _worker(session, worker_id)
    worker.status = "online"
    worker.last_seen_at = utc_now()
    if request.metadata:
        worker.metadata_json = dumps(request.metadata)
    session.commit()
    return {"id": worker.id, "status": worker.status, "server_time": utc_now()}


@router.post("/{worker_id}/claim", dependencies=[Depends(require_worker_token)])
def claim(worker_id: str, request: WorkerClaim, session: Session = Depends(get_db)) -> dict | None:
    worker = _worker(session, worker_id)
    if get_settings().multiuser_enabled:
        user = session.get(ErpUser, worker.owner_user_id) if worker.owner_user_id else None
        if not user or not user.enabled or user.role == "viewer":
            raise HTTPException(status_code=403, detail="执行电脑未绑定启用的操作账号")
    worker.last_seen_at = utc_now()
    allowed = REMOTE_JOB_TYPES.intersection(request.capabilities)
    job = acquire_job(session, worker.id, allowed_types=allowed, target_worker_id=worker.id, require_assignment=get_settings().multiuser_enabled, assigned_user_id=worker.owner_user_id)
    session.commit()
    return job_dict(job) if job else None


def _leased_job(session: Session, worker_id: str, job_id: str, attempt: int) -> Job:
    if get_settings().multiuser_enabled:
        worker = _worker(session, worker_id)
        user = session.get(ErpUser, worker.owner_user_id) if worker.owner_user_id else None
        assigned = session.scalar(select(Job.id).where(Job.id == job_id, Job.target_worker_id == worker_id, Job.created_by == worker.owner_user_id))
        if not assigned or not user or not user.enabled or user.role == "viewer":
            raise HTTPException(status_code=403, detail="任务不属于该电脑的启用用户")
    now = utc_now()
    valid = session.execute(update(Job).where(
        Job.id == job_id, Job.status == "running", Job.lease_owner == worker_id,
        Job.attempts == attempt, Job.lease_expires_at > now,
    ).values(heartbeat_at=now).execution_options(synchronize_session=False)).rowcount
    if not valid:
        session.rollback()
        raise ConflictError("任务租约或领取批次已失效，拒绝旧结果")
    job = session.get(Job, job_id)
    if not job:
        raise NotFoundError("任务不存在")
    session.refresh(job)  # A control request may have committed while the lease UPDATE waited.
    if job.status != "running" or job.lease_owner != worker_id:
        raise ConflictError("任务不属于当前 Worker 或租约已经失效")
    if job.cancel_requested or job.pause_requested:
        job.status = "cancelled" if job.cancel_requested else "paused"
        job.lease_owner = ""; job.lease_expires_at = None
        if job.cancel_requested:
            job.finished_at = now
            if job.type=='pod_wb_price':
                from erp.domains.pod.wb_price_state import release_interrupted
                release_interrupted(session,job)
            if job.type=='pod_purchase_submit':
                from erp.domains.orders.desktop_purchase_submit import release_unwritten
                release_unwritten(session,job)
        session.commit()
        raise ConflictError("任务已停止或暂停，拒绝回传结果")
    return job


def _register_image_result(session: Session, job: Job, worker_id: str, result: dict) -> Asset:
    required = ("oss_bucket", "oss_key", "oss_url", "sha256", "output_bytes", "width", "height")
    if any(not result.get(name) for name in required):
        raise ConflictError("Worker 图片结果缺少 OSS 或文件信息")
    oss_url = str(result["oss_url"])
    sha256 = str(result["sha256"])
    if not oss_url.startswith("https://") or len(sha256) != 64:
        raise ConflictError("Worker 图片结果格式无效")
    legacy_id = f"{result['oss_bucket']}:{result['oss_key']}"
    asset = session.scalar(select(Asset).where(
        Asset.legacy_source == "worker_oss", Asset.legacy_id == legacy_id,
    ))
    payload = loads(job.payload_json, {})
    if not asset:
        suffix = ".webp" if str(result.get("output_format")) == "webp" else ".jpg"
        asset = Asset(
            kind="image",
            name=str(payload.get("name") or f"worker-{job.id}{suffix}"),
            source="remote_worker",
            oss_bucket=str(result["oss_bucket"]),
            oss_key=str(result["oss_key"]),
            oss_url=oss_url,
            sha256=sha256,
            mime_type="image/webp" if suffix == ".webp" else "image/jpeg",
            size_bytes=int(result["output_bytes"]),
            width=int(result["width"]),
            height=int(result["height"]),
            read_only=False,
            mirror_status="mirrored",
            legacy_source="worker_oss",
            legacy_id=legacy_id,
            metadata_json=dumps({"job_id": job.id, "worker_id": worker_id, "source_url": payload.get("source_url", "")}),
        )
        session.add(asset)
        session.flush()
    artifact = session.scalar(select(JobArtifact).where(
        JobArtifact.job_id == job.id, JobArtifact.asset_id == asset.id,
    ))
    if not artifact:
        session.add(JobArtifact(job_id=job.id, asset_id=asset.id, role="optimized_image"))
    product_id = str(payload.get("product_id") or "")
    if product_id:
        product = session.get(CatalogProduct, product_id)
        if not product:
            raise ConflictError("图片任务关联的产品不存在")
        link_catalog_asset(
            session,
            product=product,
            asset=asset,
            role=str(payload.get("role") or "additional"),
            position=int(payload.get("position") or 0),
            variant_id=payload.get("variant_id"),
            source_kind="remote_worker",
            set_current=bool(payload.get("set_current", True)),
            metadata={"job_id": job.id, "worker_id": worker_id},
        )
    return asset


@router.post("/{worker_id}/jobs/{job_id}/heartbeat", dependencies=[Depends(require_worker_token)])
def job_heartbeat(worker_id: str, job_id: str, session: Session = Depends(get_db), x_worker_attempt: int = Header(...)) -> dict:
    job = _leased_job(session, worker_id, job_id, x_worker_attempt)
    now = utc_now()
    job.heartbeat_at = now
    job.lease_expires_at = now + timedelta(seconds=get_settings().worker_lease_seconds)
    session.commit()
    return {"id": job.id, "status": job.status, "lease_expires_at": job.lease_expires_at}


@router.post("/{worker_id}/jobs/{job_id}/complete", dependencies=[Depends(require_worker_token)])
def complete_job(worker_id: str, job_id: str, request: WorkerJobResult, session: Session = Depends(get_db), x_worker_attempt: int = Header(...)) -> dict:
    job = _leased_job(session, worker_id, job_id, x_worker_attempt)
    if job.type == "image_prepare" and request.result.get("oss_url"):
        asset = _register_image_result(session, job, worker_id, request.result)
        request.result["asset_id"] = asset.id
    for step in job.steps:
        step.status = "completed"
        step.progress = 100
        step.started_at = step.started_at or job.started_at or utc_now()
        step.finished_at = utc_now()
        step.details_json = dumps(request.result)
    job.status = "completed"
    job.progress = 100
    job.error_code = ""
    job.error_message = ""
    if not (job.type == 'catalog_ozon_options' and loads(job.payload_json,{}).get('kind') == 'listing_status'):
        job.external_ref = dumps(request.result)[:300]
    job.finished_at = utc_now()
    job.lease_owner = ""
    job.lease_expires_at = None
    session.add(JobEvent(job_id=job.id, message="本地 Worker 已回传执行结果", data_json=dumps(request.result)))
    session.commit()
    return job_dict(job)


@router.post("/{worker_id}/jobs/{job_id}/fail", dependencies=[Depends(require_worker_token)])
def fail_job(worker_id: str, job_id: str, request: WorkerJobFailure, session: Session = Depends(get_db), x_worker_attempt: int = Header(...)) -> dict:
    job = _leased_job(session, worker_id, job_id, x_worker_attempt)
    step = next((item for item in job.steps if item.status != "completed"), None)
    if step:
        step.status = "failed"
        step.started_at = step.started_at or job.started_at or utc_now()
        step.finished_at = utc_now()
        step.error_message = request.error_message
    wb_rate_limited = job.type in {'pod_wb_stock', 'pod_wb_price'} and request.error_code in {'wb_stock_rate_limited', 'wb_price_rate_limited'}
    job.status = "failed" if wb_rate_limited else ("retrying" if job.attempts < job.max_attempts else "failed")
    job.heartbeat_at = utc_now()
    job.error_code = request.error_code
    job.error_message = request.error_message
    job.lease_owner = ""
    job.lease_expires_at = None
    if job.type == 'pod_wb_price':
        from erp.domains.pod.wb_price_recovery import verification_failed, interrupted
        if not verification_failed(session, job) and job.error_code == 'wb_price_outcome_unknown':
            interrupted(session, job)
        from erp.domains.pod.wb_price_state import release_interrupted
        release_interrupted(session, job)
    if job.status == "failed":
        job.finished_at = utc_now()
        if job.type=='pod_wb_price':
            from erp.domains.pod.wb_price_state import release_interrupted
            release_interrupted(session,job)
    session.add(JobEvent(job_id=job.id, level="error", message=request.error_message))
    session.commit()
    return job_dict(job)
