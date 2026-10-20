"""Run an approved original Ozon ad batch only on its initiating computer."""
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from ad_worker import download_snapshot
from erp_desktop import TemporaryConnectionError

MAX_PACKED = 12 * 1024 * 1024


def validate_platform_state(remote, batch) -> None:
    """Refuse an approved write when Ozon changed after the ERP preview."""
    plan = json.loads(batch.preview_json)
    by_campaign = {}
    for item in plan.get('items', []):
        campaign_id = str(item['campaign_id'])
        if campaign_id not in by_campaign:
            by_campaign[campaign_id] = remote.campaign_products(campaign_id)
        current = next((row for row in by_campaign[campaign_id]
                        if str(row.get('sku')) == str(item['sku_id'])), None)
        if current is None:
            raise ValueError('平台广告商品已变化，需重新同步并预览')
        if batch.action != 'STOP_ADS_CANDIDATE':
            target = current.get('targetCir')
            previous = item.get('previous_target_drr')
            if (target is None or previous is None or
                    abs(float(target) / 100 - float(previous)) > 0.00001):
                raise ValueError('平台当前目标 DRR 与审批前值不同，需重新同步并预览')


def execute_snapshot(database: Path, payload: dict, actor: str,
                     client_id: str, client_secret: str) -> dict:
    store_id = payload.get('store_id', '')
    approval_id = payload.get('approval_id', '')
    if (not re.fullmatch(r'[a-f0-9]{32}', store_id)
            or not re.fullmatch(r'[a-f0-9]{32}', approval_id)
            or not re.fullmatch(r'[a-f0-9]{32}', actor)
            or not client_id or not client_secret):
        raise ValueError('广告执行任务范围或临时凭据无效')
    runtime = Path(__file__).parent / 'runtime'
    sys.path.insert(0, str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2] / 'backend'))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from erp.core.config import get_settings
    from erp.models import AdExecutionBatch
    from erp.schemas import AdBatchExecutionRequest
    from erp.services import ad_execution
    from erp.integrations.ozon_performance import OzonPerformanceClient

    if get_settings().runtime_mode == 'server':
        raise ValueError('广告平台写入禁止在服务器运行')
    engine = create_engine('sqlite:///' + database.resolve().as_posix())
    remote = OzonPerformanceClient(client_id, client_secret)
    previous_client = ad_execution._client
    ad_execution._client = lambda *_: remote
    try:
        with Session(engine) as session:
            batch = session.get(AdExecutionBatch, approval_id)
            if not batch or batch.store_id != store_id or batch.actor != actor:
                raise ValueError('广告执行快照与审批人不一致')
            if batch.status == 'preview':
                request = AdBatchExecutionRequest(
                    action=payload['action'], state_ids=payload['state_ids'],
                    approval_id=approval_id, confirmed=True)
                try:
                    validate_platform_state(remote, batch)
                    return ad_execution.execute(session, store_id, request, actor)
                except Exception:
                    session.rollback()
                    session.refresh(batch)
                    if batch.status == 'preview':
                        # No durable operation intent exists yet. An expired
                        # approval or failed preflight can be released safely.
                        batch.status = 'stale'
                        session.commit()
                    elif batch.status != 'stale':
                        raise
            return ad_execution.summary(batch)
    finally:
        ad_execution._client = previous_client
        remote.session.close()
        engine.dispose()


def export_result(database: Path, job: dict, snapshot_hash: str,
                  source_hash: str, destination: Path) -> dict:
    runtime = Path(__file__).parent / 'runtime'
    sys.path.insert(0, str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2] / 'backend'))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from erp.core.jsonutil import loads
    from erp.models import AdAction, AdExecutionBatch
    from erp.services.ad_execution import summary

    payload = job['payload']
    engine = create_engine('sqlite:///' + database.resolve().as_posix())
    try:
        with Session(engine) as session:
            batch = session.get(AdExecutionBatch, payload['approval_id'])
            if not batch or batch.store_id != payload['store_id'] or batch.actor != job['created_by']:
                raise ValueError('本机广告结果与任务审批不一致')
            results = loads(batch.results_json, [])
            audit_ids = {row['audit_id'] for row in results if isinstance(row, dict) and row.get('audit_id')}
            actions = []
            for audit_id in sorted(audit_ids):
                action = session.get(AdAction, audit_id)
                if not action:
                    raise ValueError('本机广告结果缺少操作审计')
                actions.append({field: getattr(action, field) for field in (
                    'id', 'ad_sku_id', 'action', 'status', 'previous_target_drr',
                    'proposed_target_drr', 'reason', 'before_metrics_json',
                    'after_metrics_json', 'decided_at')})
            result = {'schema': 1, 'store_id': batch.store_id, 'approval_id': batch.id,
                      'snapshot_sha256': snapshot_hash, 'source_sha256': source_hash,
                      'batch_status': batch.status, 'results': results,
                      'actions': actions, 'summary': summary(batch)}
    finally:
        engine.dispose()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.partial')
    if destination.exists() or temporary.exists():
        raise ValueError('本机广告执行回执已存在')
    try:
        with temporary.open('xb') as output:
            with gzip.GzipFile(fileobj=output, mode='wb', mtime=0) as compressor:
                compressor.write(json.dumps(result, ensure_ascii=False, separators=(',', ':'),
                                            default=str).encode('utf-8'))
        if not temporary.stat().st_size or temporary.stat().st_size > MAX_PACKED:
            raise ValueError('本机广告执行回执超过传输限制')
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {'approval_id': payload['approval_id'], 'store_id': payload['store_id'],
            'batch_status': result['batch_status'],
            'result_sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
            'result_bytes': destination.stat().st_size}


def run(client, endpoint: str, job: dict, root: Path, lost) -> dict:
    if job.get('type') != 'ad_optimization_execute' or not re.fullmatch(r'[a-f0-9]{32}', job.get('id', '')):
        raise ValueError('广告正式执行任务类型无效')
    work = root / 'ad-tasks' / job['id']
    database, snapshot_hash, source_hash = download_snapshot(
        client, endpoint, job['attempts'], work, lost, execution_input=True)
    result_file = work / 'execution-result.json.gz'
    if not result_file.exists():
        credentials = client.request('GET', endpoint + '/ad-execution-credentials',
                                     extra_headers={'X-Worker-Attempt': str(job['attempts'])})[0]
        if credentials.get('store_id') != job['payload'].get('store_id'):
            raise ValueError('广告执行临时凭据与店铺不一致')
        execute_snapshot(database, job['payload'], job['created_by'],
                         credentials.get('performance_client_id', ''),
                         credentials.get('performance_client_secret', ''))
        if lost.is_set():
            raise ValueError('广告执行任务租约失效，已保留本机操作记录')
        export_result(database, job, snapshot_hash, source_hash, result_file)
    content = result_file.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    headers = {'X-Worker-Attempt': str(job['attempts'])}
    for retry in range(3):
        if lost.is_set():
            raise ValueError('广告执行任务租约失效，已保留本机回执')
        try:
            received = client.request('POST', endpoint + '/ad-execution-result',
                                      {'content': base64.b64encode(content).decode('ascii')},
                                      extra_headers=headers)[0]
            if received.get('approval_id') != job['payload']['approval_id'] or received.get('result_sha256') != digest:
                raise ValueError('云端广告执行回执与本机不一致')
            return received
        except TemporaryConnectionError:
            if retry == 2 or lost.wait(5):
                raise
    raise AssertionError('广告执行回执重试次数异常')
