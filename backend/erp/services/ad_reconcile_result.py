"""Accept a read-only platform check made by the original execution computer."""
import hashlib
import json

from sqlalchemy.orm import Session

from erp.core.config import get_settings
from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps, loads
from erp.models import AdAction, AdExecutionBatch, Job
from erp.services.ad_desktop_snapshot import reconcile_source_fingerprint
from erp.services.ad_execution import _apply_confirmed
from erp.services.ad_execution_result import _unpack


def completion_summary(job: Job) -> dict:
    receipt = loads(job.external_ref, {})
    if receipt.get('kind') != 'ad_reconcile':
        raise ConflictError('广告核对回执尚未接收')
    return {key: receipt[key] for key in (
        'approval_id', 'store_id', 'batch_status', 'result_sha256', 'result_bytes')}


def accept_reconcile_result(session: Session, job: Job, encoded: str) -> dict:
    payload, packed, digest = _unpack(encoded)
    prior = loads(job.external_ref, {})
    if prior.get('kind') == 'ad_reconcile':
        if prior.get('result_sha256') != digest:
            raise ConflictError('广告核对任务已有不同回执')
        return completion_summary(job)
    if prior:
        raise ConflictError('广告核对任务已有其他回执')
    task = loads(job.payload_json, {})
    batch = session.get(AdExecutionBatch, task.get('approval_id'))
    if (job.type != 'ad_optimization_reconcile' or not batch
            or batch.store_id != task.get('store_id') or batch.actor != job.created_by
            or batch.status not in {'unknown', 'executing'} or batch.active_store != batch.store_id):
        raise ConflictError('广告核对任务与待核对批次不一致')
    directory = get_settings().data_dir / 'ad_task_snapshots'
    try:
        snapshot_hash = (directory / f'{job.id}.sha256').read_text(encoding='ascii').strip()
        source_hash = (directory / f'{job.id}.source.sha256').read_text(encoding='ascii').strip()
    except OSError as error:
        raise ConflictError('广告核对输入快照不存在') from error
    if (payload.get('schema') != 1 or payload.get('approval_id') != batch.id
            or payload.get('store_id') != batch.store_id
            or payload.get('snapshot_sha256') != snapshot_hash
            or payload.get('source_sha256') != source_hash):
        raise ConflictError('广告核对回执与输入快照不一致')
    old_results = loads(batch.results_json, [])
    new_results = payload.get('results')
    action_rows = payload.get('actions')
    if (not isinstance(old_results, list) or not isinstance(new_results, list)
            or len(new_results) != len(old_results) or len(new_results) > 1000
            or not isinstance(action_rows, list) or len(action_rows) > 500
            or payload.get('batch_status') not in {'unknown', 'completed'}):
        raise ConflictError('广告核对回执结构无效')
    actions = {}
    for row in action_rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or row['id'] in actions:
            raise ConflictError('广告核对审计编号无效')
        actions[row['id']] = row
    audit_ids = {row.get('audit_id') for row in old_results if isinstance(row, dict) and row.get('audit_id')}
    if set(actions) != audit_ids:
        raise ConflictError('广告核对审计与原批次不一致')
    confirmed = []
    for old, new in zip(old_results, new_results):
        if not isinstance(old, dict) or not isinstance(new, dict):
            raise ConflictError('广告核对商品结果无效')
        changed = {key for key in set(old) | set(new) if old.get(key) != new.get(key)}
        if changed - {'status', 'error'}:
            raise ConflictError('广告核对改变了审批商品或审计关联')
        old_status, new_status = old.get('status'), new.get('status')
        if old_status in {'unknown', 'executing'}:
            if new_status not in {old_status, 'executed'}:
                raise ConflictError('广告核对状态跳转无效')
        elif old_status != new_status or changed:
            raise ConflictError('广告核对修改了已确定的结果')
        if new_status == 'executed' and old_status != 'executed':
            confirmed.append((old, new))
    unresolved = any(row.get('status') in {'unknown', 'executing'} for row in new_results)
    if payload['batch_status'] != ('unknown' if unresolved else 'completed'):
        raise ConflictError('广告核对批次状态与逐项结果不一致')
    for audit_id, row in actions.items():
        audit = session.get(AdAction, audit_id)
        if not audit:
            raise ConflictError('广告核对原审计不存在')
        for field in ('ad_sku_id', 'action', 'previous_target_drr',
                      'proposed_target_drr', 'reason', 'before_metrics_json'):
            if row.get(field) != getattr(audit, field):
                raise ConflictError('广告核对修改了原操作审计')
        matched = next((old for old, new in confirmed if old.get('audit_id') == audit_id), None)
        if matched:
            if (audit.status not in {'unknown', 'executing'} or row.get('status') != 'executed'
                    or loads(row.get('after_metrics_json'), {}).get('confirmed_from_platform') is not True):
                raise ConflictError('广告平台确认缺少只读查询证据')
        elif row.get('status') != audit.status or row.get('after_metrics_json') != audit.after_metrics_json:
            raise ConflictError('广告核对修改了未确认的操作审计')
    source_changed = reconcile_source_fingerprint(session, batch) != source_hash
    if not source_changed:
        plan = loads(batch.preview_json, {}).get('items', [])
        for old, new in confirmed:
            item = next((item for item in plan if item.get('sku_id') == old.get('sku_id')
                         and item.get('campaign_id') == old.get('campaign_id')), None)
            if not item:
                raise ConflictError('广告核对商品不在原审批中')
            audit = session.get(AdAction, old['audit_id'])
            audit.status = 'executed'
            audit.after_metrics_json = actions[audit.id]['after_metrics_json']
            _apply_confirmed(session, batch, item)
        batch.results_json = dumps(new_results)
        batch.status = payload['batch_status']
        if batch.status == 'completed':
            batch.active_store = None
    target_dir = get_settings().data_dir / 'ad_reconcile_results'
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f'{job.id}.json.gz'
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise ConflictError('广告核对历史回执冲突')
    if not target.exists():
        temporary = target.with_suffix('.partial')
        try:
            with temporary.open('xb') as output:
                output.write(packed)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    receipt = {'kind': 'ad_reconcile', 'approval_id': batch.id,
               'store_id': batch.store_id, 'batch_status': batch.status,
               'result_sha256': digest, 'result_bytes': len(packed)}
    if len(json.dumps(receipt, separators=(',', ':'))) > 300:
        raise ConflictError('广告核对摘要超过数据库字段限制')
    job.external_ref = json.dumps(receipt, separators=(',', ':'))
    session.flush()
    return completion_summary(job)
