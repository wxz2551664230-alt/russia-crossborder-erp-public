from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, or_, case
from sqlalchemy.orm import Session, selectinload

from erp.core.database import get_db
from erp.core.jsonutil import dumps
from erp.models import Job
from erp.core.errors import AppError
from erp.services.job_filters import MODULES, module_condition, job_module
from erp.schemas import JobCreate
from erp.serializers import job_dict
from erp.services.config_resolution import build_job_snapshot
from erp.services.jobs import (
    COMPLETED_JOB_KEEP_LATEST,
    COMPLETED_JOB_RETENTION_DAYS,
    cleanup_completed_jobs,
    enqueue_job,
    get_job,
    request_cancel,
    request_pause,
    request_resume,
    retry_job,
)


router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.delete("/completed")
def clear_completed_jobs(session: Session = Depends(get_db)) -> dict:
    return {
        "removed": cleanup_completed_jobs(session),
        "retention_days": COMPLETED_JOB_RETENTION_DAYS,
        "keep_latest": COMPLETED_JOB_KEEP_LATEST,
    }


@router.get("")
def list_jobs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    status: str = "",
    job_type: str = "",
    module: str = "",
    search: str = Query("", max_length=200),
    store_id: str = Query("", max_length=32),
    worker_id: str = Query("", max_length=100),
    session: Session = Depends(get_db),
) -> dict:
    conditions = []
    if worker_id:
        conditions.append(Job.target_worker_id == worker_id)
    if module:
        if module not in {*MODULES, 'other'}:
            raise AppError('未知的任务板块', code='job_module_invalid')
        conditions.append(module_condition(module))
    if job_type:
        conditions.append(Job.type == job_type)
    if search.strip():
        term = search.strip()
        conditions.append(or_(Job.id.contains(term, autoescape=True),
            Job.type.contains(term, autoescape=True), Job.error_message.contains(term, autoescape=True),
            Job.payload_json.contains(term, autoescape=True)))
    if store_id:
        conditions.append(Job.payload_json.contains(store_id, autoescape=True))
    counts = dict(session.execute(select(Job.status, func.count()).where(*conditions).group_by(Job.status)).all())
    if status == 'unfinished':
        conditions.append(Job.status.notin_({'completed', 'cancelled'}))
    elif status == 'active':
        conditions.append(Job.status.in_({'running','retrying'}))
    elif status == 'attention':
        conditions.append(Job.status.in_({'failed','action_required','paused'}))
    elif status:
        conditions.append(Job.status == status)
    total = int(session.scalar(select(func.count()).select_from(Job).where(*conditions)) or 0)
    items = session.scalars(
        select(Job).where(*conditions).order_by(
            case((Job.status.in_({'running','retrying'}),0),(Job.status=='action_required',1),
                 else_=2) if status=='unfinished' else Job.priority.desc(),
            Job.updated_at.desc(),Job.created_at.desc(),Job.id)
        .offset((page - 1) * page_size).limit(page_size)
        .options(selectinload(Job.steps))
    ).all()
    return {"items": [{**job_dict(item), 'business_module': job_module(item.type),
                       'lease_expires_at': item.lease_expires_at.isoformat() if item.lease_expires_at else None}
                      for item in items], "page": page, "page_size": page_size, "total": total,
            'status_counts': counts,
            'modules': [{'key': key, 'label': value[0]} for key, value in MODULES.items()] + [{'key': 'other', 'label': '其他任务'}]}


@router.post("", status_code=202)
def create_job(request: JobCreate, session: Session = Depends(get_db)) -> dict:
    steps = {
        "legacy_migration": [("inspect", "扫描旧系统"), ("connections", "迁移接口与店铺"), ("research_products", "迁移选品商品"), ("catalog", "迁移产品和资产")],
        "asset_index": [("index", "建立历史文件索引")],
        "worker_probe": [("probe", "验证本地 Worker 通信")],
        "image_prepare": [("download", "下载图片"), ("optimize", "检查并压缩图片")],
    }[request.type]
    job = enqueue_job(
        session,
        job_type=request.type,
        payload=request.payload,
        config_snapshot=build_job_snapshot(session),
        idempotency_key=request.idempotency_key or f"{request.type}:{uuid.uuid4().hex}",
        priority=request.priority,
        steps=steps,
    )
    return job_dict(job)


@router.get('/overview')
def task_overview(page:int=Query(1,ge=1),page_size:int=Query(50,ge=1,le=200),status:str='',
                  module:str='',search:str=Query('',max_length=200),store_id:str='',job_type:str='',
                  worker_id:str=Query('',max_length=100),
                  session:Session=Depends(get_db)):
    from erp.services.task_overview import list_overview
    if module and module not in {*MODULES,'other'}:raise AppError('未知的任务板块',code='job_module_invalid')
    return list_overview(session,page,page_size,status,module,search,store_id,job_type,worker_id)

@router.get('/overview/{group_id}')
def task_overview_detail(group_id:str,session:Session=Depends(get_db)):
    from erp.services.task_overview import overview
    from erp.core.errors import NotFoundError
    item=next((g for g in overview(session) if g['id']==group_id),None)
    if not item:raise NotFoundError('任务分组不存在')
    return {k:v for k,v in item.items() if k!='search_text'}

@router.get("/{job_id}")
def job_detail(job_id: str, session: Session = Depends(get_db)) -> dict:
    job=get_job(session, job_id)
    return {**job_dict(job, include_events=True),'business_module':job_module(job.type)}


@router.post("/{job_id}/pause")
def pause_job(job_id: str, session: Session = Depends(get_db)) -> dict:
    return job_dict(request_pause(session, job_id))


@router.post("/{job_id}/resume")
def resume_job(job_id: str, session: Session = Depends(get_db)) -> dict:
    return job_dict(request_resume(session, job_id))


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str, session: Session = Depends(get_db)) -> dict:
    return job_dict(request_cancel(session, job_id))


@router.post("/{job_id}/retry")
def retry(job_id: str, session: Session = Depends(get_db)) -> dict:
    return job_dict(retry_job(session, job_id))
