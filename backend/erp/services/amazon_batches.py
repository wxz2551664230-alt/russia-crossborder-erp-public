"""Compact batch presentation and atomic removal of unsubmitted Amazon drafts."""
from collections import Counter

from sqlalchemy import JSON, cast, delete, func, or_, select, type_coerce
from sqlalchemy.exc import OperationalError

from erp.core.errors import ConflictError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.core.task_identity import task_identity
from erp.models import AmazonListingBatch, AmazonListingItem, Job, JobEvent, utc_now

PRE_SUBMIT = {'draft', 'waiting_codex', 'preparing', 'needs_facts', 'ready', 'validated', 'validation_failed'}


def compact_rows(session, store_id='', ids=None):
    # Extract only presentation fields in SQL; never send full facts, image
    # lists, drafts or platform receipts to the batch table.
    raw = AmazonListingBatch.snapshot_json
    snap = cast(raw, JSON) if session.get_bind().dialect.name == 'postgresql' else type_coerce(raw, JSON)
    query = select(AmazonListingBatch.id, AmazonListingBatch.store_id, AmazonListingBatch.status,
        AmazonListingBatch.job_id, AmazonListingBatch.created_by, AmazonListingBatch.worker_id,
        AmazonListingBatch.created_at, snap['generation_provider'].as_string().label('provider'),
        snap['selection_group']['id'].as_string().label('group_id'),
        snap['parent_sku'].as_string().label('parent_sku'),
        snap['product_type'].as_string().label('product_type'),
        func.coalesce(func.json_array_length(snap['items']), 0).label('count'))
    if store_id: query = query.where(AmazonListingBatch.store_id == store_id)
    if ids is not None: query = query.where(AmazonListingBatch.id.in_(ids))
    return [dict(row) for row in session.execute(query).mappings()]


def related_jobs(session, ids, lock=False):
    if not ids: return []
    from erp.services.amazon_listings import TYPES
    payload = cast(Job.payload_json, JSON) if session.get_bind().dialect.name == 'postgresql' else type_coerce(Job.payload_json, JSON)
    query = select(Job).where(Job.type.in_(TYPES), payload['batch_id'].as_string().in_(ids)).order_by(Job.id)
    if lock: query = query.with_for_update(nowait=True).execution_options(populate_existing=True)
    return [job for job in session.scalars(query) if loads(job.payload_json, {}).get('batch_id') in ids]


def removal_reason(row, jobs, unsafe=False):
    actor = task_identity.get() or {}
    if row['created_by'] != actor.get('id') or row['worker_id'] != actor.get('worker_id'):
        return '请在创建本批次的账号和原电脑删除'
    if row['status'] not in PRE_SUBMIT:
        return '已进入正式提交阶段，保留上架记录；请继续核对原批次'
    if any(j.type in {'amazon_submit', 'amazon_reconcile'} for j in jobs) or unsafe:
        return '存在正式提交或平台结果，不能释放商品以免重复上架'
    if any(j.status == 'running' or j.lease_owner for j in jobs):
        return '任务正在执行，请先停止并等待原电脑结束后删除'
    if row['job_id'] and row['job_id'] not in {j.id for j in jobs}:
        return '任务关联不完整，不能删除批次'
    return ''


def list_grouped(session, store_id='', status='', page=1):
    rows = [r for r in compact_rows(session, store_id) if r['status'] != 'deleted']
    by_batch = {r['id']: [] for r in rows}
    for job in related_jobs(session, list(by_batch)):
        by_batch[loads(job.payload_json, {})['batch_id']].append(job)
    unsafe = set(session.scalars(select(AmazonListingItem.batch_id).where(
        AmazonListingItem.batch_id.in_(by_batch), or_(AmazonListingItem.status != 'draft',
        AmazonListingItem.asin != '', AmazonListingItem.receipt_json != '{}')).distinct()))
    groups = {}
    for row in rows:
        identity = (row['group_id'], row['store_id'], row['created_by'], row['worker_id']) \
            if row['provider'] == 'codex' and row['group_id'] else (row['id'],)
        groups.setdefault(identity, []).append(row)
    result = []
    for members in groups.values():
        members.sort(key=lambda r: r['created_at'])
        first = members[0]
        counts = Counter(r['status'] for r in members)
        if status and status not in counts: continue
        stage = next((s for s in ['submitting', 'preparing', 'unknown', 'issues', 'validation_failed',
            'needs_facts', 'waiting_codex', 'ready', 'validated', 'processing', 'reviewing',
            'created_no_stock', 'published', 'draft'] if counts[s]), first['status'])
        total = sum(r['count'] for r in members)
        progress = 0
        reasons = []
        for row in members:
            jobs = by_batch[row['id']]
            current = next((j for j in jobs if j.id == row['job_id']), None)
            done = row['status'] not in {'draft', 'waiting_codex', 'preparing'}
            progress += row['count'] * (100 if done else max(0, min(99, current.progress)) if current else 0)
            reasons.append(removal_reason(row, jobs, row['id'] in unsafe))
        result.append({'id': first['group_id'] if len(members) > 1 else first['id'],
            'group_id': first['group_id'] if first['provider'] == 'codex' else '',
            'primary_batch_id': first['id'], 'batch_ids': [r['id'] for r in members],
            'store_id': first['store_id'], 'job_id': first['job_id'],
            'created_at': first['created_at'].isoformat(), 'product_type': first['product_type'],
            'generation_provider': first['provider'], 'count': total, 'batch_count': len(members),
            'parent_sku': first['parent_sku'] if len(members) == 1 else '',
            'status': stage, 'status_counts': dict(counts), 'progress': round(progress / max(1, total)),
            'can_delete': not any(reasons), 'delete_blocked_reason': next((r for r in reasons if r), '')})
    result.sort(key=lambda r: r['created_at'], reverse=True)
    return {'items': result[(page-1)*25:page*25], 'total': len(result)}


def remove(session, ids):
    ids = list(dict.fromkeys(ids))
    try:
        # Use fail-fast row locks: workers lock their job before their batch.
        # If another operation holds a batch, return a conflict instead of
        # waiting in the reverse order and deadlocking with a lease/resume.
        related_jobs(session, ids, lock=True)
        rows = session.scalars(select(AmazonListingBatch).where(AmazonListingBatch.id.in_(ids))
            .order_by(AmazonListingBatch.id).with_for_update(nowait=True)
            .execution_options(populate_existing=True)).all()
        if len(rows) != len(ids): raise NotFoundError('部分 Amazon 批次不存在，请刷新')
        jobs = related_jobs(session, ids, lock=True)
        by_batch = {id: [] for id in ids}
        for job in jobs: by_batch[loads(job.payload_json, {})['batch_id']].append(job)
        items = session.scalars(select(AmazonListingItem).where(AmazonListingItem.batch_id.in_(ids))).all()
        actor = task_identity.get() or {}
        for row in rows:
            if row.created_by != actor.get('id') or row.worker_id != actor.get('worker_id'):
                raise ConflictError('请在创建本批次的账号和原电脑删除')
            if row.status == 'deleted': continue
            unsafe = any(i.batch_id == row.id and (i.status != 'draft' or i.asin or loads(i.receipt_json, {})) for i in items)
            reason = removal_reason({'created_by':row.created_by, 'worker_id':row.worker_id,
                'status':row.status, 'job_id':row.job_id}, by_batch[row.id], unsafe)
            if reason: raise ConflictError(reason)
        now = utc_now()
        for job in jobs:
            payload = loads(job.payload_json, {})
            if payload.get('amazon_batch_removed'): continue
            payload['amazon_batch_removed'] = True
            job.payload_json = dumps(payload); job.cancel_requested = True; job.pause_requested = False
            if job.status != 'completed': job.status = 'cancelled'
            job.finished_at = now; job.lease_owner = ''; job.lease_expires_at = None
            job.error_code = 'amazon_batch_removed'; job.error_message = '批次已删除，原任务不能恢复或重试'
            session.add(JobEvent(job_id=job.id, level='warning', message='用户删除未提交批次，释放产品；未调用 Amazon'))
        for row in rows:
            if row.status == 'deleted': continue
            previous = loads(row.result_json, {})
            previous['deletion'] = {'at':now.isoformat(), 'actor_id':actor['id'],
                'items':[{'sku':i.sku, 'product_id':i.product_id, 'variant_id':i.variant_id} for i in items if i.batch_id == row.id]}
            row.result_json = dumps(previous); row.status = 'deleted'
        session.execute(delete(AmazonListingItem).where(AmazonListingItem.batch_id.in_(ids)))
        session.commit()
        return {'removed_batch_ids': ids, 'released_count': sum(bool(i.variant_id) for i in items)}
    except OperationalError as exc:
        session.rollback()
        if getattr(exc.orig, 'sqlstate', None) == '55P03' or getattr(exc.orig, 'pgcode', None) == '55P03':
            raise ConflictError('批次正在被操作，请稍后再删除') from exc
        raise
    except Exception:
        session.rollback()
        raise
