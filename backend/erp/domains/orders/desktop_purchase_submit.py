"""Task-scoped shared data bridge; supplier execution stays on the PC."""
from contextlib import closing
import hashlib
import json
import sqlite3

from erp.core.errors import ConflictError
from erp.core.jsonutil import loads
from erp.core.security import vault
from .legacy_engine import is_platform_order_cancelled
from . import purchase_scope
from .receipts import apply_once
from .service import manager


def _resources(scope):
    return sorted(['group:'+x for x in scope['groups']]+['order:'+x for x in scope['orders']])


def _owned(orders,scope,job_id):
    with closing(orders._connect()) as db:
        for resource in _resources(scope):
            row=db.execute('SELECT job_id FROM erp_purchase_reservations WHERE resource=?',(resource,)).fetchone()
            if not row or row['job_id']!=job_id:raise ConflictError('采购目标未由本任务预留')


def resume_reserved(session, target_type, target_id):
    """Keep the original PC journal when the original page submits again."""
    from erp.core.config import get_settings
    from erp.services.jobs import _controllable_job, retry_job, request_resume
    if not get_settings().multiuser_enabled:
        return None
    orders = manager(session)
    scope = purchase_scope.capture(orders, target_type, target_id)
    with closing(orders._connect()) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='erp_purchase_reservations'").fetchone():
            return None
        identities = set()
        for resource in _resources(scope):
            row = db.execute('SELECT job_id FROM erp_purchase_reservations WHERE resource=?', (resource,)).fetchone()
            if row:
                identities.add(row['job_id'])
    if not identities:
        return None
    if len(identities) != 1:
        raise ConflictError('订单涉及多个既有采购任务，请分别恢复原任务')
    job = _controllable_job(session, identities.pop())
    payload = loads(job.payload_json, {})
    if job.type != 'pod_purchase_submit' or payload.get('target_type') != target_type or payload.get('target_id') != target_id:
        raise ConflictError('订单由另一采购范围预留，请恢复原采购任务')
    if job.status in {'failed', 'cancelled'}:
        return retry_job(session, job.id)
    if job.status == 'paused':
        return request_resume(session, job.id)
    if job.status not in {'queued', 'running', 'retrying'}:
        raise ConflictError('原采购任务状态需要核对')
    return job


def recoverable_reserved_job(session, group_id):
    """Expose only the current user's failed original task for a purchase group."""
    from erp.core.task_identity import task_identity
    from erp.models import Job
    actor=task_identity.get() or {}
    orders=manager(session)
    with closing(orders._connect()) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='erp_purchase_reservations'").fetchone():
            return None
        row=db.execute('SELECT job_id FROM erp_purchase_reservations WHERE resource=?',('group:'+group_id,)).fetchone()
    if not row:return None
    job=session.get(Job,row['job_id'])
    if not job or job.type!='pod_purchase_submit' or job.status not in {'failed','paused'}:
        return None
    if job.created_by!=actor.get('id') or not job.target_worker_id:
        return None
    return {'id':job.id,'status':job.status}


def snapshot(session,job):
    if job.type!='pod_purchase_submit':raise ConflictError('不是采购提交任务')
    payload=loads(job.payload_json,{})
    target_type=payload['target_type'];target_id=payload['target_id']
    orders=manager(session)
    with closing(orders._connect()) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='erp_operation_receipts'").fetchone():
            done=db.execute('SELECT summary_json FROM erp_operation_receipts WHERE receipt_key=?',(job.id+':submit-complete',)).fetchone()
            if done:return {'completed_result':json.loads(done['summary_json'])}
    def reserve(private):
        scope=purchase_scope.capture(private,target_type,target_id)
        if not _resources(scope):raise ConflictError('采购任务没有目标')
        if target_type=='group':
            private.assert_purchase_sources_active(scope['groups'])
            targets=[private.get_batch_group(identity) for identity in scope['groups']]
        else:
            target=private.get_order(target_id);targets=[target]
            if is_platform_order_cancelled(target) or target.get('tms_status')=='manual_tracking':
                raise ConflictError('订单已取消或已人工处理物流')
        if any(t.get('submission_status')=='unknown' for t in targets):
            raise ConflictError('原采购结果未知，请先同步核对，不能创建新的采购任务')
        with closing(private._connect()) as db:
            db.execute('CREATE TABLE IF NOT EXISTS erp_purchase_reservations (resource TEXT PRIMARY KEY, job_id TEXT NOT NULL)')
            for resource in _resources(scope):
                previous=db.execute('SELECT job_id FROM erp_purchase_reservations WHERE resource=?',(resource,)).fetchone()
                if previous and previous['job_id']!=job.id:raise ConflictError('另一采购任务已预留相同订单，请继续原任务')
                db.execute('INSERT OR IGNORE INTO erp_purchase_reservations VALUES (?,?)',(resource,job.id))
        return scope
    scope=apply_once(orders,job.id+':reserve',{'type':target_type,'id':target_id},reserve,private_result=True)
    config=vault.decrypt(job.config_snapshot_json)
    if config.get('_submit_scope') not in (None,scope):raise ConflictError('采购任务范围已变化')
    config['_submit_scope']=scope;job.config_snapshot_json=vault.encrypt(config)
    return {'config':{k:v for k,v in config.items() if k!='_submit_scope'},
            'target_type':target_type,'target_id':target_id}


def operation(session,job,method,args,kwargs):
    if job.type!='pod_purchase_submit':raise ConflictError('不是采购提交任务')
    config=vault.decrypt(job.config_snapshot_json);scope=config.get('_submit_scope')
    if not scope:raise ConflictError('尚未取得采购任务快照')
    orders=manager(session)
    def apply(private):
        _owned(private,scope,job.id)
        return purchase_scope.invoke(private,scope,config,method,args,kwargs)
    if method in purchase_scope.WRITE_METHODS:
        payload={'method':method,'args':args,'kwargs':kwargs}
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return apply_once(orders,job.id+':operation:'+digest,payload,apply,private_result=True)
    return apply(orders)


def write_intent(session,job):
    config=vault.decrypt(job.config_snapshot_json);scope=config.get('_submit_scope')
    if job.type!='pod_purchase_submit' or not scope:raise ConflictError('采购任务状态无效')
    def mark(orders):
        _owned(orders,scope,job.id)
        with closing(orders._connect()) as db:
            db.execute('CREATE TABLE IF NOT EXISTS erp_purchase_external_writes (job_id TEXT PRIMARY KEY)')
            db.execute('INSERT OR IGNORE INTO erp_purchase_external_writes VALUES (?)',(job.id,))
        return {'recorded':True}
    return apply_once(manager(session),job.id+':write-intent',{},mark)


def release_unwritten(session,job):
    """Only called after cancellation fences out every worker request."""
    if job.type!='pod_purchase_submit' or job.status!='cancelled':return False
    orders=manager(session)
    with closing(sqlite3.connect(orders.path,timeout=30)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'erp_purchase_external_writes' in tables and db.execute('SELECT 1 FROM erp_purchase_external_writes WHERE job_id=?',(job.id,)).fetchone():
                return False
            if 'erp_purchase_reservations' in tables:db.execute('DELETE FROM erp_purchase_reservations WHERE job_id=?',(job.id,))
            if 'erp_operation_receipts' in tables:db.execute('DELETE FROM erp_operation_receipts WHERE receipt_key LIKE ?',(job.id+':%',))
            db.commit();return True
        except BaseException:
            db.rollback();raise


def completed_state(session,job):
    """Validate actual shared state; never trust a client's completion string."""
    config=vault.decrypt(job.config_snapshot_json);scope=config.get('_submit_scope')
    if job.type!='pod_purchase_submit' or not scope:raise ConflictError('采购任务状态无效')
    orders=manager(session)
    def finish(private):
        _owned(private,scope,job.id)
        targets=([private.get_batch_group(x) for x in scope['groups']] if scope['groups']
                 else [private.get_order(x) for x in scope['orders']])
        if any(t.get('submission_status')!='submitted' or not t.get('hicustom_order_id') for t in targets):
            raise ConflictError('采购结果尚未核对成功')
        with closing(private._connect()) as db:
            db.execute('DELETE FROM erp_purchase_reservations WHERE job_id=?',(job.id,))
        return {'submitted':len(targets)}
    summary=apply_once(orders,job.id+':submit-complete',{},finish)
    if session is not None:
        from erp.domains.pod.gallery import mark_ordered_gallery_codes
        for identity in scope['groups']:
            group=orders.get_batch_group(identity)
            codes=loads(str(group.get('fulfillment_gallery_codes') or '[]'),[])
            if codes:
                mark_ordered_gallery_codes(session,supplier=str(group.get('supplier') or 'hicustom'),
                    gallery_codes=codes,sku=group['sku'],product_name=str(group.get('product_name') or ''),
                    order_id=str(group.get('hicustom_order_id') or ''),commit=False)
        session.flush()
    return summary
