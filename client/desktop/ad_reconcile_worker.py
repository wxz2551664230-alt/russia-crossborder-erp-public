"""Check uncertain Ozon advertisement writes on the original PC, read-only."""
import base64
import hashlib
from pathlib import Path
import re
import sys

from ad_worker import download_snapshot
from ad_execution_worker import export_result
from erp_desktop import TemporaryConnectionError


def reconcile_snapshot(database: Path, payload: dict, actor: str,
                       client_id: str, client_secret: str) -> dict:
    store_id = payload.get('store_id', '')
    approval_id = payload.get('approval_id', '')
    if (not re.fullmatch(r'[a-f0-9]{32}', store_id)
            or not re.fullmatch(r'[a-f0-9]{32}', approval_id)
            or not re.fullmatch(r'[a-f0-9]{32}', actor)
            or not client_id or not client_secret):
        raise ValueError('广告核对范围或临时凭据无效')
    runtime = Path(__file__).parent / 'runtime'
    sys.path.insert(0, str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2] / 'backend'))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from erp.core.config import get_settings
    from erp.models import AdExecutionBatch
    from erp.services import ad_execution
    from erp.integrations.ozon_performance import OzonPerformanceClient

    if get_settings().runtime_mode == 'server':
        raise ValueError('广告平台核对禁止在服务器运行')
    engine = create_engine('sqlite:///' + database.resolve().as_posix())
    remote = OzonPerformanceClient(client_id, client_secret)
    previous_client = ad_execution._client
    ad_execution._client = lambda *_: remote
    try:
        with Session(engine) as session:
            batch = session.get(AdExecutionBatch, approval_id)
            if not batch or batch.store_id != store_id or batch.actor != actor:
                raise ValueError('广告核对快照与原审批不一致')
            if batch.status in {'unknown', 'executing'}:
                return ad_execution.reconcile(session, approval_id, store_id)
            return ad_execution.summary(batch)
    finally:
        ad_execution._client = previous_client
        remote.session.close()
        engine.dispose()


def run(client, endpoint: str, job: dict, root: Path, lost) -> dict:
    if job.get('type') != 'ad_optimization_reconcile' or not re.fullmatch(r'[a-f0-9]{32}', job.get('id', '')):
        raise ValueError('广告只读核对任务类型无效')
    work = root / 'ad-tasks' / job['id']
    database, snapshot_hash, source_hash = download_snapshot(
        client, endpoint, job['attempts'], work, lost, reconcile_input=True)
    result_file = work / 'reconcile-result.json.gz'
    if not result_file.exists():
        credentials = client.request('GET', endpoint + '/ad-execution-credentials',
                                     extra_headers={'X-Worker-Attempt': str(job['attempts'])})[0]
        if credentials.get('store_id') != job['payload'].get('store_id'):
            raise ValueError('广告核对临时凭据与店铺不一致')
        reconcile_snapshot(database, job['payload'], job['created_by'],
                           credentials.get('performance_client_id', ''),
                           credentials.get('performance_client_secret', ''))
        if lost.is_set():
            raise ValueError('广告核对任务租约失效，已保留本机查询记录')
        export_result(database, job, snapshot_hash, source_hash, result_file)
    content = result_file.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    headers = {'X-Worker-Attempt': str(job['attempts'])}
    for retry in range(3):
        if lost.is_set():
            raise ValueError('广告核对任务租约失效，已保留本机回执')
        try:
            received = client.request('POST', endpoint + '/ad-reconcile-result',
                                      {'content': base64.b64encode(content).decode('ascii')},
                                      extra_headers=headers)[0]
            if (received.get('approval_id') != job['payload']['approval_id']
                    or received.get('result_sha256') != digest):
                raise ValueError('云端广告核对回执与本机不一致')
            return received
        except TemporaryConnectionError:
            if retry == 2 or lost.wait(5):
                raise
    raise AssertionError('广告核对回执重试次数异常')
