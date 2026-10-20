from datetime import timedelta
from pathlib import Path
import hashlib
import uuid

from fastapi import APIRouter, Depends, Request, Response, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from erp.auth_models import ErpSession, ErpUser
from erp.core.access import COOKIE_NAME
from erp.core.database import get_db
from erp.core.errors import ConflictError, NotFoundError
from erp.core.jsonutil import loads, dumps
from erp.models import CatalogImportRun, IntegrationConnection, Job, JobEvent, WorkerNode, utc_now
from erp.services.accounts import audit
from erp.api.routes.auth import admin, account_mode
from erp.services.jobs import enqueue_job
from erp.schemas import WorkerJobResult, WorkerJobFailure
from erp.serializers import job_dict
from erp.services.jobs import acquire_job

router = APIRouter(prefix="/execution-devices", tags=["execution-devices"])


def _sha256_file(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class Owner(BaseModel):
    user_id: str = Field(max_length=32)


class Selection(BaseModel):
    worker_id: str = Field(min_length=1, max_length=100)


class LocalRegistration(BaseModel):
    id: str = Field(pattern=r'^pc-[a-f0-9]{32}$')
    name: str = Field(min_length=1,max_length=200)
    amazon_version: int = Field(default=0,ge=0,le=1)
    composition_version: int = Field(default=0,ge=0,le=2)
    artwork_version: int = Field(default=0,ge=0,le=1)
    ai_version: int = Field(default=0,ge=0,le=1)
    upscale_ready: bool = False
    purchase_submit_version: int = Field(default=0, ge=0, le=1)
    purchase_read_version: int = Field(default=0, ge=0, le=1)
    order_read_version: int = Field(default=0, ge=0, le=1)
    wb_version: int = Field(default=0, ge=0, le=1)
    wb_api_version: int = Field(default=0, ge=0, le=1)
    wb_reconcile_version: int = Field(default=0, ge=0, le=1)
    wb_history_version: int = Field(default=0, ge=0, le=3)
    ozon_promotion_unlimited_version: int = Field(default=0, ge=0, le=1)
    wb_stock_version: int = Field(default=0, ge=0, le=1)
    wb_price_version: int = Field(default=0, ge=0, le=1)
    ad_analysis_version: int = Field(default=0, ge=0, le=1)
    ad_backtest_version: int = Field(default=0, ge=0, le=1)
    ad_sync_version: int = Field(default=0, ge=0, le=1)
    ad_report_version: int = Field(default=0, ge=0, le=1)
    ad_preview_version: int = Field(default=0, ge=0, le=1)
    ad_execute_version: int = Field(default=0, ge=0, le=1)
    ad_reconcile_version: int = Field(default=0, ge=0, le=1)
    supplier_gallery_version: int = Field(default=0, ge=0, le=1)
    supplier_catalog_version: int = Field(default=0, ge=0, le=3)
    catalog_import_version: int = Field(default=0, ge=0, le=1)
    pod_import_version: int = Field(default=0, ge=0, le=1)
    self_listing_version: int = Field(default=0, ge=0, le=1)
    store_test_version: int = Field(default=0, ge=0, le=1)
    catalog_ozon_version: int = Field(default=0, ge=0, le=3)
    codex_generation_version: int = Field(default=0, ge=0, le=4)


class CompositionProgress(BaseModel):
    key: str = Field(max_length=80)
    progress: float = Field(ge=0,le=100)
    completed: bool = False


class WbFile(BaseModel):
    content: str = Field(min_length=1, max_length=34*1024**2)


class AdAnalysisFile(BaseModel):
    content: str = Field(min_length=1, max_length=16*1024**2 + 4)


class SelfListingArtifactFile(BaseModel):
    role: str = Field(pattern=r'^(copywriting_xlsx|oss_original_xlsx|oss_watermarked_xlsx)$')
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    content: str = Field(min_length=1, max_length=34*1024**2)


class OzonAiStoreResult(BaseModel):
    revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    draft: dict


class AdSyncChunk(BaseModel):
    index: int = Field(ge=0, lt=4000)
    total: int = Field(ge=1, le=4000)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    content: str = Field(min_length=1, max_length=55*1024)


class AdSyncCommit(BaseModel):
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class AdReportFile(BaseModel):
    content: str = Field(min_length=1, max_length=16*1024**2 + 4)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    snapshot_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    summary: dict


class CatalogImportFile(BaseModel):
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    content: str = Field(min_length=1, max_length=35 * 1024 * 1024)


class OrderBatch(BaseModel):
    index: int = Field(ge=0, lt=10000)
    postings: list[dict] = Field(max_length=200)
    final: bool = False


class OrderImages(BaseModel):
    images: dict[str, str]


class PurchaseStatuses(BaseModel):
    records: list[dict] = Field(max_length=500)


class PurchaseOperation(BaseModel):
    method: str = Field(min_length=1,max_length=80)
    args: list = Field(default_factory=list,max_length=5)
    kwargs: dict = Field(default_factory=dict)


def leased_purchase_submit(worker_id,job_id,request,db,attempt):
    from erp.api.routes.workers import _leased_job
    job=local_job(worker_id,job_id,request,db)
    if job.type!='pod_purchase_submit':raise HTTPException(409,'不是采购提交任务')
    return _leased_job(db,worker_id,job_id,attempt)


@router.post('/local/{worker_id}/jobs/{job_id}/purchase-submit-snapshot')
def purchase_submit_snapshot(worker_id:str,job_id:str,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from fastapi.responses import JSONResponse
    from erp.domains.orders.desktop_purchase_submit import snapshot
    value=snapshot(db,leased_purchase_submit(worker_id,job_id,request,db,x_worker_attempt))
    db.commit();return JSONResponse(value,headers={'Cache-Control':'no-store'})


@router.post('/local/{worker_id}/jobs/{job_id}/purchase-operation')
def purchase_operation(worker_id:str,job_id:str,payload:PurchaseOperation,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from fastapi.responses import JSONResponse
    from erp.domains.orders.desktop_purchase_submit import operation
    value=operation(db,leased_purchase_submit(worker_id,job_id,request,db,x_worker_attempt),payload.method,payload.args,payload.kwargs)
    db.commit();return JSONResponse(value,headers={'Cache-Control':'no-store'})


@router.post('/local/{worker_id}/jobs/{job_id}/purchase-submit-result')
def purchase_submit_result(worker_id:str,job_id:str,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from erp.domains.orders.desktop_purchase_submit import completed_state
    value=completed_state(db,leased_purchase_submit(worker_id,job_id,request,db,x_worker_attempt))
    db.commit();return value


@router.post('/local/{worker_id}/jobs/{job_id}/purchase-write-intent')
def purchase_write_intent(worker_id:str,job_id:str,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from erp.domains.orders.desktop_purchase_submit import write_intent
    value=write_intent(db,leased_purchase_submit(worker_id,job_id,request,db,x_worker_attempt))
    db.commit();return value


def leased_purchase(worker_id,job_id,request,db,attempt):
    from erp.api.routes.workers import _leased_job
    job=local_job(worker_id,job_id,request,db)
    if job.type!='pod_purchase_sync':raise HTTPException(409,'不是采购状态同步任务')
    return _leased_job(db,worker_id,job_id,attempt)


@router.post('/local/{worker_id}/jobs/{job_id}/purchase-snapshot')
def purchase_snapshot(worker_id:str,job_id:str,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from fastapi.responses import JSONResponse
    from erp.domains.orders.desktop_purchase import snapshot
    value=snapshot(db,leased_purchase(worker_id,job_id,request,db,x_worker_attempt))
    db.commit();return JSONResponse(value,headers={'Cache-Control':'no-store'})


@router.post('/local/{worker_id}/jobs/{job_id}/purchase-statuses')
def purchase_statuses(worker_id:str,job_id:str,payload:PurchaseStatuses,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from erp.domains.orders.desktop_purchase import accept
    value=accept(db,leased_purchase(worker_id,job_id,request,db,x_worker_attempt),payload.records)
    db.commit();return value


def leased_order(worker_id, job_id, request, db, attempt):
    from erp.api.routes.workers import _leased_job
    from erp.domains.orders.desktop import TYPES
    job = local_job(worker_id, job_id, request, db)
    if job.type not in TYPES:
        raise HTTPException(409, '不是订单读取任务')
    return _leased_job(db, worker_id, job_id, attempt)


@router.get('/local/{worker_id}/jobs/{job_id}/ad-analysis-snapshot')
def ad_analysis_snapshot(worker_id: str, job_id: str, request: Request,
                         db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from fastapi.responses import FileResponse
    from erp.api.routes.workers import _leased_job
    from erp.services.ad_desktop_snapshot import analysis_download
    job = local_job(worker_id, job_id, request, db)
    if job.type not in {'ad_optimization_analyze', 'ad_optimization_backtest', 'ad_optimization_reports', 'ad_optimization_preview'}:
        raise HTTPException(409, '不是广告诊断任务')
    _leased_job(db, worker_id, job_id, x_worker_attempt)
    store_id = loads(job.payload_json, {}).get('store_id')
    if not isinstance(store_id, str) or not store_id:
        raise HTTPException(409, '广告任务缺少店铺')
    path, digest, source_digest = analysis_download(db, job_id, store_id)
    db.commit()
    return FileResponse(path, media_type='application/gzip', filename='ad-analysis.sqlite.gz',
                        headers={'Cache-Control': 'no-store', 'X-Content-SHA256': digest,
                                 'X-Source-SHA256': source_digest})


@router.get('/local/{worker_id}/jobs/{job_id}/ad-sync-snapshot')
def ad_sync_snapshot(worker_id: str, job_id: str, request: Request,
                     db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from fastapi.responses import FileResponse
    from erp.api.routes.workers import _leased_job
    from erp.services.ad_desktop_snapshot import analysis_download
    job = local_job(worker_id, job_id, request, db)
    if job.type != 'ad_optimization_sync':
        raise HTTPException(409, '不是广告同步任务')
    _leased_job(db, worker_id, job_id, x_worker_attempt)
    store_id = loads(job.payload_json, {}).get('store_id')
    if not isinstance(store_id, str) or not store_id:
        raise HTTPException(409, '广告任务缺少店铺')
    path, digest, source_digest = analysis_download(db, job_id, store_id, sync_input=True)
    db.commit()
    return FileResponse(path, media_type='application/gzip', filename='ad-sync.sqlite.gz',
                        headers={'Cache-Control': 'no-store', 'X-Content-SHA256': digest,
                                 'X-Source-SHA256': source_digest})


@router.get('/local/{worker_id}/jobs/{job_id}/ad-execution-snapshot')
def ad_execution_snapshot(worker_id: str, job_id: str, request: Request,
                          db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from fastapi.responses import FileResponse
    from erp.api.routes.workers import _leased_job
    from erp.models import AdExecutionBatch
    from erp.services.ad_desktop_snapshot import analysis_download
    job = local_job(worker_id, job_id, request, db)
    if job.type != 'ad_optimization_execute':
        raise HTTPException(409, '不是广告正式执行任务')
    _leased_job(db, worker_id, job_id, x_worker_attempt)
    payload = loads(job.payload_json, {})
    batch = db.get(AdExecutionBatch, payload.get('approval_id'))
    if (not batch or batch.store_id != payload.get('store_id') or batch.actor != job.created_by
            or batch.status != 'executing' or batch.active_store != batch.store_id):
        raise ConflictError('广告批次未锁定到原任务')
    path, digest, source_digest = analysis_download(
        db, job_id, batch.store_id, execution_batch=batch)
    db.commit()
    return FileResponse(path, media_type='application/gzip', filename='ad-execution.sqlite.gz',
                        headers={'Cache-Control': 'no-store', 'X-Content-SHA256': digest,
                                 'X-Source-SHA256': source_digest})


@router.get('/local/{worker_id}/jobs/{job_id}/ad-execution-credentials')
def ad_execution_credentials(worker_id: str, job_id: str, request: Request,
                             db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from fastapi.responses import JSONResponse
    from erp.api.routes.workers import _leased_job
    from erp.core.security import vault
    from erp.models import AdExecutionBatch, Store
    job = local_job(worker_id, job_id, request, db)
    if job.type not in {'ad_optimization_execute', 'ad_optimization_reconcile'}:
        raise HTTPException(409, '不是广告正式执行任务')
    _leased_job(db, worker_id, job_id, x_worker_attempt)
    store_id = loads(job.payload_json, {}).get('store_id')
    if job.type == 'ad_optimization_reconcile':
        batch = db.get(AdExecutionBatch, loads(job.payload_json, {}).get('approval_id'))
        if (not batch or batch.store_id != store_id or batch.actor != job.created_by
                or batch.status not in {'unknown', 'executing'}
                or batch.active_store != store_id):
            raise ConflictError('广告待核对批次已变化，拒绝发放临时凭据')
    store = db.get(Store, store_id)
    if not store or not store.enabled or store.platform != 'ozon':
        raise ConflictError('广告店铺不可用')
    secrets = vault.decrypt(store.secrets_encrypted)
    client_id = str(secrets.get('performance_client_id') or '')
    client_secret = str(secrets.get('performance_client_secret') or '')
    if not client_id or not client_secret:
        raise ConflictError('Ozon 广告连接凭据不完整')
    return JSONResponse({'store_id': store_id, 'performance_client_id': client_id,
                         'performance_client_secret': client_secret},
                        headers={'Cache-Control': 'no-store'})


@router.get('/local/{worker_id}/jobs/{job_id}/ad-reconcile-snapshot')
def ad_reconcile_snapshot(worker_id: str, job_id: str, request: Request,
                          db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from fastapi.responses import FileResponse
    from erp.api.routes.workers import _leased_job
    from erp.models import AdExecutionBatch
    from erp.services.ad_desktop_snapshot import analysis_download
    job = local_job(worker_id, job_id, request, db)
    if job.type != 'ad_optimization_reconcile':
        raise HTTPException(409, '不是广告只读核对任务')
    _leased_job(db, worker_id, job_id, x_worker_attempt)
    task = loads(job.payload_json, {})
    batch = db.get(AdExecutionBatch, task.get('approval_id'))
    if (not batch or batch.store_id != task.get('store_id') or batch.actor != job.created_by
            or batch.status not in {'unknown', 'executing'} or batch.active_store != batch.store_id):
        raise ConflictError('广告待核对批次与任务不一致')
    path, digest, source_digest = analysis_download(
        db, job_id, batch.store_id, reconcile_batch=batch)
    db.commit()
    return FileResponse(path, media_type='application/gzip', filename='ad-reconcile.sqlite.gz',
                        headers={'Cache-Control': 'no-store', 'X-Content-SHA256': digest,
                                 'X-Source-SHA256': source_digest})


@router.get('/local/{worker_id}/jobs/{job_id}/ad-sync-credentials')
def ad_sync_credentials(worker_id: str, job_id: str, request: Request,
                        db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from fastapi.responses import JSONResponse
    from erp.api.routes.workers import _leased_job
    from erp.services.config_resolution import build_job_snapshot, resolve_job_snapshot
    job = local_job(worker_id, job_id, request, db)
    if job.type != 'ad_optimization_sync':
        raise HTTPException(409, '不是广告同步任务')
    _leased_job(db, worker_id, job_id, x_worker_attempt)
    store_id = loads(job.payload_json, {}).get('store_id')
    if not isinstance(store_id, str) or not store_id:
        raise HTTPException(409, '广告任务缺少店铺')
    config = resolve_job_snapshot(build_job_snapshot(db, store_id=store_id))
    db.commit()
    return JSONResponse({'store_id': store_id, 'config': config},
                        headers={'Cache-Control': 'no-store'})


@router.post('/local/{worker_id}/jobs/{job_id}/ad-analysis-result')
def ad_analysis_result(worker_id: str, job_id: str, payload: AdAnalysisFile,
                       request: Request, db: Session = Depends(get_db),
                       x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.ad_desktop_result import apply_analysis_result
    job = local_job(worker_id, job_id, request, db)
    if job.type not in {'ad_optimization_analyze', 'ad_optimization_preview'}:
        raise HTTPException(409, '不是广告分析任务')
    job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    result = apply_analysis_result(db, job, payload.content)
    db.commit()
    return result


@router.post('/local/{worker_id}/jobs/{job_id}/ad-backtest-result')
def ad_backtest_result(worker_id: str, job_id: str, payload: AdAnalysisFile,
                       request: Request, db: Session = Depends(get_db),
                       x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.ad_desktop_result import apply_backtest_result
    job = local_job(worker_id, job_id, request, db)
    if job.type != 'ad_optimization_backtest':
        raise HTTPException(409, '不是广告回测任务')
    job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    result = apply_backtest_result(db, job, payload.content)
    db.commit()
    return result


@router.post('/local/{worker_id}/jobs/{job_id}/ad-report-result')
def ad_report_result(worker_id: str, job_id: str, payload: AdReportFile,
                     request: Request, db: Session = Depends(get_db),
                     x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.ad_report_result import accept_report

    job = local_job(worker_id, job_id, request, db)
    if job.type != 'ad_optimization_reports':
        raise HTTPException(409, '不是广告报告任务')
    job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    receipt = accept_report(db, job, encoded=payload.content, digest=payload.sha256,
                            snapshot_hash=payload.snapshot_sha256,
                            source_hash=payload.source_sha256, summary=payload.summary)
    db.commit()
    return receipt


@router.post('/local/{worker_id}/jobs/{job_id}/ad-execution-result')
def ad_execution_result(worker_id: str, job_id: str, payload: AdAnalysisFile,
                        request: Request, db: Session = Depends(get_db),
                        x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.ad_execution_result import accept_execution_result
    job = local_job(worker_id, job_id, request, db)
    if job.type != 'ad_optimization_execute':
        raise HTTPException(409, '不是广告正式执行任务')
    job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    result = accept_execution_result(db, job, payload.content)
    db.commit()
    return result


@router.post('/local/{worker_id}/jobs/{job_id}/ad-reconcile-result')
def ad_reconcile_result(worker_id: str, job_id: str, payload: AdAnalysisFile,
                        request: Request, db: Session = Depends(get_db),
                        x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.ad_reconcile_result import accept_reconcile_result
    job = local_job(worker_id, job_id, request, db)
    if job.type != 'ad_optimization_reconcile':
        raise HTTPException(409, '不是广告只读核对任务')
    job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    result = accept_reconcile_result(db, job, payload.content)
    db.commit()
    return result


@router.post('/local/{worker_id}/jobs/{job_id}/ad-sync-chunk')
def ad_sync_chunk(worker_id: str, job_id: str, payload: AdSyncChunk,
                  request: Request, db: Session = Depends(get_db),
                  x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.ad_sync_transport import store_chunk

    job = local_job(worker_id, job_id, request, db)
    if job.type != 'ad_optimization_sync':
        raise HTTPException(409, '不是广告同步任务')
    _leased_job(db, worker_id, job_id, x_worker_attempt)
    receipt = store_chunk(job_id, index=payload.index, total=payload.total,
                          digest=payload.sha256, encoded=payload.content)
    db.commit()
    return receipt


@router.post('/local/{worker_id}/jobs/{job_id}/ad-sync-result')
def ad_sync_result(worker_id: str, job_id: str, payload: AdSyncCommit,
                   request: Request, db: Session = Depends(get_db),
                   x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.ad_sync_result import apply_sync_result, completion_summary
    from erp.services.ad_sync_transport import read_result, cleanup

    job = local_job(worker_id, job_id, request, db)
    if job.type != 'ad_optimization_sync':
        raise HTTPException(409, '不是广告同步任务')
    if job.status == 'completed':
        receipt = completion_summary(job)
        if job.attempts != x_worker_attempt or receipt['result_sha256'] != payload.sha256:
            raise ConflictError('广告同步已完成，拒绝不同领取批次或结果')
        return receipt
    job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    value, size = read_result(job_id, digest=payload.sha256)
    receipt = apply_sync_result(db, job, value, payload.sha256, size)
    # The data merge and terminal job receipt must commit together. A lost
    # response must never leave an applied sync stuck behind an expired lease.
    now = utc_now()
    for step in job.steps:
        step.status = 'completed'
        step.progress = 100
        step.started_at = step.started_at or job.started_at or now
        step.finished_at = now
        step.details_json = dumps(receipt)
    job.status = 'completed'
    job.progress = 100
    job.finished_at = now
    job.lease_owner = ''
    job.lease_expires_at = None
    job.error_code = ''
    job.error_message = ''
    db.add(JobEvent(job_id=job.id, message='本地 Worker 广告同步结果已验收并完成任务',
                    data_json=dumps(receipt)))
    db.commit()
    from erp.services.ad_desktop_snapshot import cleanup_completed_snapshot
    try: cleanup(job.id)
    except OSError: pass
    try: cleanup_completed_snapshot(job.id)
    except OSError: pass
    return receipt


@router.post('/local/{worker_id}/jobs/{job_id}/order-snapshot')
def order_snapshot(worker_id:str,job_id:str,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from fastapi.responses import JSONResponse
    from erp.domains.orders.desktop import snapshot
    job=leased_order(worker_id,job_id,request,db,x_worker_attempt)
    if loads(job.payload_json, {}).get('scope_version') == 1 and request.headers.get('X-Order-Scope-Version') != '1':
        raise HTTPException(409, '请更新本机订单执行模块后继续，旧客户端不支持指定同步范围')
    value=snapshot(db,job);db.commit()
    return JSONResponse(value,headers={'Cache-Control':'no-store'})


@router.post('/local/{worker_id}/jobs/{job_id}/order-batch')
def order_batch(worker_id:str,job_id:str,payload:OrderBatch,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from erp.domains.orders.desktop import accept_batch
    job=leased_order(worker_id,job_id,request,db,x_worker_attempt)
    value=accept_batch(db,job,payload.index,payload.postings,payload.final)
    db.commit();return value


@router.post('/local/{worker_id}/jobs/{job_id}/order-images')
def order_images(worker_id:str,job_id:str,payload:OrderImages,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from erp.domains.orders.desktop import accept_images
    job=leased_order(worker_id,job_id,request,db,x_worker_attempt)
    value=accept_images(db,job,payload.images)
    db.commit();return value


def leased_wb(worker_id, job_id, request, db, attempt):
    from erp.api.routes.workers import _leased_job
    job = local_job(worker_id, job_id, request, db)
    if job.type not in {'pod_wb_excel_export', 'pod_wb_api_publish'}: raise HTTPException(409, '不是 WB 发布任务')
    return _leased_job(db, worker_id, job_id, attempt)


@router.post('/local/{worker_id}/jobs/{job_id}/wb-snapshot')
def wb_snapshot(worker_id:str,job_id:str,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    import base64
    from pathlib import Path
    from fastapi.responses import JSONResponse
    from erp.domains.pod.desktop_wb import snapshot
    from erp.domains.pod.wb_export import _template
    job = leased_wb(worker_id,job_id,request,db,x_worker_attempt)
    value = snapshot(db,job)
    value['template_content'] = base64.b64encode(Path(_template(value['template']['id'])['path']).read_bytes()).decode()
    db.commit()
    return JSONResponse(value,headers={'Cache-Control':'no-store'})


@router.post('/local/{worker_id}/jobs/{job_id}/wb-reconcile-snapshot')
def wb_reconcile_snapshot(worker_id:str,job_id:str,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from fastapi.responses import JSONResponse
    from erp.api.routes.workers import _leased_job
    from erp.domains.pod.desktop_wb import reconcile_snapshot
    job = local_job(worker_id,job_id,request,db)
    if job.type != 'pod_wb_excel_reconcile':raise HTTPException(409,'不是 WB 商品卡核对任务')
    _leased_job(db,worker_id,job_id,x_worker_attempt)
    value = reconcile_snapshot(db,job);db.commit()
    return JSONResponse(value,headers={'Cache-Control':'no-store'})


@router.post('/local/{worker_id}/jobs/{job_id}/wb-history-snapshot')
def wb_history_snapshot(worker_id:str,job_id:str,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from fastapi.responses import JSONResponse
    from erp.api.routes.workers import _leased_job
    from erp.core.security import vault
    job=local_job(worker_id,job_id,request,db)
    if job.type!='pod_wb_history_reconcile':raise HTTPException(409,'不是 WB 历史扫描任务')
    _leased_job(db,worker_id,job_id,x_worker_attempt)
    config=vault.decrypt(job.config_snapshot_json)
    secrets=(config.get('store') or {}).get('secrets') or {}
    token=next((str(secrets.get(key) or '').strip() for key in ('token','api_key','wildberries_token') if str(secrets.get(key) or '').strip()),'')
    if not token:raise ConflictError('WB 店铺缺少 Content API Token')
    if loads(job.payload_json,{}).get('shop_sync'):
        from erp.domains.pod.wb_shop_sync import snapshot
        value=snapshot(db,job,token)
    else:
        value={'store_id':loads(job.payload_json,{})['store_id'],'config':{'store':{'secrets':{'token':token}}}}
    db.commit()
    return JSONResponse(value,headers={'Cache-Control':'no-store'})


class WbHistoryPart(BaseModel):
    index:int=Field(ge=0,lt=5000)
    cards:list[dict]


class WbShopSyncThrottle(BaseModel):
    retry_seconds:int=Field(ge=1,le=86400)


@router.post('/local/{worker_id}/jobs/{job_id}/wb-shop-sync-throttle')
def wb_shop_sync_throttle(worker_id:str,job_id:str,payload:WbShopSyncThrottle,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.domains.pod.wb_shop_sync import throttle
    local_job(worker_id,job_id,request,db)
    job=_leased_job(db,worker_id,job_id,x_worker_attempt)
    return throttle(db,job,payload.retry_seconds)


@router.post('/local/{worker_id}/jobs/{job_id}/wb-history-part')
def wb_history_part(worker_id:str,job_id:str,payload:WbHistoryPart,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.domains.pod.wb_history_transport import stage
    job=local_job(worker_id,job_id,request,db)
    if job.type!='pod_wb_history_reconcile':raise HTTPException(409,'不是 WB 历史扫描任务')
    _leased_job(db,worker_id,job_id,x_worker_attempt)
    value=stage(job,payload.index,payload.cards)
    db.commit()
    return value


@router.post('/local/{worker_id}/jobs/{job_id}/wb-stock-snapshot')
def wb_stock_snapshot(worker_id:str,job_id:str,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from fastapi.responses import JSONResponse
    from erp.api.routes.workers import _leased_job
    from erp.domains.pod.desktop_wb import stock_snapshot
    job=local_job(worker_id,job_id,request,db)
    if job.type!='pod_wb_stock':raise HTTPException(409,'不是 WB 库存任务')
    _leased_job(db,worker_id,job_id,x_worker_attempt)
    value=stock_snapshot(db,job);db.commit()
    return JSONResponse(value,headers={'Cache-Control':'no-store'})


@router.post('/local/{worker_id}/jobs/{job_id}/wb-price-snapshot')
def wb_price_snapshot(worker_id:str,job_id:str,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from fastapi.responses import JSONResponse
    from erp.api.routes.workers import _leased_job
    from erp.domains.pod.desktop_wb import price_snapshot
    job=local_job(worker_id,job_id,request,db)
    if job.type!='pod_wb_price':raise HTTPException(409,'不是 WB 价格任务')
    _leased_job(db,worker_id,job_id,x_worker_attempt)
    value=price_snapshot(db,job);db.commit()
    return JSONResponse(value,headers={'Cache-Control':'no-store'})


@router.post('/local/{worker_id}/jobs/{job_id}/wb-file/{role}')
def wb_file(worker_id:str,job_id:str,role:str,payload:WbFile,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    import base64, binascii
    from erp.domains.pod.desktop_wb import stage_file
    job = leased_wb(worker_id,job_id,request,db,x_worker_attempt)
    try: content = base64.b64decode(payload.content, validate=True)
    except (ValueError,binascii.Error): raise HTTPException(422,'文件编码无效')
    result = stage_file(job,role,content); db.commit()
    return result


@router.post('/local/register')
def register_local(payload:LocalRegistration, request:Request, db:Session=Depends(get_db)) -> dict:
    account_mode()
    user=request.state.user
    if user['role'] not in ('admin','operator'):raise HTTPException(403,'需要操作账号')
    device=db.get(WorkerNode,payload.id)
    if device and device.owner_user_id!=user['id']:raise ConflictError('这台电脑已绑定其他账号')
    if not device:
        device=WorkerNode(id=payload.id,owner_user_id=user['id']);db.add(device)
    capabilities=['worker_probe','pod_library_restore']+(['pod_native_composition'] if payload.composition_version>=1 else [])
    if payload.composition_version>=2:capabilities.append('pod_us_composition')
    if payload.amazon_version==1:capabilities.extend(['amazon_metadata','amazon_prepare','amazon_validate','amazon_submit','amazon_reconcile'])
    if payload.amazon_version==1 and payload.codex_generation_version>=4:capabilities.extend(['amazon_prepare_codex','codex_amazon_v1'])
    if payload.artwork_version==1:capabilities.append('pod_artwork_upload')
    if payload.ai_version==1:
        capabilities.extend(['pod_ai_design_plan','pod_ai_design_generate'])
        if payload.upscale_ready:capabilities.append('pod_ai_design_upscale')
    if payload.purchase_submit_version==1:capabilities.append('pod_purchase_submit')
    if payload.purchase_read_version==1:capabilities.append('pod_purchase_sync')
    if payload.order_read_version==1:capabilities.extend(['pod_order_sync','pod_order_image_sync'])
    if payload.wb_version==1:capabilities.append('pod_wb_excel_export')
    if payload.wb_api_version==1:capabilities.append('pod_wb_api_publish')
    if payload.wb_reconcile_version==1:capabilities.append('pod_wb_excel_reconcile')
    if payload.wb_history_version>=1:capabilities.append('pod_wb_history_reconcile')
    if payload.wb_history_version>=2:capabilities.append('wb_shop_sync_v1')
    if payload.wb_history_version>=3:capabilities.append('wb_shop_sync_v2')
    if payload.ozon_promotion_unlimited_version==1:capabilities.append('ozon_promotion_unlimited')
    if payload.wb_stock_version==1:capabilities.append('pod_wb_stock')
    if payload.wb_price_version==1:capabilities.append('pod_wb_price')
    if payload.ad_analysis_version==1:capabilities.append('ad_optimization_analyze')
    if payload.ad_backtest_version==1:capabilities.append('ad_optimization_backtest')
    if payload.ad_sync_version==1:capabilities.append('ad_optimization_sync')
    if payload.ad_report_version==1:capabilities.append('ad_optimization_reports')
    if payload.ad_preview_version==1:capabilities.append('ad_optimization_preview')
    if payload.ad_execute_version==1:capabilities.append('ad_optimization_execute')
    if payload.ad_reconcile_version==1:capabilities.append('ad_optimization_reconcile')
    if payload.supplier_gallery_version==1:capabilities.append('pod_supplier_gallery_sync')
    if payload.supplier_catalog_version>=1:capabilities.append('pod_supplier_catalog_sync')
    if payload.supplier_catalog_version>=2:capabilities.append('pod_supplier_us_catalog_sync')
    if payload.supplier_catalog_version>=3:capabilities.append('pod_supplier_us_freight_quote')
    if payload.catalog_import_version==1:capabilities.append('catalog_self_selected_import')
    if payload.pod_import_version==1:capabilities.append('catalog_pod_import')
    if payload.self_listing_version==1:capabilities.append('self_listing_pipeline')
    if payload.store_test_version==1:capabilities.append('store_connection_test')
    if payload.catalog_ozon_version>=2:capabilities.append('catalog_ozon_batch')
    if payload.catalog_ozon_version>=3:capabilities.append('catalog_ozon_stock_scan')
    if payload.codex_generation_version>=1:capabilities.append('codex_handoff_v1')
    if payload.codex_generation_version>=2:capabilities.append('codex_ozon_poster_v1')
    if payload.codex_generation_version>=3:capabilities.append('codex_wb_v1')
    if payload.catalog_ozon_version>=1:capabilities.extend(['catalog_ozon_prepare','catalog_ozon_publication','catalog_ozon_followup','catalog_ozon_options'])
    device.name=payload.name;device.capabilities_json=dumps(capabilities)
    device.last_seen_at=utc_now();device.status='online'
    digest=hashlib.sha256(request.cookies.get(COOKIE_NAME,'').encode()).hexdigest()
    if not db.execute(update(ErpSession).where(ErpSession.token_hash==digest,ErpSession.user_id==user['id']).values(worker_id=device.id)).rowcount:
        raise ConflictError('登录已失效')
    audit(db,user,'local_device_register',device.id);db.commit()
    return {'worker_id':device.id,'capabilities':capabilities}


def local_device(worker_id,request,db):
    account_mode();user=request.state.user;device=db.get(WorkerNode,worker_id)
    if user['role'] not in ('admin','operator') or not device or device.owner_user_id!=user['id'] or user.get('worker_id')!=worker_id:
        raise HTTPException(403,'当前登录未绑定这台执行电脑')
    device.last_seen_at=utc_now()
    return device


@router.post('/local/{worker_id}/claim')
def local_claim(worker_id:str,request:Request,db:Session=Depends(get_db)):
    device=local_device(worker_id,request,db)
    allowed=set(loads(device.capabilities_json,[])) & {'worker_probe','pod_library_restore','pod_native_composition','pod_us_composition','pod_artwork_upload','pod_ai_design_plan','pod_ai_design_generate','pod_ai_design_upscale','pod_wb_excel_export','pod_wb_api_publish','pod_wb_excel_reconcile','pod_wb_history_reconcile','pod_wb_stock','pod_wb_price','pod_order_sync','pod_order_image_sync','pod_purchase_sync','pod_purchase_submit','pod_supplier_gallery_sync','pod_supplier_catalog_sync','pod_supplier_us_catalog_sync','pod_supplier_us_freight_quote','amazon_metadata','amazon_prepare','amazon_prepare_codex','amazon_validate','amazon_submit','amazon_reconcile','self_listing_pipeline','catalog_self_selected_import','catalog_pod_import','store_connection_test','catalog_ozon_prepare','catalog_ozon_publication','catalog_ozon_followup','catalog_ozon_options','ad_optimization_analyze','ad_optimization_backtest','ad_optimization_sync','ad_optimization_reports','ad_optimization_preview','ad_optimization_execute','ad_optimization_reconcile'}
    job=acquire_job(db,worker_id,allowed_types=allowed,target_worker_id=worker_id,require_assignment=True,assigned_user_id=device.owner_user_id)
    db.commit();return job_dict(job) if job else None


def local_job(worker_id,job_id,request,db):
    device=local_device(worker_id,request,db);job=db.get(Job,job_id)
    if not job or job.target_worker_id!=worker_id or job.created_by!=device.owner_user_id or job.type not in ('worker_probe','pod_library_restore','pod_native_composition','pod_us_composition','pod_artwork_upload','pod_ai_design_plan','pod_ai_design_generate','pod_ai_design_upscale','pod_wb_excel_export','pod_wb_api_publish','pod_wb_excel_reconcile','pod_wb_history_reconcile','pod_wb_stock','pod_wb_price','pod_order_sync','pod_order_image_sync','pod_purchase_sync','pod_purchase_submit','pod_supplier_gallery_sync','pod_supplier_catalog_sync','pod_supplier_us_catalog_sync','pod_supplier_us_freight_quote','amazon_metadata','amazon_prepare','amazon_prepare_codex','amazon_validate','amazon_submit','amazon_reconcile','self_listing_pipeline','catalog_self_selected_import','catalog_pod_import','store_connection_test','ozon_ai_prepare','catalog_ozon_prepare','catalog_ozon_publication','catalog_ozon_followup','catalog_ozon_options','ad_optimization_analyze','ad_optimization_backtest','ad_optimization_sync','ad_optimization_reports','ad_optimization_preview','ad_optimization_execute','ad_optimization_reconcile'):
        raise HTTPException(403,'任务不属于这台电脑')
    return job


class CatalogOzonPrepared(BaseModel):
    revision: str = Field(min_length=64, max_length=64)
    result: dict


@router.get('/local/{worker_id}/jobs/{job_id}/catalog-ozon-targets')
def catalog_ozon_targets(worker_id: str, job_id: str, request: Request,
                         db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.catalog_personal_publications import targets
    local_job(worker_id, job_id, request, db)
    job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    return {'stores': targets(db, job)}


@router.post('/local/{worker_id}/jobs/{job_id}/catalog-ozon-prepare-finish')
def catalog_ozon_prepare_finish(worker_id: str, job_id: str, payload: WorkerJobResult,
                                request: Request, db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.catalog_personal_publications import finish_preparation, digest
    job = local_job(worker_id, job_id, request, db)
    if job.attempts != x_worker_attempt:
        raise ConflictError('准备阶段完成回执的尝试已过期')
    previous = loads(job.external_ref, {})
    if previous.get('kind') != 'catalog_ozon_prepared' or previous.get('finished_hash') != digest(payload.result):
        job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    result = finish_preparation(db, job, payload.result)
    db.commit()
    return result


class CatalogOzonImportIntent(BaseModel):
    item_hash: str = Field(min_length=64, max_length=64)


class CatalogOzonImportTask(CatalogOzonImportIntent):
    task_id: int = Field(gt=0)


class CatalogOzonImportResult(CatalogOzonImportTask):
    result: dict


class CatalogOzonFollowupIntent(BaseModel):
    operation_hash: str = Field(min_length=64, max_length=64)


class CatalogOzonFollowupResult(CatalogOzonFollowupIntent):
    result: dict


@router.get('/local/{worker_id}/jobs/{job_id}/catalog-ozon-followup-snapshot')
def catalog_ozon_followup_snapshot(worker_id: str, job_id: str, request: Request,
                                  db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from fastapi.responses import JSONResponse
    from erp.api.routes.workers import _leased_job
    from erp.services.catalog_personal_followup import snapshot
    local_job(worker_id, job_id, request, db)
    job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    return JSONResponse(snapshot(db, job), headers={'Cache-Control': 'no-store'})


@router.post('/local/{worker_id}/jobs/{job_id}/catalog-ozon-followup-intent')
def catalog_ozon_followup_intent(worker_id: str, job_id: str, payload: CatalogOzonFollowupIntent,
                                request: Request, db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.catalog_personal_followup import intent
    local_job(worker_id, job_id, request, db)
    job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    result = intent(db, job, payload.operation_hash)
    db.commit()
    return result


@router.post('/local/{worker_id}/jobs/{job_id}/catalog-ozon-followup-result')
def catalog_ozon_followup_result(worker_id: str, job_id: str, payload: CatalogOzonFollowupResult,
                                request: Request, db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.catalog_personal_followup import accept
    job = local_job(worker_id, job_id, request, db)
    if job.attempts != x_worker_attempt:
        raise ConflictError('阶段回执的任务尝试已过期')
    if loads(job.external_ref, {}).get('state') != 'done':
        job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    result = accept(db, job, payload.operation_hash, payload.result)
    db.commit()
    return result


@router.get('/local/{worker_id}/jobs/{job_id}/catalog-ozon-import-snapshot')
def catalog_ozon_import_snapshot(worker_id: str, job_id: str, request: Request,
                                 db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from fastapi.responses import JSONResponse
    from erp.api.routes.workers import _leased_job
    from erp.services.catalog_personal_submit import snapshot
    local_job(worker_id, job_id, request, db)
    job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    return JSONResponse(snapshot(db, job), headers={'Cache-Control': 'no-store'})


@router.post('/local/{worker_id}/jobs/{job_id}/catalog-ozon-import-intent')
def catalog_ozon_import_intent(worker_id: str, job_id: str, payload: CatalogOzonImportIntent,
                               request: Request, db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.catalog_personal_submit import intent
    local_job(worker_id, job_id, request, db)
    job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    result = intent(db, job, payload.item_hash)
    db.commit()
    return result


@router.post('/local/{worker_id}/jobs/{job_id}/catalog-ozon-import-task')
def catalog_ozon_import_task(worker_id: str, job_id: str, payload: CatalogOzonImportTask,
                             request: Request, db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.catalog_personal_submit import accept_task
    local_job(worker_id, job_id, request, db)
    job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    result = accept_task(db, job, payload.item_hash, payload.task_id)
    db.commit()
    return result


@router.post('/local/{worker_id}/jobs/{job_id}/catalog-ozon-import-result')
def catalog_ozon_import_result(worker_id: str, job_id: str, payload: CatalogOzonImportResult,
                               request: Request, db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.catalog_personal_submit import accept_import
    local_job(worker_id, job_id, request, db)
    job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    result = accept_import(db, job, payload.item_hash, payload.task_id, payload.result)
    db.commit()
    return result


@router.post('/local/{worker_id}/jobs/{job_id}/catalog-ozon-import-finish')
def catalog_ozon_import_finish(worker_id: str, job_id: str, payload: WorkerJobResult,
                               request: Request, db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.catalog_personal_submit import finish
    job = local_job(worker_id, job_id, request, db)
    if job.attempts != x_worker_attempt:
        raise ConflictError('建品完成回执的任务尝试已过期')
    if not loads(job.external_ref, {}).get('finished_hash'):
        job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    result = finish(db, job, payload.result)
    db.commit()
    return result


@router.get('/local/{worker_id}/jobs/{job_id}/catalog-ozon-snapshot/{store_id}')
def catalog_ozon_snapshot(worker_id: str, job_id: str, store_id: str, request: Request,
                          db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from fastapi.responses import JSONResponse
    from erp.api.routes.workers import _leased_job
    from erp.services.catalog_personal_publications import snapshot
    local_job(worker_id, job_id, request, db)
    job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    value = snapshot(db, job, store_id)
    from erp.services.publication_options import require_options_client
    require_options_client(value.get('requirements', {}), request.headers.get('X-ERP-Client-Version',''))
    return JSONResponse(value, headers={'Cache-Control': 'no-store'})


@router.post('/local/{worker_id}/jobs/{job_id}/catalog-ozon-prepared/{store_id}')
def catalog_ozon_prepared(worker_id: str, job_id: str, store_id: str,
                          payload: CatalogOzonPrepared, request: Request,
                          db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.catalog_personal_publications import accept_prepared
    local_job(worker_id, job_id, request, db)
    job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    result = accept_prepared(db, job, store_id, payload.revision, payload.result)
    db.commit()
    return result


class CatalogOzonCandidate(BaseModel):
    revision: str = Field(min_length=64, max_length=64)
    candidate: dict


class CatalogOzonCopyRevision(BaseModel):
    revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_candidate_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_content_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    content: dict


@router.post('/local/{worker_id}/jobs/{job_id}/catalog-ozon-copy-revision/{store_id}')
def catalog_ozon_copy_revision(worker_id: str, job_id: str, store_id: str,
                               payload: CatalogOzonCopyRevision, request: Request,
                               db: Session = Depends(get_db)):
    from erp.services.catalog_personal_publications import revise_pending_copy
    job = local_job(worker_id, job_id, request, db)
    result = revise_pending_copy(db, job, store_id, request.state.user['id'], worker_id,
        payload.revision, payload.expected_candidate_hash, payload.expected_content_hash, payload.content)
    db.commit()
    return result


@router.post('/local/{worker_id}/jobs/{job_id}/catalog-ozon-candidate/{store_id}')
def catalog_ozon_candidate(worker_id: str, job_id: str, store_id: str,
                           payload: CatalogOzonCandidate, request: Request,
                           db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.catalog_personal_publications import accept_review_candidate
    local_job(worker_id, job_id, request, db)
    job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    result = accept_review_candidate(db, job, store_id, payload.revision, payload.candidate)
    db.commit()
    return result


@router.get('/local/{worker_id}/jobs/{job_id}/store-test-snapshot')
def store_test_snapshot(worker_id: str, job_id: str, request: Request,
                        db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from fastapi.responses import JSONResponse
    from erp.api.routes.workers import _leased_job
    from erp.services.config_resolution import resolve_job_snapshot
    job = local_job(worker_id, job_id, request, db)
    if job.type != 'store_connection_test':
        raise HTTPException(409, '不是店铺连接测试任务')
    _leased_job(db, worker_id, job_id, x_worker_attempt)
    config = resolve_job_snapshot(job.config_snapshot_json)
    if config.get('store_id') != loads(job.payload_json, {}).get('store_id') or config.get('platform') != 'ozon':
        raise ConflictError('店铺连接快照与任务不一致')
    return JSONResponse(config, headers={'Cache-Control': 'no-store'})


@router.get('/local/{worker_id}/jobs/{job_id}/catalog-ozon-options-snapshot')
def catalog_ozon_options_snapshot(worker_id: str, job_id: str, request: Request,
                        db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from fastapi.responses import JSONResponse
    from erp.api.routes.workers import _leased_job
    from erp.services.config_resolution import resolve_job_snapshot
    job = local_job(worker_id, job_id, request, db)
    if job.type != 'catalog_ozon_options':
        raise HTTPException(409, '不是店铺连接测试任务')
    _leased_job(db, worker_id, job_id, x_worker_attempt)
    if loads(job.payload_json,{}).get('kind') == 'listing_status':
        from erp.services.publications import resolve_publication_snapshot
        config = resolve_publication_snapshot(job.config_snapshot_json)
        config.pop('services',None)
    else:
        config = resolve_job_snapshot(job.config_snapshot_json)
    if config.get('store_id') != loads(job.payload_json, {}).get('store_id') or config.get('platform') != 'ozon':
        raise ConflictError('店铺连接快照与任务不一致')
    return JSONResponse(config, headers={'Cache-Control': 'no-store'})


@router.get('/local/{worker_id}/jobs/{job_id}/self-listing-config')
def self_listing_config(worker_id: str, job_id: str, request: Request,
                        db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from fastapi.responses import JSONResponse
    from erp.api.routes.workers import _leased_job
    from erp.services.config_resolution import resolve_job_snapshot

    job = local_job(worker_id, job_id, request, db)
    if job.type != 'self_listing_pipeline':
        raise HTTPException(409, '不是自主上架目录任务')
    _leased_job(db, worker_id, job_id, x_worker_attempt)
    snapshot = resolve_job_snapshot(job.config_snapshot_json)
    if (not isinstance(snapshot.get('connections'), dict)
            or not isinstance(snapshot.get('secrets'), dict)):
        raise ConflictError('自主上架连接快照无效')
    return JSONResponse(snapshot, headers={'Cache-Control': 'no-store'})


@router.get('/local/{worker_id}/jobs/{job_id}/ozon-ai-snapshot/{store_id}')
def ozon_ai_snapshot(worker_id: str, job_id: str, store_id: str, request: Request,
                     db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from fastapi.responses import JSONResponse
    from erp.api.routes.workers import _leased_job
    from erp.services.ozon_personal_ai import snapshot

    job = local_job(worker_id, job_id, request, db)
    if job.type != 'ozon_ai_prepare':
        raise HTTPException(409, '不是 Ozon 跟卖 AI 准备任务')
    _leased_job(db, worker_id, job_id, x_worker_attempt)
    value = snapshot(db, job, store_id)
    db.commit()
    return JSONResponse(value, headers={'Cache-Control': 'no-store'})


@router.post('/local/{worker_id}/jobs/{job_id}/ozon-ai-result/{store_id}')
def ozon_ai_result(worker_id: str, job_id: str, store_id: str, payload: OzonAiStoreResult,
                   request: Request, db: Session = Depends(get_db),
                   x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.ozon_personal_ai import accept_result

    job = local_job(worker_id, job_id, request, db)
    if job.type != 'ozon_ai_prepare':
        raise HTTPException(409, '不是 Ozon 跟卖 AI 准备任务')
    _leased_job(db, worker_id, job_id, x_worker_attempt)
    value = accept_result(db, job, store_id, revision=payload.revision, draft_data=payload.draft)
    db.commit()
    return value


@router.post('/local/{worker_id}/jobs/{job_id}/self-listing-artifact')
def self_listing_artifact(worker_id: str, job_id: str, payload: SelfListingArtifactFile,
                          request: Request, db: Session = Depends(get_db),
                          x_worker_attempt: int = Header(...)) -> dict:
    import base64
    import binascii
    import os
    import uuid
    import zipfile
    from erp.api.routes.workers import _leased_job
    from erp.core.config import get_settings
    from erp.models import Asset, JobArtifact
    from erp.services.assets import register_local_asset

    job = local_job(worker_id, job_id, request, db)
    if job.type != 'self_listing_pipeline':
        raise HTTPException(409, '不是自主上架目录任务')
    _leased_job(db, worker_id, job_id, x_worker_attempt)
    steps = set(loads(job.payload_json, {}).get('selected_steps') or [])
    expected_step = {'copywriting_xlsx': 'generate_copywriting',
                     'oss_original_xlsx': 'upload_original',
                     'oss_watermarked_xlsx': 'upload_watermarked'}[payload.role]
    if expected_step not in steps:
        raise ConflictError('表格不属于本次目录流水线')
    try:
        content = base64.b64decode(payload.content, validate=True)
    except binascii.Error as error:
        raise ConflictError('表格编码无效') from error
    if (not content or len(content) > 25 * 1024**2
            or hashlib.sha256(content).hexdigest() != payload.sha256):
        raise ConflictError('表格大小或校验值无效')
    directory = get_settings().data_dir / 'self_listing_artifacts' / job.id
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f'{payload.role}.xlsx'
    if target.exists():
        if _sha256_file(target) != payload.sha256:
            raise ConflictError('已保存的表格与回执不一致')
    else:
        temporary = directory / f'{uuid.uuid4().hex}.partial'
        try:
            with temporary.open('xb') as stream:
                stream.write(content)
            if not zipfile.is_zipfile(temporary):
                raise ConflictError('上传结果不是 Excel 文件')
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    existing = db.scalar(select(Asset).where(Asset.legacy_source == 'self_listing_pipeline',
                                            Asset.legacy_id == f'{job.id}:{payload.role}'))
    if existing and (existing.sha256 != payload.sha256 or existing.size_bytes != len(content)):
        raise ConflictError('此任务已有不同表格结果')
    asset = existing or register_local_asset(
        db, target, kind='spreadsheet', source='self_listing_pipeline',
        read_only=True, calculate_hash=True,
        metadata={'job_id': job.id, 'role': payload.role},
        legacy_source='self_listing_pipeline', legacy_id=f'{job.id}:{payload.role}')
    if not db.scalar(select(JobArtifact).where(JobArtifact.job_id == job.id,
                                                JobArtifact.asset_id == asset.id)):
        db.add(JobArtifact(job_id=job.id, asset_id=asset.id, role=payload.role))
    db.commit()
    return {'role': payload.role, 'asset_id': asset.id,
            'sha256': payload.sha256, 'bytes': len(content)}


@router.get('/local/{worker_id}/jobs/{job_id}/catalog-import-source')
def catalog_import_source(worker_id: str, job_id: str, request: Request,
                          db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.core.config import get_settings

    job = local_job(worker_id, job_id, request, db)
    if job.type != 'catalog_self_selected_import':
        raise HTTPException(409, '不是自主上架导入任务')
    _leased_job(db, worker_id, job_id, x_worker_attempt)
    run = db.get(CatalogImportRun, loads(job.payload_json, {}).get('import_run_id'))
    if not run or run.kind not in {'self_selected', 'self_selected_bundle'}:
        raise ConflictError('导入任务与工作簿不匹配')
    source = Path(run.source_path).resolve()
    directory = (get_settings().data_dir / 'imports').resolve()
    suffixes = {'.zip'} if run.kind == 'self_selected_bundle' else {'.xlsx', '.xlsm'}
    max_bytes = 2 * 1024**3 if run.kind == 'self_selected_bundle' else 20 * 1024 * 1024
    if source.parent != directory or not source.is_file() or source.suffix.casefold() not in suffixes:
        raise ConflictError('导入工作簿不存在或路径无效')
    if source.stat().st_size > max_bytes:
        raise ConflictError('导入源文件超过上限')
    digest = _sha256_file(source)
    if digest != loads(run.report_json, {}).get('source_sha256'):
        raise ConflictError('导入源文件已变化，拒绝执行')
    return FileResponse(source, media_type='application/octet-stream', filename=source.name,
                        headers={'Cache-Control': 'no-store', 'X-Content-SHA256': digest})


@router.get('/local/{worker_id}/jobs/{job_id}/catalog-import-oss')
def catalog_import_oss(worker_id: str, job_id: str, request: Request, response: Response,
                       db: Session = Depends(get_db), x_worker_attempt: int = Header(...)) -> dict:
    from erp.api.routes.workers import _leased_job
    from erp.core.security import vault
    job = local_job(worker_id, job_id, request, db)
    _leased_job(db, worker_id, job_id, x_worker_attempt)
    run = db.get(CatalogImportRun, loads(job.payload_json, {}).get('import_run_id'))
    if (job.type != 'catalog_self_selected_import' or not run
            or run.kind != 'self_selected_bundle' or run.dry_run):
        raise ConflictError('不是正式 ZIP 图片入库任务')
    connection = db.get(IntegrationConnection, loads(run.report_json, {}).get('oss_connection_id'))
    if (not connection or connection.provider != 'oss' or not connection.enabled
            or connection.name != 'OZON 自动上架 OSS'):
        raise ConflictError('OSS 配置不可用')
    response.headers['Cache-Control'] = 'no-store'
    return {'id': connection.id, 'name': connection.name, 'provider': connection.provider,
            'base_url': connection.base_url, 'config_json': connection.config_json,
            'secrets': vault.decrypt(connection.secrets_encrypted), 'enabled': True}


@router.post('/local/{worker_id}/jobs/{job_id}/catalog-import-result')
def catalog_import_result(worker_id: str, job_id: str, payload: CatalogImportFile,
                          request: Request, db: Session = Depends(get_db),
                          x_worker_attempt: int = Header(...)) -> dict:
    import base64
    import binascii
    import gzip
    import io
    import json
    from erp.api.routes.workers import _leased_job
    from erp.services.catalog_personal_import import accept_workbook_rows

    job = local_job(worker_id, job_id, request, db)
    if job.type != 'catalog_self_selected_import':
        raise HTTPException(409, '不是自主上架导入任务')
    previous = loads(job.external_ref, {})
    if job.status == 'completed':
        if previous.get('kind') != 'catalog_import' or previous.get('sha256') != payload.sha256:
            raise ConflictError('自主上架导入已完成，拒绝不同结果')
        return previous['summary']
    _leased_job(db, worker_id, job_id, x_worker_attempt)
    run = db.get(CatalogImportRun, loads(job.payload_json, {}).get('import_run_id'))
    if not run or run.kind not in {'self_selected', 'self_selected_bundle'} or run.dry_run:
        raise ConflictError('不是自主上架正式导入')
    source = Path(run.source_path).resolve()
    from erp.core.config import get_settings
    allowed_suffixes = {'.zip'} if run.kind == 'self_selected_bundle' else {'.xlsx', '.xlsm'}
    max_bytes = 2 * 1024**3 if run.kind == 'self_selected_bundle' else 20 * 1024 * 1024
    if (source.parent != (get_settings().data_dir / 'imports').resolve()
            or not source.is_file() or source.suffix.casefold() not in allowed_suffixes
            or source.stat().st_size > max_bytes):
        raise ConflictError('导入工作簿路径或大小无效')
    expected = _sha256_file(source)
    if expected != loads(run.report_json, {}).get('source_sha256'):
        raise ConflictError('导入源文件已变化，拒绝写入')
    try:
        packed = base64.b64decode(payload.content, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ConflictError('自主上架结果编码无效') from error
    if not packed or len(packed) > 25 * 1024 * 1024 or hashlib.sha256(packed).hexdigest() != payload.sha256:
        raise ConflictError('自主上架结果校验失败')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
            raw = stream.read(80_000_001)
        if len(raw) > 80_000_000:
            raise ValueError('too large')
        value = json.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError) as error:
        raise ConflictError('自主上架结果解压或解析失败') from error
    if not isinstance(value, dict) or value.get('source_sha256') != expected:
        raise ConflictError('自主上架结果与工作簿不一致')
    summary = accept_workbook_rows(db, run, value)
    now = utc_now()
    for step in job.steps:
        step.status = 'completed'; step.progress = 100
        step.started_at = step.started_at or job.started_at or now
        step.finished_at = now; step.details_json = dumps(summary)
    job.status = 'completed'; job.progress = 100; job.finished_at = now
    job.lease_owner = ''; job.lease_expires_at = None
    job.error_code = ''; job.error_message = ''
    job.external_ref = dumps({'kind': 'catalog_import', 'sha256': payload.sha256,
                              'source_sha256': expected, 'summary': summary})
    db.add(JobEvent(job_id=job.id, message='原电脑自主上架结果已合并至共享产品库', data_json=dumps(summary)))
    db.commit()
    return summary


@router.post('/local/{worker_id}/jobs/{job_id}/pod-import-result')
def pod_import_result(worker_id: str, job_id: str, payload: CatalogImportFile,
                      request: Request, db: Session = Depends(get_db),
                      x_worker_attempt: int = Header(...)) -> dict:
    import base64
    import binascii
    import gzip
    import io
    import json
    from erp.api.routes.workers import _leased_job
    from erp.services.pod_personal_import import accept_pod_history

    job = local_job(worker_id, job_id, request, db)
    if job.type != 'catalog_pod_import':
        raise HTTPException(409, '不是 POD 历史导入任务')
    previous = loads(job.external_ref, {})
    if job.status == 'completed':
        if previous.get('kind') != 'pod_import' or previous.get('sha256') != payload.sha256:
            raise ConflictError('POD 历史导入已完成，拒绝不同结果')
        return previous['summary']
    _leased_job(db, worker_id, job_id, x_worker_attempt)
    run = db.get(CatalogImportRun, loads(job.payload_json, {}).get('import_run_id'))
    if not run or run.kind != 'pod_history' or run.dry_run:
        raise ConflictError('不是 POD 历史正式导入')
    try:
        packed = base64.b64decode(payload.content, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ConflictError('POD 历史回执编码无效') from error
    if not packed or len(packed) > 25 * 1024**2 or hashlib.sha256(packed).hexdigest() != payload.sha256:
        raise ConflictError('POD 历史回执校验失败')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
            raw = stream.read(120_000_001)
        if len(raw) > 120_000_000:
            raise ValueError('too large')
        value = json.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError) as error:
        raise ConflictError('POD 历史回执解压或解析失败') from error
    summary = accept_pod_history(db, run, value)
    now = utc_now()
    for step in job.steps:
        step.status = 'completed'; step.progress = 100
        step.started_at = step.started_at or job.started_at or now
        step.finished_at = now; step.details_json = dumps(summary)
    job.status = 'completed'; job.progress = 100; job.finished_at = now
    job.lease_owner = ''; job.lease_expires_at = None
    job.error_code = ''; job.error_message = ''
    job.external_ref = dumps({'kind': 'pod_import', 'sha256': payload.sha256,
                              'source_sha256': value['source_sha256'], 'summary': summary})
    db.add(JobEvent(job_id=job.id, message='原电脑 POD 历史资料已增量合并', data_json=dumps(summary)))
    db.commit()
    return summary


@router.get('/local/{worker_id}/jobs/{job_id}/supplier-gallery-snapshot')
def supplier_gallery_snapshot(worker_id:str,job_id:str,request:Request,
                              db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.domains.pod.integrations.common import load_supplier_connection
    job=local_job(worker_id,job_id,request,db)
    if job.type!='pod_supplier_gallery_sync':raise HTTPException(409,'不是供应商图库同步任务')
    _leased_job(db,worker_id,job_id,x_worker_attempt)
    payload=loads(job.payload_json,{})
    targets=payload.get('targets')
    if not isinstance(targets,list):raise ConflictError('供应商图库任务缺少冻结清单')
    config=None
    if any(row.get('supplier')=='hicustom' for row in targets):
        connection=load_supplier_connection(db,'hicustom',str((payload.get('connection_ids') or {}).get('hicustom') or ''))
        config={'provider':connection.provider,'connection_id':connection.connection_id,
                'base_url':connection.base_url,'config':connection.config,'secrets':connection.secrets}
    db.commit()
    from fastapi.responses import JSONResponse
    return JSONResponse({'targets':targets,'connection':config},headers={'Cache-Control':'no-store'})


@router.post('/local/{worker_id}/jobs/{job_id}/supplier-gallery-result')
def supplier_gallery_result(worker_id:str,job_id:str,payload:dict,request:Request,
                            db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.domains.pod.gallery import accept_personal_gallery_sync
    job=local_job(worker_id,job_id,request,db)
    if job.type!='pod_supplier_gallery_sync':raise HTTPException(409,'不是供应商图库同步任务')
    facts=payload.get('facts')
    if not isinstance(facts,list):raise ConflictError('供应商图库核对结果无效')
    digest=hashlib.sha256(dumps({'facts':facts}).encode('utf-8')).hexdigest()
    prior=loads(job.external_ref,{})
    if job.status=='completed':
        if (job.attempts!=x_worker_attempt or prior.get('kind')!='supplier_gallery_sync'
                or prior.get('sha256')!=digest):
            raise ConflictError('供应商图库同步已完成，拒绝不同结果')
        return prior['summary']
    _leased_job(db,worker_id,job_id,x_worker_attempt)
    summary=accept_personal_gallery_sync(db,job,payload)
    now=utc_now()
    for step in job.steps:
        step.status='completed';step.progress=100
        step.started_at=step.started_at or job.started_at or now
        step.finished_at=now;step.details_json=dumps(summary)
    job.status='completed';job.progress=100;job.finished_at=now
    job.lease_owner='';job.lease_expires_at=None
    job.external_ref=dumps({'kind':'supplier_gallery_sync','sha256':digest,'summary':summary})
    job.error_code='';job.error_message=''
    db.add(JobEvent(job_id=job.id,message='原电脑已核对供应商图库并保存共享结果',data_json=dumps(summary)))
    db.commit()
    return summary


@router.get('/local/{worker_id}/jobs/{job_id}/supplier-catalog-snapshot')
def supplier_catalog_snapshot(worker_id:str,job_id:str,request:Request,
                              db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.domains.pod.integrations.common import load_supplier_connection
    from erp.core.errors import AppError
    job=local_job(worker_id,job_id,request,db)
    if job.type not in {'pod_supplier_catalog_sync','pod_supplier_us_catalog_sync','pod_supplier_us_freight_quote'}:raise HTTPException(409,'不是供应商货盘任务')
    _leased_job(db,worker_id,job_id,x_worker_attempt)
    payload=loads(job.payload_json,{})
    sources={}
    for supplier in (('s2b',) if job.type in {'pod_supplier_us_catalog_sync','pod_supplier_us_freight_quote'} else ('hicustom','s2b')):
        try:
            connection=load_supplier_connection(db,supplier,str((payload.get('connection_ids') or {}).get(supplier) or ''))
            sources[supplier]={'connection':{'provider':connection.provider,
                'connection_id':connection.connection_id,'base_url':connection.base_url,
                'config':connection.config,'secrets':connection.secrets},'error':''}
        except AppError as error:
            sources[supplier]={'connection':None,'error':str(error)}
    db.commit()
    from fastapi.responses import JSONResponse
    return JSONResponse({'base_snapshot_id':payload.get('base_snapshot_id'),'sources':sources,
                         **({'request':payload} if job.type in {'pod_supplier_us_catalog_sync','pod_supplier_us_freight_quote'} else {})},
                        headers={'Cache-Control':'no-store'})


@router.post('/local/{worker_id}/jobs/{job_id}/supplier-catalog-result')
def supplier_catalog_result(worker_id:str,job_id:str,payload:dict,request:Request,
                            db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    import base64,binascii,gzip,io,json
    from erp.api.routes.workers import _leased_job
    from erp.domains.pod.supplier_catalog import accept_personal_catalog
    job=local_job(worker_id,job_id,request,db)
    if job.type not in {'pod_supplier_catalog_sync','pod_supplier_us_catalog_sync','pod_supplier_us_freight_quote'}:raise HTTPException(409,'不是供应商货盘任务')
    digest=payload.get('sha256')
    if not isinstance(digest,str) or not __import__('re').fullmatch('[a-f0-9]{64}',digest):
        raise ConflictError('供应商货盘结果校验值无效')
    prior=loads(job.external_ref,{})
    if job.status=='completed':
        if (job.attempts!=x_worker_attempt or prior.get('kind')!='supplier_catalog_sync'
                or prior.get('sha256')!=digest):
            raise ConflictError('供应商货盘已完成，拒绝不同结果')
        return prior['summary']
    _leased_job(db,worker_id,job_id,x_worker_attempt)
    content=payload.get('content')
    if not isinstance(content,str) or len(content)>14_000_000:
        raise ConflictError('供应商货盘文件编码无效')
    try:packed=base64.b64decode(content,validate=True)
    except (ValueError,binascii.Error):raise ConflictError('供应商货盘文件编码无效')
    if (not packed or len(packed)>10_000_000
            or hashlib.sha256(packed).hexdigest()!=digest):
        raise ConflictError('供应商货盘文件校验失败')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(packed)) as source:
            raw=source.read(30_000_001)
        if len(raw)>30_000_000:raise ValueError('too large')
        result=json.loads(raw)
    except (OSError,ValueError,UnicodeDecodeError):
        raise ConflictError('供应商货盘结果无法解压或解析')
    if not isinstance(result,dict):raise ConflictError('供应商货盘结果无效')
    if job.type=='pod_supplier_us_freight_quote':
        from erp.domains.pod.us_freight import accept
        summary=accept(db,job,result)
    elif job.type=='pod_supplier_us_catalog_sync':
        from erp.domains.pod.us_supplier_catalog import accept
        summary=accept(db,job,result)
    else:
        summary=accept_personal_catalog(db,job,result)
    now=utc_now()
    for step in job.steps:
        step.status='completed';step.progress=100
        step.started_at=step.started_at or job.started_at or now
        step.finished_at=now;step.details_json=dumps(summary)
    job.status='completed';job.progress=100;job.finished_at=now
    job.lease_owner='';job.lease_expires_at=None
    job.external_ref=dumps({'kind':'supplier_catalog_sync','sha256':digest,'summary':summary})
    job.error_code='';job.error_message=''
    db.add(JobEvent(job_id=job.id,message='原电脑供应商货盘已验收并保存共享快照',data_json=dumps(summary)))
    db.commit()
    return summary


@router.post('/local/{worker_id}/jobs/{job_id}/heartbeat')
def local_heartbeat(worker_id:str,job_id:str,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    local_job(worker_id,job_id,request,db)
    from erp.api.routes.workers import job_heartbeat
    return job_heartbeat(worker_id,job_id,db,x_worker_attempt)


@router.post('/local/{worker_id}/jobs/{job_id}/complete')
def local_complete(worker_id:str,job_id:str,payload:WorkerJobResult,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    job=local_job(worker_id,job_id,request,db)
    if job.type.startswith('amazon_'):
        if job.status=='completed':
            if job.attempts!=x_worker_attempt or not job.steps or loads(job.steps[0].details_json,{})!=payload.result:
                raise ConflictError('Amazon 完成回执与已记录结果不符')
            return job_dict(job)
        receipt=loads(job.external_ref,{})
        if not receipt.get('amazon_result_hash') or payload.result.get('amazon')!=receipt.get('summary'):
            raise ConflictError('请先验收 Amazon 业务回执，不能仅标记任务完成')
    if job.type in {'pod_supplier_us_catalog_sync','pod_supplier_us_freight_quote'}:
        recorded=loads(job.external_ref,{})
        if (job.status!='completed' or job.attempts!=x_worker_attempt
                or recorded.get('kind')!='supplier_catalog_sync' or recorded.get('summary')!=payload.result):
            raise ConflictError('美国货盘必须先验收共享快照，不能仅标记任务完成')
    if job.type in {'catalog_ozon_publication','catalog_ozon_followup'}:
        raise ConflictError('Ozon 平台阶段必须使用绑定原操作的专用完成回执')
    if job.type == 'catalog_ozon_prepare' and job.status != 'completed':
        from erp.api.routes.workers import _leased_job
        from erp.services.catalog_personal_publications import completion_summary
        _leased_job(db, worker_id, job_id, x_worker_attempt)
        if payload.result != completion_summary(job):
            raise ConflictError('产品库 Ozon 准备完成清单与逐店回执不一致')
    if job.type == 'catalog_ozon_options':
        if job.status == 'completed':
            recorded = loads(job.steps[0].details_json,{}) if job.steps else {}
            if recorded != payload.result:raise ConflictError('店铺读取已有不同回执')
            return job_dict(job)
        from erp.api.routes.workers import _leased_job
        _leased_job(db,worker_id,job_id,x_worker_attempt)
        expected = loads(job.payload_json, {})
        value = payload.result
        from erp.services.catalog_personal_publications import _public_result
        if (set(value) != {'store_id','kind','items'} or value.get('store_id') != expected.get('store_id')
                or value.get('kind') != expected.get('kind') or not isinstance(value.get('items'), list)
                or len(value['items']) > 10000 or len(dumps(value).encode()) > 2 * 1024**2):
            raise ConflictError('店铺资料回执与原任务不一致')
        _public_result(value)
        from erp.services.ozon_categories import save_category_snapshot, save_options_cache
        if value['kind'] == 'listing_status':
            from erp.services.catalog_ozon_stock_scan import accept
            summary = accept(db,job,value['items'])
            job.external_ref = dumps({'stock_scan_summary':summary})
            db.flush()  # The second lease validation refreshes Job; keep the summary in the same transaction.
        elif value['kind'] == 'category_tree':
            from erp.models import Store
            store = db.get(Store, value['store_id'])
            save_category_snapshot(store.id, store.name, 'ZH_HANS', value['items'])
        else:
            save_options_cache(value['store_id'], value['kind'], expected.get('params', {}), value['items'])
    if job.type == 'store_connection_test' and job.status != 'completed':
        from erp.api.routes.workers import _leased_job
        _leased_job(db, worker_id, job_id, x_worker_attempt)
        value = payload.result
        if (set(value) != {'ok', 'store_id', 'category_roots'} or value.get('ok') is not True
                or value.get('store_id') != loads(job.payload_json, {}).get('store_id')
                or type(value.get('category_roots')) is not int or not 0 <= value['category_roots'] <= 10000):
            raise ConflictError('店铺连接测试回执无效')
    if job.type == 'ozon_ai_prepare' and job.status != 'completed':
        from erp.api.routes.workers import _leased_job
        from erp.services.ozon_personal_ai import completion_summary

        _leased_job(db, worker_id, job_id, x_worker_attempt)
        if payload.result != completion_summary(job):
            raise ConflictError('Ozon 跟卖 AI 完成清单与逐店回执不一致')
    if job.type == 'self_listing_pipeline' and job.status != 'completed':
        from erp.api.routes.workers import _leased_job
        from erp.models import Asset, JobArtifact
        _leased_job(db, worker_id, job_id, x_worker_attempt)
        value = payload.result
        expected_steps = [step.key for step in sorted(job.steps, key=lambda item: item.position)]
        expected_roles = {'generate_copywriting': 'copywriting_xlsx',
                          'upload_original': 'oss_original_xlsx',
                          'upload_watermarked': 'oss_watermarked_xlsx'}
        required_roles = {role for step, role in expected_roles.items() if step in expected_steps}
        if (not isinstance(value, dict) or set(value) != {'schema', 'completed_steps', 'artifacts'}
                or value['schema'] != 1 or value['completed_steps'] != expected_steps
                or not isinstance(value['artifacts'], list)
                or {item.get('role') for item in value['artifacts'] if isinstance(item, dict)} != required_roles
                or len(value['artifacts']) != len(required_roles)):
            raise ConflictError('目录流水线完成清单与原任务步骤不一致')
        for item in value['artifacts']:
            if (not isinstance(item, dict)
                    or set(item) != {'role', 'asset_id', 'sha256', 'bytes'}):
                raise ConflictError('目录流水线表格回执无效')
            asset = db.get(Asset, item['asset_id'])
            linked = db.scalar(select(JobArtifact).where(JobArtifact.job_id == job.id,
                                                          JobArtifact.asset_id == item['asset_id'],
                                                          JobArtifact.role == item['role']))
            if (not asset or not linked or asset.sha256 != item['sha256']
                    or asset.size_bytes != item['bytes']):
                raise ConflictError('目录流水线表格尚未入云或校验不一致')
    if job.type == 'catalog_pod_import' and job.status != 'completed':
        from erp.api.routes.workers import _leased_job
        _leased_job(db, worker_id, job_id, x_worker_attempt)
        run = db.get(CatalogImportRun, loads(job.payload_json, {}).get('import_run_id'))
        value = payload.result
        if run and not run.dry_run:
            receipt = loads(job.external_ref, {})
            if receipt.get('kind') != 'pod_import' or value != receipt.get('summary'):
                raise ConflictError('POD 历史正式回执与已接收结果不一致')
        else:
            required = {'counts', 'broken_current_versions', 'blank_skus',
                        'source_database', 'identity_sha256'}
            if (not run or run.kind != 'pod_history' or not run.dry_run
                    or not isinstance(value, dict) or set(value) != required
                    or not isinstance(value['source_database'], str)
                    or not isinstance(value['counts'], dict)
                    or set(value['counts']) != {'products', 'product_versions',
                                               'product_variants', 'product_oss_assets'}
                    or any(type(number) is not int or number < 0 or number > 1_000_000
                           for number in list(value['counts'].values())
                           + [value['broken_current_versions'], value['blank_skus']])
                    or not isinstance(value['identity_sha256'], dict)
                    or set(value['identity_sha256']) != {'products', 'product_variants',
                                                        'product_oss_assets'}
                    or any(not isinstance(digest, str) or len(digest) != 64
                           or any(char not in '0123456789abcdef' for char in digest)
                           for digest in value['identity_sha256'].values())):
                raise ConflictError('POD 历史预演回执无效')
            run.status = 'completed'
            run.counts_json = dumps(value['counts'])
            run.report_json = dumps(value)
    if job.type == 'catalog_self_selected_import' and job.status != 'completed':
        from erp.api.routes.workers import _leased_job
        _leased_job(db, worker_id, job_id, x_worker_attempt)
        run = db.get(CatalogImportRun, loads(job.payload_json, {}).get('import_run_id'))
        result = payload.result
        if not run or run.kind not in {'self_selected', 'self_selected_bundle'} or not run.dry_run or set(result) != {'sha256', 'rows', 'product_groups', 'local_images', 'errors'}:
            raise ConflictError('自主上架预演回执无效')
        source = Path(run.source_path)
        max_bytes = 2 * 1024**3 if run.kind == 'self_selected_bundle' else 20 * 1024 * 1024
        if (not source.is_file() or source.stat().st_size > max_bytes
                or result['sha256'] != _sha256_file(source)):
            raise ConflictError('自主上架预演文件与回执不一致')
        errors = result['errors']
        if (any(type(result[key]) is not int or result[key] < 0 or result[key] > 100_000
                for key in ('rows', 'product_groups', 'local_images'))
                or not isinstance(errors, list) or len(errors) > 5000
                or any(not isinstance(item, dict) or set(item) != {'row', 'field', 'message'}
                       or type(item['row']) is not int or item['row'] < 1
                       or not isinstance(item['field'], str) or len(item['field']) > 80
                       or not isinstance(item['message'], str) or len(item['message']) > 500
                       for item in errors)):
            raise ConflictError('自主上架预演数量或错误清单无效')
        run.counts_json = dumps({'rows': result['rows'], 'product_groups': result['product_groups'],
                                  'local_images': result['local_images'], 'errors': len(errors)})
        run.errors_json = dumps(errors)
        run.status = 'validation_failed' if errors else 'completed'
    if job.type in {'ad_optimization_analyze','ad_optimization_backtest','ad_optimization_sync','ad_optimization_reports','ad_optimization_preview','ad_optimization_execute','ad_optimization_reconcile'} and job.status!='completed':
        from erp.api.routes.workers import _leased_job
        if job.type == 'ad_optimization_sync':
            from erp.services.ad_sync_result import completion_summary
        elif job.type == 'ad_optimization_reports':
            from erp.services.ad_report_result import completion_summary
        elif job.type == 'ad_optimization_execute':
            from erp.services.ad_execution_result import completion_summary
        elif job.type == 'ad_optimization_reconcile':
            from erp.services.ad_reconcile_result import completion_summary
        else:
            from erp.services.ad_desktop_result import completion_summary
        _leased_job(db,worker_id,job_id,x_worker_attempt)
        if payload.result != completion_summary(job):
            raise ConflictError('广告分析完成回执与已接收结果不一致')
    if job.type=='pod_purchase_submit' and job.status!='completed':
        from erp.domains.orders.desktop_purchase_submit import completed_state
        leased_purchase_submit(worker_id,job_id,request,db,x_worker_attempt)
        if payload.result!=completed_state(db,job):raise ConflictError('采购完成摘要不一致')
    if job.type=='pod_purchase_sync' and job.status!='completed':
        from erp.api.routes.workers import _leased_job
        from erp.domains.orders.desktop_purchase import completion_summary
        _leased_job(db,worker_id,job_id,x_worker_attempt)
        if payload.result!=completion_summary(job):raise HTTPException(409,'采购状态完成摘要不一致')
    if job.type in {'pod_order_sync','pod_order_image_sync'} and job.status!='completed':
        from erp.api.routes.workers import _leased_job
        from erp.domains.orders.desktop import completion_summary
        _leased_job(db,worker_id,job_id,x_worker_attempt)
        if payload.result != completion_summary(job):
            raise ConflictError('订单完成回执与已接收步骤不一致')
    if job.type in {'pod_wb_excel_export','pod_wb_api_publish'} and job.status!='completed':
        from erp.api.routes.workers import _leased_job
        from erp.domains.pod.desktop_wb import accept_result, accept_api_result
        _leased_job(db,worker_id,job_id,x_worker_attempt)
        (accept_api_result if job.type=='pod_wb_api_publish' else accept_result)(db,job,payload.result)
    if job.type=='pod_wb_excel_reconcile' and job.status!='completed':
        from erp.api.routes.workers import _leased_job
        from erp.domains.pod.desktop_wb import accept_reconcile_result
        _leased_job(db,worker_id,job_id,x_worker_attempt)
        accept_reconcile_result(db,job,payload.result)
    if job.type=='pod_wb_history_reconcile' and job.status!='completed':
        from erp.api.routes.workers import _leased_job
        from erp.domains.pod.wb_history_transport import accept
        _leased_job(db,worker_id,job_id,x_worker_attempt)
        accept(db,job,payload.result)
    if job.type=='pod_wb_stock' and job.status!='completed':
        from erp.api.routes.workers import _leased_job
        from erp.domains.pod.desktop_wb import accept_stock_result
        _leased_job(db,worker_id,job_id,x_worker_attempt)
        accept_stock_result(db,job,payload.result)
    if job.type=='pod_wb_price' and job.status!='completed':
        from erp.api.routes.workers import _leased_job
        from erp.domains.pod.desktop_wb import accept_price_result
        _leased_job(db,worker_id,job_id,x_worker_attempt)
        accept_price_result(db,job,payload.result)
        # complete_job rechecks and refreshes the lease row. Preserve the verified
        # recovery metadata within this same transaction before that refresh.
        db.flush()
    if job.type.startswith('pod_ai_design_') and job.status!='completed':
        from erp.api.routes.workers import _leased_job
        from erp.domains.pod.ai_design.cloud import accept
        _leased_job(db,worker_id,job_id,x_worker_attempt)
        accept(db,job,payload.result)
    if job.type=='pod_artwork_upload' and job.status!='completed':
        from erp.api.routes.workers import _leased_job
        from erp.domains.pod.desktop_artwork import accept_result
        _leased_job(db,worker_id,job_id,x_worker_attempt)
        accept_result(db,job,payload.result)
    if job.type in {'pod_native_composition','pod_us_composition'} and job.status!='completed':
        from erp.api.routes.workers import _leased_job
        from erp.domains.pod.desktop_composition import accept_result
        job=_leased_job(db,worker_id,job_id,x_worker_attempt)
        accept_result(db,job,payload.result)
    if job.type=='pod_library_restore':
        expected={item['id']:item for item in loads(job.payload_json,{})['files']}
        rows=payload.result.get('files',[])
        if not isinstance(rows,list) or len(rows)!=len(expected) or any(not isinstance(r,dict) or not isinstance(r.get('id'),str) for r in rows) or {r.get('id') for r in rows}!=set(expected):
            raise ConflictError('恢复结果未覆盖全部文件')
        import re
        for row in rows:
            source=expected[row['id']]
            if not re.fullmatch('[a-f0-9]{64}',str(row.get('sha256',''))) or type(row.get('bytes')) is not int or row['bytes']<=0 or (source['sha256'] and source['sha256']!=row['sha256']) or (source['size_bytes'] and source['size_bytes']!=row['bytes']):
                raise ConflictError('恢复结果校验不一致')
    if job.status=='completed' and job.attempts==x_worker_attempt:
        if all(loads(step.details_json,{})==payload.result for step in job.steps):return job_dict(job)
        raise ConflictError('该任务已完成，拒绝不同的重复结果')
    from erp.api.routes.workers import complete_job
    result=complete_job(worker_id,job_id,payload,db,x_worker_attempt)
    if job.type=='pod_wb_history_reconcile':
        from erp.domains.pod.wb_history_transport import cleanup
        try:cleanup(job.id)
        except OSError:pass
    if job.type in {'ad_optimization_analyze','ad_optimization_backtest','ad_optimization_sync','ad_optimization_reports','ad_optimization_preview','ad_optimization_execute','ad_optimization_reconcile'}:
        from erp.services.ad_desktop_snapshot import cleanup_completed_snapshot
        try:cleanup_completed_snapshot(job.id)
        except OSError:pass
        if job.type=='ad_optimization_sync':
            from erp.services.ad_sync_transport import cleanup
            try:cleanup(job.id)
            except OSError:pass
    if job.type=='pod_artwork_upload':
        from erp.domains.pod.desktop_artwork import staged_path
        try:staged_path(job).unlink(missing_ok=True)
        except (OSError,ConflictError):pass
    return result


@router.post('/local/{worker_id}/jobs/{job_id}/artwork-snapshot')
def artwork_snapshot(worker_id:str,job_id:str,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from fastapi.responses import JSONResponse
    from erp.api.routes.workers import _leased_job
    from erp.domains.pod.desktop_artwork import snapshot
    job=local_job(worker_id,job_id,request,db)
    if job.type!='pod_artwork_upload':raise HTTPException(409,'不是图库上传任务')
    _leased_job(db,worker_id,job_id,x_worker_attempt)
    value=snapshot(db,job);db.commit()
    return JSONResponse(value,headers={'Cache-Control':'no-store'})


@router.get('/local/{worker_id}/jobs/{job_id}/artwork-source')
def artwork_source(worker_id:str,job_id:str,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from fastapi.responses import FileResponse
    from erp.api.routes.workers import _leased_job
    from erp.domains.pod.desktop_artwork import staged_path
    job=local_job(worker_id,job_id,request,db)
    if job.type!='pod_artwork_upload':raise HTTPException(409,'不是图库上传任务')
    _leased_job(db,worker_id,job_id,x_worker_attempt)
    path=staged_path(job);db.commit()
    return FileResponse(path,media_type='application/octet-stream',headers={'Cache-Control':'no-store'})


@router.post('/local/{worker_id}/jobs/{job_id}/fail')
def local_fail(worker_id:str,job_id:str,payload:WorkerJobFailure,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    job=local_job(worker_id,job_id,request,db)
    if payload.error_code=='codex_output_missing' and loads(job.payload_json,{}).get('generation_provider')=='codex':
        from erp.api.routes.workers import _leased_job
        _leased_job(db,worker_id,job_id,x_worker_attempt)
        job.status='paused';job.pause_requested=True;job.lease_owner='';job.lease_expires_at=None
        job.error_code='';job.error_message=''
        if job.type=='amazon_prepare_codex':
            from erp.models import AmazonListingBatch
            batch=db.get(AmazonListingBatch,loads(job.payload_json,{})['batch_id'])
            if batch:batch.status='waiting_codex'
        db.add(JobEvent(job_id=job.id,message='等待 Codex 回填结果，不会回退到外部生成模型'))
        db.commit();return job_dict(job)
    if loads(job.payload_json,{}).get('batch_phase'):
        from erp.services.catalog_ozon_batch import targets
        phase,rows=targets(db,job)
        if loads(job.external_ref,{}).get('state')=='pending':
            for _,child,_ in rows:
                if phase=='submit':child.status='import_uncertain'
                else:setattr(child,phase+'_status','unknown')
                child.last_error=payload.error_message[:1000]
    if job.type == 'catalog_ozon_followup':
        from erp.api.routes.workers import _leased_job
        from erp.services.catalog_personal_followup import target
        _leased_job(db,worker_id,job_id,x_worker_attempt)
        publication, child, value = target(db, job)
        if loads(job.external_ref, {}).get('state') == 'pending':
            setattr(child, value['phase'] + '_status', 'unknown')
        child.last_error = payload.error_message[:1000]
    if job.type == 'catalog_self_selected_import':
        from erp.api.routes.workers import _leased_job
        _leased_job(db,worker_id,job_id,x_worker_attempt)
        run = db.get(CatalogImportRun, loads(job.payload_json, {}).get('import_run_id'))
        if run:
            run.status = 'failed'
            run.errors_json = dumps([payload.error_message[:500]])
    if job.type=='pod_wb_stock':
        from erp.api.routes.workers import _leased_job
        from erp.models import WbPublicationItem
        _leased_job(db,worker_id,job_id,x_worker_attempt)
        for item in db.scalars(select(WbPublicationItem).where(WbPublicationItem.id.in_(loads(job.payload_json,{})['ledger_ids']))):
            if item.status=='stock_processing':
                item.status='pending_stock' if payload.error_code=='wb_stock_rate_limited' else 'stock_failed';item.last_error=payload.error_message[:1000]
    if job.type=='pod_wb_price':
        from erp.api.routes.workers import _leased_job
        from erp.models import WbPublicationItem
        _leased_job(db,worker_id,job_id,x_worker_attempt)
        for item in db.scalars(select(WbPublicationItem).where(WbPublicationItem.id.in_(loads(job.payload_json,{})['ledger_ids']))):
            if item.status=='price_processing':
                item.status='pending_price' if payload.error_code=='wb_price_rate_limited' else 'price_failed'
                item.last_error=payload.error_message[:1000]
    if job.type.startswith('pod_ai_design_'):
        from erp.api.routes.workers import _leased_job
        from erp.domains.pod.ai_design.cloud import row
        _leased_job(db,worker_id,job_id,x_worker_attempt)
        stored=row(db,loads(job.payload_json,{})['batch_id']);value=loads(stored.value_json,{})
        value['status']='failed';stored.value_json=dumps(value)
    if job.type in {'pod_native_composition','pod_us_composition'}:
        from erp.api.routes.workers import _leased_job
        from erp.domains.pod.models import PodComposition
        _leased_job(db,worker_id,job_id,x_worker_attempt)
        item=db.get(PodComposition,loads(job.payload_json,{})['composition_id'])
        item.status='failed';item.error_message=payload.error_message
        item.outcome_unknown=payload.error_code=='pod_write_outcome_unknown'
    from erp.api.routes.workers import fail_job
    return fail_job(worker_id,job_id,payload,db,x_worker_attempt)


@router.post('/local/{worker_id}/jobs/{job_id}/ai-snapshot')
def ai_snapshot(worker_id:str,job_id:str,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from fastapi.responses import JSONResponse
    from erp.api.routes.workers import _leased_job
    from erp.domains.pod.ai_design.cloud import snapshot,TYPES
    job=local_job(worker_id,job_id,request,db)
    if job.type not in TYPES:raise HTTPException(409,'不是AI任务')
    _leased_job(db,worker_id,job_id,x_worker_attempt)
    value=snapshot(db,job);db.commit();return JSONResponse(value,headers={'Cache-Control':'no-store'})


@router.get('/local/{worker_id}/jobs/{job_id}/ai-source/{identity}')
def ai_source(worker_id:str,job_id:str,identity:str,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from fastapi.responses import FileResponse
    from erp.api.routes.workers import _leased_job
    from erp.domains.pod.ai_design.cloud import source,TYPES
    job=local_job(worker_id,job_id,request,db)
    if job.type not in TYPES:raise HTTPException(409,'不是AI任务')
    _leased_job(db,worker_id,job_id,x_worker_attempt)
    path=source(db,job,identity);db.commit();return FileResponse(path,headers={'Cache-Control':'no-store'})


@router.post('/local/{worker_id}/jobs/{job_id}/composition-snapshot')
def composition_snapshot(worker_id:str,job_id:str,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from fastapi.responses import JSONResponse
    from erp.api.routes.workers import _leased_job
    from erp.domains.pod.desktop_composition import snapshot
    job=local_job(worker_id,job_id,request,db)
    if job.type not in {'pod_native_composition','pod_us_composition'}:raise HTTPException(409,'不是合成任务')
    _leased_job(db,worker_id,job_id,x_worker_attempt)
    value=snapshot(db,job);db.commit()
    return JSONResponse(value,headers={'Cache-Control':'no-store'})


@router.post('/local/{worker_id}/jobs/{job_id}/wb-progress')
@router.post('/local/{worker_id}/jobs/{job_id}/ai-progress')
@router.post('/local/{worker_id}/jobs/{job_id}/composition-progress')
def composition_progress(worker_id:str,job_id:str,payload:CompositionProgress,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from erp.api.routes.workers import _leased_job
    job=local_job(worker_id,job_id,request,db)
    if job.type not in {'pod_native_composition','pod_us_composition','pod_ai_design_plan','pod_ai_design_generate','pod_ai_design_upscale','pod_wb_excel_export','pod_wb_api_publish'}:raise HTTPException(409,'任务不支持分步进度')
    _leased_job(db,worker_id,job_id,x_worker_attempt)
    step=next((s for s in job.steps if s.key==payload.key),None)
    if not step:raise HTTPException(409,'任务步骤无效')
    step.status='completed' if payload.completed else 'running';step.progress=payload.progress
    step.started_at=step.started_at or utc_now()
    if payload.completed:step.finished_at=utc_now()
    job.progress=sum(s.progress for s in job.steps)/max(1,len(job.steps))
    db.commit();return {'ok':True}


@router.get('/local/{worker_id}/jobs/{job_id}/source/{asset_id}')
def composition_source(worker_id:str,job_id:str,asset_id:str,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from fastapi.responses import FileResponse
    from erp.api.routes.workers import _leased_job
    from erp.domains.pod.desktop_composition import source_path
    job=local_job(worker_id,job_id,request,db)
    if job.type not in {'pod_native_composition','pod_us_composition'}:raise HTTPException(409,'不是合成任务')
    _leased_job(db,worker_id,job_id,x_worker_attempt)
    path=source_path(db,job,asset_id);db.commit()
    return FileResponse(path,headers={'Cache-Control':'no-store'})


@router.post("/probe", status_code=202)
def probe(request: Request, db: Session = Depends(get_db)) -> dict:
    account_mode()
    job = enqueue_job(db, job_type="worker_probe", payload={"message": "本机连接检查"}, config_snapshot=dumps({}),
                      idempotency_key="device-check:" + uuid.uuid4().hex, steps=[("probe", "检查当前电脑执行连接")])
    return {"job_id": job.id, "target_worker_id": job.target_worker_id}


@router.get("")
def devices(request: Request, db: Session = Depends(get_db)) -> dict:
    account_mode()
    user = request.state.user
    query = select(WorkerNode).order_by(WorkerNode.name, WorkerNode.id)
    if user["role"] != "admin":
        query = query.where(WorkerNode.owner_user_id == user["id"])
    cutoff = utc_now().replace(tzinfo=None) - timedelta(seconds=90)
    return {"current_worker_id": user.get("worker_id", ""), "user_id": user["id"], "admin": user["role"] == "admin", "items": [
        {"id": row.id, "name": row.name, "owner_user_id": row.owner_user_id,
         "online": bool(row.last_seen_at and row.last_seen_at.replace(tzinfo=None) >= cutoff),
         "capabilities": loads(row.capabilities_json, [])} for row in db.scalars(query)
    ]}


@router.patch("/{worker_id}/owner")
def bind_owner(worker_id: str, payload: Owner, actor: dict = Depends(admin), db: Session = Depends(get_db)) -> dict:
    db.execute(update(WorkerNode).where(WorkerNode.id == worker_id).values(status=WorkerNode.status))
    device = db.get(WorkerNode, worker_id)
    if not device:
        raise NotFoundError("电脑尚未注册，请先连接本地执行端")
    user = db.get(ErpUser, payload.user_id) if payload.user_id else None
    if payload.user_id and (not user or not user.enabled):
        raise ConflictError("请选择启用的用户账号")
    if db.scalar(select(Job.id).where(Job.target_worker_id == worker_id, Job.status.in_(["queued", "running", "retrying", "paused"]))):
        raise ConflictError("该电脑仍有未结束任务，不能更换归属")
    device.owner_user_id = payload.user_id
    db.execute(update(ErpSession).where(ErpSession.worker_id == worker_id).values(worker_id=""))
    audit(db, actor, "device_bind", worker_id)
    db.commit()
    return {"ok": True}


@router.post("/select")
def select_device(payload: Selection, request: Request, db: Session = Depends(get_db)) -> dict:
    account_mode()
    device = db.get(WorkerNode, payload.worker_id)
    if not device or device.owner_user_id != request.state.user["id"]:
        raise ConflictError("只能选择绑定本人账号的电脑")
    digest = hashlib.sha256(request.cookies.get(COOKIE_NAME, "").encode()).hexdigest()
    updated = db.execute(update(ErpSession).where(ErpSession.token_hash == digest, ErpSession.user_id == request.state.user["id"]).values(worker_id=device.id)).rowcount
    if not updated:
        raise ConflictError("登录已失效，请重新登录")
    audit(db, request.state.user, "device_select", device.id)
    db.commit()
    return {"worker_id": device.id}


@router.get('/local/{worker_id}/jobs/{job_id}/catalog-ozon-batch/snapshot')
def batch_ozon_snapshot(worker_id: str, job_id: str, request: Request, db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.catalog_ozon_batch import snapshot
    local_job(worker_id,job_id,request,db)
    return snapshot(db,_leased_job(db,worker_id,job_id,x_worker_attempt))

@router.post('/local/{worker_id}/jobs/{job_id}/catalog-ozon-batch/{action}')
def batch_ozon_receipt(worker_id: str, job_id: str, action: str, payload: dict, request: Request, db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.services.catalog_ozon_batch import accept
    if action not in {'intent','task','result'}:raise ConflictError('批次操作无效')
    job=local_job(worker_id,job_id,request,db)
    if not (action=='result' and loads(job.external_ref,{}).get('state')=='done' and job.attempts==x_worker_attempt):
        job=_leased_job(db,worker_id,job_id,x_worker_attempt)
    result=accept(db,job,action,payload);db.commit();return result


@router.post('/local/{worker_id}/jobs/{job_id}/wb-price-progress')
def wb_price_progress(worker_id: str, job_id: str, payload: dict, request: Request, db: Session = Depends(get_db), x_worker_attempt: int = Header(...)):
    from erp.api.routes.workers import _leased_job
    from erp.domains.pod.desktop_wb import price_progress
    local_job(worker_id, job_id, request, db)
    job = _leased_job(db, worker_id, job_id, x_worker_attempt)
    result = price_progress(db, job, payload)
    db.commit()
    return result
