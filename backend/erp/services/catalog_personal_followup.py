"""Shared Ozon stock/promotion coordination; platform operations run on a PC."""
from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps, loads
from erp.models import Job, PublicationStore, utc_now
from erp.services.jobs import enqueue_job
from erp.services.publications import get_publication, publication_status, resolve_publication_snapshot
from erp.services.catalog_personal_publications import digest
from sqlalchemy import select


def enqueue(session, publication, child, phase, params):
    if phase not in {'stock', 'promotion'}:
        raise ConflictError('不支持的 Ozon 后续阶段')
    child = session.scalar(select(PublicationStore).where(PublicationStore.id == child.id).with_for_update()
                           .execution_options(populate_existing=True))
    if child.status != phase + '_pending' or getattr(child, phase + '_status') in {'processing', 'unknown'}:
        raise ConflictError('该阶段已在处理或结果未知，请先核对原任务')
    if phase == 'stock':
        from erp.services.catalog_ozon_stock_scan import require_fresh
        require_fresh(child)
    if phase == 'promotion' and not str(child.external_product_id).isdigit():
        raise ConflictError('Ozon 商品 ID 尚未回写')
    if phase=='promotion' and params.get('stock') is None:
        from erp.core.task_identity import task_identity
        from erp.models import WorkerNode
        device=session.get(WorkerNode,(task_identity.get() or {}).get('worker_id',''))
        if not device or 'ozon_promotion_unlimited' not in loads(device.capabilities_json,[]):
            raise ConflictError('无限促销数量需要将执行电脑更新至0.6.82并重新连接')
    payload = {'publication_id': publication.id, 'store_id': child.store_id, 'child_id': child.id,
               'offer_id': child.offer_id, 'product_id': child.external_product_id, 'phase': phase, 'params': params}
    job = enqueue_job(session, job_type='catalog_ozon_followup', payload=payload,
        config_snapshot=child.settings_snapshot_json,
        idempotency_key='catalog-followup:' + child.id + ':' + phase + ':' + __import__('uuid').uuid4().hex,
        steps=[(child.store_id + ':' + phase, child.store.name + ' · ' + ('添加库存' if phase == 'stock' else '添加促销'))],
        max_attempts=3, commit=False)
    setattr(child, phase + '_status', 'processing')
    session.commit()
    return job


def target(session, job):
    if job.type != 'catalog_ozon_followup' or not job.created_by or not job.target_worker_id:
        raise ConflictError('不是本人电脑的 Ozon 后续阶段任务')
    payload = loads(job.payload_json, {})
    publication = get_publication(session, payload.get('publication_id', ''))
    child = next((row for row in publication.stores if row.id == payload.get('child_id') and row.store_id == payload.get('store_id')), None)
    if (not child or child.offer_id != payload.get('offer_id') or child.external_product_id != payload.get('product_id')
            or payload.get('phase') not in {'stock', 'promotion'}):
        raise ConflictError('Ozon 阶段商品或店铺资料已变化')
    return publication, child, payload


def snapshot(session, job):
    publication, child, payload = target(session, job)
    return {'schema': 1, 'job_id': job.id, **payload, 'operation_hash': digest(payload),
            'config': resolve_publication_snapshot(job.config_snapshot_json)}


def intent(session, job, operation_hash):
    publication, child, payload = target(session, job)
    if digest(payload) != operation_hash:
        raise ConflictError('阶段快照已变化')
    prior = loads(job.external_ref, {})
    if prior:
        if prior.get('kind') != 'catalog_ozon_followup' or prior.get('operation_hash') != operation_hash:
            raise ConflictError('已有不同的操作意图')
        return prior
    if child.status != payload['phase'] + '_pending' or getattr(child, payload['phase'] + '_status') != 'processing':
        raise ConflictError('当前状态不能执行本次操作')
    value = {'kind': 'catalog_ozon_followup', 'operation_hash': operation_hash, 'state': 'pending'}
    job.external_ref = dumps(value)
    return value


def accept(session, job, operation_hash, result):
    publication, child, payload = target(session, job)
    prior = loads(job.external_ref, {})
    if prior.get('kind') != 'catalog_ozon_followup' or prior.get('operation_hash') != operation_hash or digest(payload) != operation_hash:
        raise ConflictError('没有匹配的阶段操作意图')
    if not isinstance(result, dict) or set(result) != {'ok', 'eligible'} or result.get('ok') is not True or type(result.get('eligible')) is not bool:
        raise ConflictError('阶段完成回执格式无效')
    if prior.get('state') == 'done':
        if prior.get('result_hash') != digest(result): raise ConflictError('原阶段已有不同完成回执')
        return {'job_id': job.id, 'status': job.status}
    phase = payload['phase']
    if child.status != phase + '_pending': raise ConflictError('商品已经不在本次操作阶段')
    if phase == 'stock':
        if not result['eligible']: raise ConflictError('库存回执不应包含促销候选状态')
        child.stock_status = 'completed'; child.promotion_status = 'pending'; child.status = 'promotion_pending'
        assets = loads(child.assets_json, {})
        assets['stock_confirmation'] = {'quantity': payload['params']['stock'],
                                        'warehouse_id': payload['params']['warehouse_id'], 'job_id': job.id}
        child.assets_json = dumps(assets)
    elif not result['eligible']:
        child.promotion_status = 'pending_eligibility'
    else:
        child.promotion_status = 'completed'; child.status = 'completed'
    child.last_error = ''
    publication.status = publication_status(publication.stores)
    job.status = 'completed'; job.progress = 100; job.finished_at = utc_now()
    job.lease_owner = ''; job.lease_expires_at = None; job.error_code = ''; job.error_message = ''
    for step in job.steps:
        step.status = 'completed'; step.progress = 100; step.details_json = dumps(result)
    prior.update(state='done', result_hash=digest(result))
    job.external_ref = dumps(prior)
    if result['eligible']:
        from erp.api.routes.publications import _complete_followup_step
        _complete_followup_step(session, publication, child.store_id, phase, payload['params'])
    return {'job_id': job.id, 'status': job.status}
