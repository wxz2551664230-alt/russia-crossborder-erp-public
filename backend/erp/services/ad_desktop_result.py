"""Apply a personal PC's advertisement analysis, fenced to one frozen source."""
import base64
import binascii
from datetime import datetime, timedelta
import gzip
import hashlib
import io
import json
import re

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from erp.core.config import get_settings
from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps, loads
from erp.models import AdSku, AdSkuState, AdSkuStateHistory, AdThresholdVersion, AdBacktestRun, AdExecutionBatch, Job, Store, utc_now
from erp.services.ad_desktop_snapshot import analysis_source_fingerprint

MAX_PACKED = 12 * 1024 * 1024
MAX_JSON = 80 * 1024 * 1024


def _unpack(encoded: str) -> tuple[dict, str, int]:
    if len(encoded) > MAX_PACKED * 4 // 3 + 4:
        raise ConflictError('广告结果超过大小限制')
    try:
        packed = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ConflictError('广告结果编码无效') from error
    if not packed or len(packed) > MAX_PACKED:
        raise ConflictError('广告结果超过大小限制')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(packed)) as source:
            content = source.read(MAX_JSON + 1)
        if len(content) > MAX_JSON:
            raise ConflictError('广告结果解压超过大小限制')
        value = json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConflictError('广告结果压缩或 JSON 格式无效') from error
    if not isinstance(value, dict):
        raise ConflictError('广告结果格式无效')
    return value, hashlib.sha256(packed).hexdigest(), len(packed)


def _receipt(job: Job) -> dict | None:
    try:
        value = json.loads(job.external_ref)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) and value.get('kind') in {'ad_analysis', 'ad_backtest', 'ad_preview'} else None


def completion_summary(job: Job) -> dict:
    receipt = _receipt(job)
    if not receipt:
        raise ConflictError('广告任务结果尚未被服务器接收')
    keys = (('store_id', 'version', 'analyzed', 'result_sha256', 'result_bytes', 'approval_id')
            if receipt['kind'] == 'ad_preview' else
            ('store_id', 'version', 'analyzed', 'result_sha256', 'result_bytes')
            if receipt['kind'] == 'ad_analysis' else
            ('store_id', 'run_id', 'sample_size', 'result_sha256', 'result_bytes'))
    return {key: receipt[key] for key in keys}


def apply_analysis_result(session: Session, job: Job, encoded: str) -> dict:
    payload, digest, size = _unpack(encoded)
    previous = _receipt(job)
    if previous:
        expected_kind = 'ad_preview' if job.type == 'ad_optimization_preview' else 'ad_analysis'
        if previous.get('kind') != expected_kind or previous.get('result_sha256') != digest:
            raise ConflictError('广告任务已经写入不同结果')
        return completion_summary(job)
    store_id = loads(job.payload_json, {}).get('store_id')
    archive_dir = get_settings().data_dir / 'ad_task_snapshots'
    try:
        expected_snapshot = (archive_dir / f'{job.id}.sha256').read_text(encoding='ascii').strip()
        expected_source = (archive_dir / f'{job.id}.source.sha256').read_text(encoding='ascii').strip()
    except OSError as error:
        raise ConflictError('广告任务来源快照不存在') from error
    summary = payload.get('summary')
    preview_task = job.type == 'ad_optimization_preview'
    preview_plan = payload.get('preview_plan')
    job_payload = loads(job.payload_json, {})
    if (payload.get('schema') != 1 or payload.get('store_id') != store_id
            or payload.get('snapshot_sha256') != expected_snapshot
            or payload.get('source_sha256') != expected_source
            or not isinstance(summary, dict) or summary.get('store_id') != store_id
            or not re.fullmatch(r'TV_[0-9_]+', str(summary.get('version', '')))):
        raise ConflictError('广告结果与任务来源不一致')
    if preview_task:
        ids = job_payload.get('state_ids')
        if (not isinstance(preview_plan, dict) or not isinstance(ids, list)
                or preview_plan.get('state_ids') != sorted(set(ids))
                or job_payload.get('action') not in {'STOP_ADS_CANDIDATE', 'SCALE_CANDIDATES'}):
            raise ConflictError('广告预览结果与任务选择不一致')
        if session.scalar(select(AdExecutionBatch.id).where(AdExecutionBatch.active_store == store_id)):
            raise ConflictError('该店铺有正在执行或待核对的广告批次')
    elif preview_plan is not None:
        raise ConflictError('非预览任务不能提交广告审批计划')
    if analysis_source_fingerprint(session, store_id) != expected_source:
        raise ConflictError('店铺广告数据已变化，请重新发起分析任务')
    thresholds, states, histories, labels = (
        payload.get(name) for name in ('thresholds', 'states', 'histories', 'labels'))
    if any(not isinstance(value, list) for value in (thresholds, states, histories, labels)):
        raise ConflictError('广告分析结果缺少记录')
    sku_ids = set(session.scalars(select(AdSku.id).where(AdSku.store_id == store_id)))
    count = len(sku_ids)
    if (not count or count > 20000 or not 1 <= len(thresholds) <= 500
            or any(len(rows) != count for rows in (states, histories, labels))
            or type(summary.get('analyzed')) is not int or summary['analyzed'] != count):
        raise ConflictError('广告分析结果数量不一致')
    try:
        threshold_ids = {row['id'] for row in thresholds}
        if (len(threshold_ids) != len(thresholds)
                or any(row['store_id'] != store_id or row['version'] != summary['version'] or row['active'] not in (1, True) for row in thresholds)
                or any({row['ad_sku_id'] for row in rows} != sku_ids for rows in (states, histories))
                or {row['id'] for row in labels} != sku_ids
                or any(row['threshold_version_id'] not in threshold_ids for row in states + histories)
                or len({row['id'] for row in histories}) != count):
            raise ConflictError('广告分析结果包含其他店铺或重复记录')
    except (KeyError, TypeError, AttributeError) as error:
        raise ConflictError('广告分析结果字段无效') from error
    if session.scalar(select(AdThresholdVersion.id).where(AdThresholdVersion.store_id == store_id,
                                                          AdThresholdVersion.version == summary['version'])):
        raise ConflictError('广告阈值版本已经存在')
    session.execute(update(AdThresholdVersion).where(AdThresholdVersion.store_id == store_id,
                                                     AdThresholdVersion.active.is_(True)).values(active=False))
    for row in thresholds:
        item = AdThresholdVersion()
        for field in ('id', 'store_id', 'version', 'cohort_key', 'cohort_level', 'sample_size',
                      'success_sample_size', 'window_days', 'thresholds_json', 'active'):
            setattr(item, field, row[field])
        session.add(item)
    session.flush()
    for row in labels:
        sku = session.get(AdSku, row['id'])
        sku.weak_label = row['weak_label']
        sku.data_maturity = row['data_maturity']
    state_fields = ('state', 'confidence', 'recommendation', 'reason', 'cohort_key', 'cohort_level',
                    'cohort_sample_size', 'threshold_version_id', 'metrics_json', 'evidence_json',
                    'financial_check_json')
    for row in states:
        current = session.scalar(select(AdSkuState).where(AdSkuState.ad_sku_id == row['ad_sku_id']))
        if not current:
            current = AdSkuState(id=row['id'], ad_sku_id=row['ad_sku_id'])
            session.add(current)
        for field in state_fields:
            setattr(current, field, row[field])
    for row in histories:
        history = AdSkuStateHistory()
        for field in ('id', 'ad_sku_id', 'state', 'confidence', 'recommendation', 'reason',
                      'cohort_key', 'threshold_version_id', 'metrics_json', 'evidence_json'):
            setattr(history, field, row[field])
        history.created_at = datetime.fromisoformat(row['created_at'])
        session.add(history)
    session.flush()
    if preview_task:
        from erp.services.ad_execution import _plan
        expected_plan = _plan(session, store_id, job_payload['action'], ids)
        if not expected_plan['items'] or dumps(expected_plan) != dumps(preview_plan):
            raise ConflictError('广告预览计划与原业务规则或安全门不一致')
        preview_plan = {**preview_plan, 'source_fingerprint': analysis_source_fingerprint(session, store_id)}
        store = session.get(Store, store_id)
        batch = AdExecutionBatch(store_id=store_id, action=job_payload['action'],
                                 actor=job.created_by, preview_json=dumps(preview_plan),
                                 expires_at=utc_now() + timedelta(minutes=10))
        session.add(batch)
        session.flush()
    receipt = {'kind': 'ad_preview' if preview_task else 'ad_analysis',
               'store_id': store_id, 'version': summary['version'],
               'analyzed': count, 'result_sha256': digest, 'result_bytes': size}
    if preview_task:
        receipt['approval_id'] = batch.id
    if len(json.dumps(receipt, separators=(',', ':'))) > 300:
        raise ConflictError('广告任务回执超过存储限制')
    job.external_ref = json.dumps(receipt, separators=(',', ':'))
    session.flush()
    return completion_summary(job)


def apply_backtest_result(session: Session, job: Job, encoded: str) -> dict:
    payload, digest, size = _unpack(encoded)
    previous = _receipt(job)
    if previous:
        if previous.get('kind') != 'ad_backtest' or previous.get('result_sha256') != digest:
            raise ConflictError('广告回测任务已经写入不同结果')
        return completion_summary(job)
    store_id = loads(job.payload_json, {}).get('store_id')
    directory = get_settings().data_dir / 'ad_task_snapshots'
    try:
        snapshot_hash = (directory / f'{job.id}.sha256').read_text(encoding='ascii').strip()
        source_hash = (directory / f'{job.id}.source.sha256').read_text(encoding='ascii').strip()
    except OSError as error:
        raise ConflictError('广告回测来源快照不存在') from error
    row = payload.get('backtest')
    if (payload.get('schema') != 2 or payload.get('store_id') != store_id
            or payload.get('snapshot_sha256') != snapshot_hash
            or payload.get('source_sha256') != source_hash or not isinstance(row, dict)
            or row.get('store_id') != store_id
            or not re.fullmatch('[a-f0-9]{32}', str(row.get('id', '')))):
        raise ConflictError('广告回测结果与任务来源不一致')
    if analysis_source_fingerprint(session, store_id) != source_hash:
        raise ConflictError('店铺广告数据已变化，请重新发起回测')
    sku_count = len(set(session.scalars(select(AdSku.id).where(AdSku.store_id == store_id))))
    if not sku_count or type(row.get('sample_size')) is not int or row['sample_size'] != sku_count:
        raise ConflictError('广告回测样本数量不一致')
    if session.get(AdBacktestRun, row['id']):
        raise ConflictError('广告回测编号已存在')
    active = session.scalar(select(AdThresholdVersion.version).where(
        AdThresholdVersion.store_id == store_id, AdThresholdVersion.active.is_(True)).order_by(
        AdThresholdVersion.created_at.desc()))
    if row.get('threshold_version') != (active or ''):
        raise ConflictError('广告回测阈值版本不一致')
    try:
        summary = json.loads(row['summary_json'])
        if (summary.get('method') != 'CHRONOLOGICAL_REPLAY' or summary.get('future_leakage') is not False
                or not isinstance(summary.get('strategies'), list)):
            raise ValueError('bad summary')
        item = AdBacktestRun()
        for field in ('id', 'store_id', 'threshold_version', 'sample_size', 'false_kill_count',
                      'false_keep_count', 'potential_savings', 'summary_json'):
            setattr(item, field, row[field])
        item.created_at = datetime.fromisoformat(row['created_at'])
    except (KeyError, ValueError, TypeError) as error:
        raise ConflictError('广告回测结果字段无效') from error
    session.add(item)
    receipt = {'kind': 'ad_backtest', 'store_id': store_id, 'run_id': item.id,
               'sample_size': sku_count, 'result_sha256': digest, 'result_bytes': size}
    job.external_ref = json.dumps(receipt, separators=(',', ':'))
    session.flush()
    return completion_summary(job)
