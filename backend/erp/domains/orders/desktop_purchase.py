"""Private purchase status reads; only the initiating PC contacts suppliers."""
import asyncio
import hashlib
import json

from erp.core.errors import ConflictError
from erp.core.jsonutil import loads, dumps
from erp.core.security import vault
from .legacy_engine import HicustomOrderQueue, OrderError
from .receipts import apply_once
from .service import manager


def fingerprint(group):
    # Detail reads add decrypted address; list reads omit it. Compare the same
    # persisted fields in both paths (including updated_at), not view shape.
    value={k:v for k,v in group.items() if k!='address'}
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def snapshot(session, job):
    if job.type != 'pod_purchase_sync':raise ConflictError('不是采购状态同步任务')
    config=vault.decrypt(job.config_snapshot_json)
    if '_status_targets' not in config:
        supplier=loads(job.payload_json,{}).get('supplier','')
        orders=manager(session)
        groups=orders.list_batch_groups(limit=500)
        selected=[g for g in groups if g.get('submission_status') in {'submitted','unknown'}
                  and str(g.get('out_order_id') or '').strip()
                  and (not supplier or str(g.get('supplier') or 'hicustom')==supplier)]
        config['_status_targets']=[{'id':g['id'],'supplier':str(g.get('supplier') or 'hicustom'),
                                   'out_order_id':str(g['out_order_id']).strip(),
                                   'fingerprint':fingerprint(g)} for g in selected]
        guards={g['id']:g for g in selected}
        for group in selected:
            if str(group.get('supplier') or 'hicustom')=='hicustom' and group.get('purchase_id'):
                guards.update({g['id']:g for g in orders.list_purchase_groups(group['purchase_id'])})
        config['_status_guards']={key:fingerprint(group) for key,group in guards.items()}
        job.config_snapshot_json=vault.encrypt(config)
    targets=list({(g['supplier'],g['out_order_id']):
                  {'supplier':g['supplier'],'out_order_id':g['out_order_id']}
                  for g in config['_status_targets']}.values())
    return {'config':{k:v for k,v in config.items() if k not in {'_status_targets','_status_guards'}},'targets':targets}


def accept(session, job, records):
    if job.type!='pod_purchase_sync':raise ConflictError('不是采购状态同步任务')
    config=vault.decrypt(job.config_snapshot_json)
    expected=config.get('_status_targets')
    if expected is None:raise ConflictError('尚未获取采购任务快照')
    expected_keys={(g['supplier'],g['out_order_id']) for g in expected}
    facts={}
    for row in records:
        key=(row.get('supplier'),row.get('out_order_id'))
        if key not in expected_keys or key in facts or type(row.get('failed')) is not bool:
            raise ConflictError('采购回传目标无效或重复')
        if row.get('remote') is not None and not isinstance(row['remote'],dict):
            raise ConflictError('供应商订单结果无效')
        if row['failed'] and row.get('remote') is not None:raise ConflictError('采购结果状态矛盾')
        facts[key]=row
    if set(facts)!=expected_keys:raise ConflictError('采购结果不完整')
    def apply(orders):
        current={g['id']:g for g in orders.list_batch_groups(limit=500)}
        for target in expected:
            group=current.get(target['id'])
            if group and str(group.get('supplier') or 'hicustom')=='hicustom' and group.get('purchase_id'):
                merged={g['id']:g for g in orders.list_purchase_groups(group['purchase_id'])}
                if not set(merged)<=set(config['_status_guards']):
                    raise ConflictError('合并采购范围已变化，请重新同步')
                current.update(merged)
        for identity,digest in config['_status_guards'].items():
            # Older frozen snapshots hashed the detail-only address too.
            # Accept that exact representation for same-task recovery only.
            group=current.get(identity)
            valid={fingerprint(group),hashlib.sha256(json.dumps(group,sort_keys=True,ensure_ascii=False).encode()).hexdigest()} if group else set()
            if digest not in valid:
                raise ConflictError('合并采购资料已变化，请重新同步')
        groups=[]
        for target in expected:
            group=current.get(target['id'])
            if not group or fingerprint(group)!=target['fingerprint']:
                raise ConflictError('采购资料已变化，请重新同步，旧结果不覆盖新状态')
            groups.append(group)
        async def forbidden(*args):
            raise RuntimeError('供应商查询只能在发起人电脑执行')
        async def read(provider,external):
            row=facts[(provider,external)]
            if row['failed']:raise OrderError('供应商查询失败，请检查连接后重新同步')
            return row['remote']
        queue=HicustomOrderQueue(orders,lambda:{},forbidden)
        result=asyncio.run(queue.sync_remote_orders(remote_reader=read,groups_snapshot=groups,strict_apply=True))
        # Supplier details stay in the private order database, not job logs.
        return {k:v for k,v in result.items() if k!='errors'}
    summary=apply_once(manager(session),job.id+':purchase-status',records,apply)
    step=next(s for s in job.steps if s.key=='sync')
    step.status='completed';step.progress=100;step.details_json=dumps({'summary':summary})
    return summary


def completion_summary(job):
    if job.type!='pod_purchase_sync' or any(s.status!='completed' for s in job.steps):
        raise ConflictError('采购状态尚未完整回传')
    return loads(next(s for s in job.steps if s.key=='sync').details_json,{})['summary']
