"""Recover WB price acknowledgements on the original PC, without replaying writes."""
import math
import hashlib
import json
from datetime import timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import load_only

from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps, loads
from erp.models import Asset, CatalogProduct, CatalogVariant, Job, JobArtifact, JobEvent, WbPublicationItem, utc_now

PENDING_CODE = 'wb_price_verification_pending'
QUEUED_CODE = 'wb_price_recovery_queued'


def legacy_package_prices(session, source_ids, store_id):
    """Use hash-checked original export receipts, never today's mutable costs."""
    from erp.domains.pod.wb_export import _roots
    root = _roots()[1].resolve()
    prices = {}
    for source_id, asset in session.execute(select(JobArtifact.job_id, Asset).join(
            Asset, Asset.id == JobArtifact.asset_id).where(JobArtifact.job_id.in_(source_ids),
            JobArtifact.role == 'wb_package')):
        try:
            path = Path(asset.local_path).resolve()
            if not path.is_relative_to(root) or not path.is_file() or not 0 < asset.size_bytes <= 20_000_000:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if len(data) != asset.size_bytes or hashlib.sha256(data).hexdigest() != asset.sha256:
            continue
        try:
            package = json.loads(data)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(package, dict) or (package.get('payload') or {}).get('store_id') != store_id:
            continue
        for group in package.get('groups', []):
            for variant in group.get('variants', []):
                value = variant.get('price')
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    continue
                key = (source_id, variant.get('vendorCode'))
                prices.setdefault(key, set()).add(value)
    return {key: next(iter(values)) for key, values in prices.items() if len(values) == 1}


def price_rows(session, job, rows=None, *, immutable_only=False):
    """Batch-read the original plan; new operations freeze it before dispatch."""
    payload = loads(job.payload_json, {})
    ids = payload.get('ledger_ids') or []
    rows = rows if rows is not None else list(session.scalars(select(WbPublicationItem).where(WbPublicationItem.id.in_(ids))))
    by_id = {r.id: r for r in rows}
    if not ids or len(ids) != len(set(ids)) or set(by_id) != set(ids):
        raise ConflictError('WB 原价格任务商品不完整，请核对')
    if any(r.store_id != payload.get('store_id') or not str(r.external_product_id).isdigit() for r in rows):
        raise ConflictError('WB 原价格任务商品编号已变化，请核对')
    frozen = payload.get('price_plan')
    if frozen is None:
        sources = {s.id: loads(s.payload_json, {}) for s in session.scalars(select(Job).where(
            Job.id.in_({r.job_id for r in rows})).options(load_only(Job.id, Job.payload_json)))}
        products, variants = {}, {}
        historical = legacy_package_prices(session, set(sources), payload.get('store_id')) if immutable_only and not payload.get('price') else {}
        if not immutable_only:
            products = {p.id: p for p in session.scalars(select(CatalogProduct).where(CatalogProduct.id.in_({r.product_id for r in rows})))}
            variants = {v.id: v for v in session.scalars(select(CatalogVariant).where(CatalogVariant.id.in_({r.variant_id for r in rows})))}
        frozen = []
        for identity in ids:
            row = by_id[identity]
            source = sources.get(row.job_id, {})
            saved = next((v for v in source.get('rows', []) if v.get('id') == identity), {})
            value = payload.get('price') or ((saved.get('price') or historical.get((row.job_id, row.offer_id))) if immutable_only else None)
            if not value and not immutable_only:
                from erp.domains.pod.wb_export import _variant_price
                product, variant = products.get(row.product_id), variants.get(row.variant_id)
                if not product or not variant:
                    raise ConflictError('WB 商品价格资料缺失')
                value = _variant_price(product, variant, source.get('pricing') or {}).get('list_price_rub')
            if not value or not math.isfinite(float(value)) or float(value) <= 0:
                raise ConflictError('缺少原任务的确定价格，不能自动确认成功')
            frozen.append({'id': identity, 'nm_id': int(row.external_product_id),
                           'price': math.ceil(float(value)), 'discount': payload.get('discount_percent')})
    if (not isinstance(frozen, list) or len(frozen) != len(ids)
            or any(not isinstance(v, dict) or set(v) != {'id', 'nm_id', 'price', 'discount'}
                   or v.get('id') not in by_id or type(v.get('nm_id')) is not int
                   or v['nm_id'] != int(by_id[v['id']].external_product_id) or v['nm_id'] <= 0
                   or type(v.get('price')) is not int or v['price'] <= 0
                   or type(v.get('discount')) is not int or not 0 <= v['discount'] <= 99 for v in frozen)
            or {v['id'] for v in frozen} != set(ids)):
        raise ConflictError('WB 原价格快照无效或商品编号已变化')
    return frozen


def freeze_plan(session, job, rows=None):
    payload = loads(job.payload_json, {})
    plan = price_rows(session, job, rows)
    if 'price_plan' not in payload:
        payload['price_plan'] = plan
        job.payload_json = dumps(payload)
    return plan


def prepare_manual_retry(session, job):
    """Unknown writes remain read-only even when the user retries the old task."""
    payload = loads(job.payload_json, {})
    if job.error_code in {'worker_lease_expired', 'wb_price_outcome_unknown', PENDING_CODE} or payload.get('wb_price_recovery'):
        payload['verify_only'] = True
        recovery = payload.setdefault('wb_price_recovery', {'original_error_code': job.error_code,
                                                          'original_error_message': job.error_message})
        recovery['state'] = 'queued'
        job.payload_json = dumps(payload)
    rows = list(session.scalars(select(WbPublicationItem).where(
        WbPublicationItem.id.in_(payload.get('ledger_ids') or [])).with_for_update()))
    from erp.domains.pod.wb_price_state import latest_operations
    latest = latest_operations(session, rows)
    if not rows or any(latest.get(r.id) is not job or r.status not in {'pending_price', 'price_failed', 'price_processing'} for r in rows):
        raise ConflictError('价格台账已变化或已有新任务，请刷新查看')
    freeze_plan(session, job, rows)
    for row in rows:
        row.status = 'price_processing'


def interrupted(session, job):
    """At most one automatic read, fenced by a new attempt on the same device."""
    payload = loads(job.payload_json, {})
    if (job.type != 'pod_wb_price' or job.cancel_requested or job.pause_requested
            or payload.get('wb_shop_sync_released_by') or not job.target_worker_id):
        return False
    previous = payload.get('wb_price_recovery') or {}
    can_read = bool(payload.get('price_plan')) and not previous
    if can_read:
        try:
            price_rows(session, job)
            from erp.domains.pod.wb_price_state import latest_operations
            rows = list(session.scalars(select(WbPublicationItem).where(WbPublicationItem.id.in_(payload['ledger_ids']))))
            latest = latest_operations(session, rows)
            can_read = all(latest.get(r.id) is job for r in rows)
        except ConflictError:
            can_read = False
    now = utc_now()
    payload['wb_price_recovery'] = {**previous, 'state': 'queued' if can_read else 'pending',
        'interrupted_attempt': previous.get('interrupted_attempt', job.attempts),
        'interrupted_at': previous.get('interrupted_at', now.isoformat()),
        'original_error_code': previous.get('original_error_code', job.error_code),
        'original_error_message': previous.get('original_error_message', job.error_message)}
    # An old client already supports verify_only; this flag can never fall back to upload.
    payload['verify_only'] = True
    job.payload_json = dumps(payload)
    job.status = 'retrying' if can_read else 'action_required'
    job.error_code = QUEUED_CODE if can_read else PENDING_CODE
    job.error_message = ('执行连接中断，平台结果待核验；60秒后由原电脑批量只读核对，不重新提交价格。' if can_read else
                         '执行连接中断，平台结果尚未确认；请在原电脑只读核对或完成店铺同步，不要重复提交价格。')
    job.heartbeat_at = now
    job.finished_at = None
    job.lease_owner = ''
    job.lease_expires_at = None
    for step in job.steps:
        if step.status != 'completed':
            step.status = 'pending'
            step.error_message = ''
            if step.key == 'verify':
                step.name = '只读核验原价格结果'
    session.add(JobEvent(job_id=job.id, level='warning', message=job.error_message,
                         data_json=dumps(payload['wb_price_recovery'])))
    return True


def verification_failed(session, job):
    payload = loads(job.payload_json, {})
    recovery = payload.get('wb_price_recovery')
    if job.type != 'pod_wb_price' or not recovery:
        return False
    reason = job.error_message
    mismatch = 'WB 实际价格或折扣与计划不一致；本次未写入' in reason
    recovery.update(state='not_applied' if mismatch else 'pending', last_check_error=reason)
    payload['verify_only'] = True
    job.payload_json = dumps(payload)
    job.status = 'action_required'
    job.error_code = 'wb_price_manual_submit_required' if mismatch else PENDING_CODE
    job.error_message = ('已只读核验，价格或折扣尚未全部符合计划；商品已释放到待设置价格折扣页面，请人工提交。本次未重新写入。' if mismatch else
                         '原任务结果仍待核验；本次未重新提交价格。') + reason
    session.add(JobEvent(job_id=job.id, level='warning', message=job.error_message))
    return True


def verified(session, job, *, via, reference=''):
    payload = loads(job.payload_json, {})
    recovery = payload.get('wb_price_recovery') or {}
    recovery.update(state='verified', verified_at=utc_now().isoformat(), via=via, reference=reference)
    payload['wb_price_recovery'] = recovery
    job.payload_json = dumps(payload)
    session.add(JobEvent(job_id=job.id, message='已只读核验原计划价格和折扣一致；原执行中断记录保留，未重新提交价格',
                         data_json=dumps(recovery)))


def resolve_from_sync(session, refresh, existing, matched, *, warnings):
    """A complete authorised store read resolves tasks as well as ledger rows."""
    if warnings:
        return []
    request = loads(refresh.payload_json, {})
    cards = {r.id: matched[v] for v, r in existing.items() if v in matched}
    from erp.domains.pod.wb_price_state import latest_operations
    latest = latest_operations(session, list(existing.values()))
    jobs = list(session.scalars(select(Job).where(Job.type == 'pod_wb_price',
        Job.created_by == refresh.created_by, Job.status.in_({'failed', 'cancelled', 'action_required'}),
        Job.payload_json.contains(request['store_id'])).with_for_update()))
    resolved = []
    for job in jobs:
        payload = loads(job.payload_json, {})
        recovery = payload.get('wb_price_recovery') or {}
        origin_error = recovery.get('original_error_code') or job.error_code
        if (payload.get('store_id') != request['store_id'] or job.attempts < 1
                or origin_error not in {'worker_lease_expired', 'wb_price_outcome_unknown'}
                or payload.get('wb_shop_sync_original_cancel_requested') is True
                or job.created_at.replace(tzinfo=timezone.utc) > refresh.created_at.replace(tzinfo=timezone.utc)
                or (job.cancel_requested and not payload.get('wb_shop_sync_released_by'))):
            continue
        if any(latest.get(identity) is not job and latest.get(identity)
               and latest[identity].status not in {'failed', 'cancelled', 'completed'}
               for identity in payload.get('ledger_ids', [])):
            continue
        try:
            expected = price_rows(session, job, immutable_only=True)
        except ConflictError:
            continue
        if any(v['id'] not in cards or cards[v['id']]['nmID'] != v['nm_id']
               or cards[v['id']]['price']['state'] != 'verified'
               # 0.6.91 receipts can omit the optional currency label. Compare
               # original API numeric values on the same store, without FX.
               or cards[v['id']]['price']['currency'] not in {'', 'CNY'}
               or cards[v['id']]['price']['price'] != v['price']
               or cards[v['id']]['price']['discount'] != v['discount'] for v in expected):
            continue
        payload.setdefault('price_plan', expected)
        payload.setdefault('wb_price_recovery', {'original_error_code': job.error_code,
                                               'original_error_message': job.error_message})
        job.payload_json = dumps(payload)
        verified(session, job, via='shop_sync', reference=refresh.id)
        job.status = 'completed'
        job.progress = 100
        job.error_code = job.error_message = ''
        job.finished_at = utc_now()
        job.lease_owner = ''
        job.lease_expires_at = None
        for step in job.steps:
            step.status = 'completed'
            step.progress = 100
            step.error_message = ''
            step.finished_at = job.finished_at
            step.details_json = dumps({'verified_existing': True, 'verification_job_id': refresh.id,
                                       'items': len(expected), 'platform_writes': 0})
        resolved.append(job.id)
    return resolved
