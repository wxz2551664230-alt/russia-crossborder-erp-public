"""Apply the initiating PC's reviewed Ozon ad result without server API calls."""
import base64
import binascii
from datetime import datetime
import gzip
import hashlib
import io
import json
import re

from sqlalchemy.orm import Session

from erp.core.config import get_settings
from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps, loads
from erp.models import AdAction, AdExecutionBatch, Job
from erp.services.ad_desktop_snapshot import execution_source_fingerprint
from erp.services.ad_execution import _apply_confirmed

MAX_PACKED = 12 * 1024 * 1024
MAX_JSON = 64 * 1024 * 1024


def completion_summary(job: Job) -> dict:
    receipt = loads(job.external_ref, {})
    if receipt.get('kind') != 'ad_execution':
        raise ConflictError('广告执行结果尚未被云端接收')
    return {key: receipt[key] for key in (
        'approval_id', 'store_id', 'batch_status', 'result_sha256', 'result_bytes')}


def _unpack(encoded: str) -> tuple[dict, bytes, str]:
    if len(encoded) > MAX_PACKED * 4 // 3 + 4:
        raise ConflictError('广告执行回执超过大小限制')
    try:
        packed = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ConflictError('广告执行回执编码无效') from error
    if not packed or len(packed) > MAX_PACKED:
        raise ConflictError('广告执行回执超过大小限制')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(packed)) as source:
            raw = source.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise ConflictError('广告执行回执解压超过限制')
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConflictError('广告执行回执格式无效') from error
    if not isinstance(value, dict):
        raise ConflictError('广告执行回执结构无效')
    return value, packed, hashlib.sha256(packed).hexdigest()


def accept_execution_result(session: Session, job: Job, encoded: str) -> dict:
    payload, packed, digest = _unpack(encoded)
    existing = loads(job.external_ref, {})
    if existing.get('kind') == 'ad_execution':
        if existing.get('result_sha256') != digest:
            raise ConflictError('广告执行任务已保存不同结果')
        return completion_summary(job)
    if existing:
        raise ConflictError('广告执行任务已有其他回执')
    task = loads(job.payload_json, {})
    batch = session.get(AdExecutionBatch, task.get('approval_id'))
    if (job.type != 'ad_optimization_execute' or not batch or batch.store_id != task.get('store_id')
            or batch.actor != job.created_by or batch.status != 'executing'
            or batch.active_store != batch.store_id):
        raise ConflictError('广告执行任务与审批批次不一致')
    source_dir = get_settings().data_dir / 'ad_task_snapshots'
    try:
        expected_snapshot = (source_dir / f'{job.id}.sha256').read_text(encoding='ascii').strip()
        expected_source = (source_dir / f'{job.id}.source.sha256').read_text(encoding='ascii').strip()
    except OSError as error:
        raise ConflictError('广告执行来源快照不存在') from error
    if (payload.get('schema') != 1 or payload.get('store_id') != batch.store_id
            or payload.get('approval_id') != batch.id
            or payload.get('snapshot_sha256') != expected_snapshot
            or payload.get('source_sha256') != expected_source):
        raise ConflictError('广告执行回执与任务来源不一致')
    plan = loads(batch.preview_json, {})
    items = plan.get('items', [])
    blocked = plan.get('blocked', [])
    results = payload.get('results')
    actions = payload.get('actions')
    status = payload.get('batch_status')
    if (status not in {'completed', 'unknown', 'stale'}
            or not isinstance(items, list) or not isinstance(blocked, list)
            or not isinstance(results, list) or not isinstance(actions, list)
            or len(results) > 1000 or len(actions) > 500):
        raise ConflictError('广告执行结果字段无效')
    if status == 'stale':
        if results or actions:
            raise ConflictError('过期广告批次不能包含平台写入')
    else:
        expected = {(row['sku_id'], row['campaign_id']) for row in items}
        attempted = {(row.get('sku_id'), row.get('campaign_id')) for row in results if row.get('campaign_id')}
        if len(expected) != len(items) or attempted != expected or len(results) != len(items) + len(blocked):
            raise ConflictError('广告执行回执未覆盖全部审批商品')
        allowed_statuses = {'executed', 'unknown', 'executing', 'not_attempted', 'blocked'}
        if any(row.get('status') not in allowed_statuses for row in results):
            raise ConflictError('广告执行回执状态无效')
        if any((row.get('status') in {'executed', 'unknown', 'executing'}) != bool(row.get('audit_id'))
               for row in results if row.get('campaign_id')):
            raise ConflictError('广告执行回执缺少或多出操作审计')
        if status == 'completed' and any(row.get('status') != 'executed' for row in results if row.get('campaign_id')):
            raise ConflictError('完成批次仍含未确认的平台写入')
        if status == 'unknown' and not any(row.get('status') in {'unknown', 'executing'} for row in results):
            raise ConflictError('未知批次缺少待核对结果')
    action_map = {row.get('id'): row for row in actions if isinstance(row, dict)}
    if len(action_map) != len(actions):
        raise ConflictError('广告执行审计包含重复编号')
    item_by_key = {(row['sku_id'], row['campaign_id']): row for row in items}
    referenced = {row.get('audit_id') for row in results if row.get('audit_id')}
    if referenced != set(action_map):
        raise ConflictError('广告执行审计与平台结果不一致')
    source_changed = execution_source_fingerprint(session, batch) != expected_source
    if source_changed and status != 'stale':
        # A changed shared source makes the PC's confirmed writes ambiguous for
        # current ERP rows. Keep the platform evidence and the store lock.
        status = 'unknown'
        results = [{**row, 'status': 'unknown' if row.get('status') in {'executed', 'executing'} else row.get('status'),
                    'error': '广告资料在执行期间变化，须核对平台当前结果'}
                   if row.get('campaign_id') else row for row in results]
    for row in results:
        audit_id = row.get('audit_id')
        if not audit_id:
            continue
        source = action_map[audit_id]
        item = item_by_key.get((row.get('sku_id'), row.get('campaign_id')))
        if (not item or not re.fullmatch(r'[a-f0-9]{32}', str(audit_id))
                or source.get('ad_sku_id') != item.get('ad_sku_id')
                or source.get('action') != ('STOP_ADS_CANDIDATE' if batch.action == 'STOP_ADS_CANDIDATE' else 'INCREASE_TARGET_DRR_CANDIDATE')
                or source.get('status') not in {'executed', 'unknown', 'executing'}
                or (not source_changed and source.get('status') != row['status'])
                or source.get('previous_target_drr') != item.get('previous_target_drr')
                or source.get('proposed_target_drr') != item.get('proposed_target_drr')
                or session.get(AdAction, audit_id)):
            raise ConflictError('广告执行审计与审批商品不一致')
        before = loads(source.get('before_metrics_json'), {})
        if before.get('approval_id') != batch.id or before.get('proposal') != item:
            raise ConflictError('广告操作审计未绑定原审批')
        try:
            decided_at = datetime.fromisoformat(source['decided_at'])
        except (KeyError, TypeError, ValueError) as error:
            raise ConflictError('广告操作审计时间无效') from error
        audit = AdAction(id=audit_id, ad_sku_id=item['ad_sku_id'],
                         action=source['action'], status=row['status'],
                         previous_target_drr=source.get('previous_target_drr'),
                         proposed_target_drr=source.get('proposed_target_drr'),
                         reason=str(source.get('reason') or '')[:2000],
                         before_metrics_json=source['before_metrics_json'],
                         after_metrics_json=source.get('after_metrics_json') or '{}',
                         decided_at=decided_at)
        session.add(audit)
        if row['status'] == 'executed':
            _apply_confirmed(session, batch, item)
    batch.results_json = dumps(results)
    batch.status = status
    if status in {'completed', 'stale'}:
        batch.active_store = None
    target_dir = get_settings().data_dir / 'ad_execution_results'
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f'{job.id}.json.gz'
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise ConflictError('广告执行历史回执与现有文件冲突')
    if not target.exists():
        temporary = target.with_suffix('.partial')
        try:
            with temporary.open('xb') as output:
                output.write(packed)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    receipt = {'kind': 'ad_execution', 'approval_id': batch.id,
               'store_id': batch.store_id, 'batch_status': status,
               'result_sha256': digest, 'result_bytes': len(packed)}
    if len(json.dumps(receipt, separators=(',', ':'))) > 300:
        raise ConflictError('广告执行回执超过数据库字段限制')
    job.external_ref = json.dumps(receipt, separators=(',', ':'))
    session.flush()
    return completion_summary(job)
