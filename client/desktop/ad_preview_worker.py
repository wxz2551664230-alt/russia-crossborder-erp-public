"""Calculate the original reviewed advertisement plan on the initiating PC."""
from pathlib import Path
import base64
import hashlib
import re
import sys

from ad_worker import analyze_snapshot, download_snapshot, export_analysis_result
from erp_desktop import TemporaryConnectionError


def preview_snapshot(database: Path, store_id: str, action: str,
                     state_ids: list[str]) -> dict:
    if (not re.fullmatch(r'[a-f0-9]{32}', store_id)
            or action not in {'STOP_ADS_CANDIDATE', 'SCALE_CANDIDATES'}
            or not isinstance(state_ids, list) or not 1 <= len(state_ids) <= 500
            or any(not re.fullmatch(r'[a-f0-9]{32}', value) for value in state_ids)):
        raise ValueError('广告预览任务参数无效')
    analysis = analyze_snapshot(database, store_id)
    runtime = Path(__file__).parent / 'runtime'
    sys.path.insert(0, str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2] / 'backend'))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from erp.services.ad_execution import _plan, _signature

    engine = create_engine('sqlite:///' + database.resolve().as_posix())
    try:
        with Session(engine) as session:
            plan = _plan(session, store_id, action, state_ids)
    finally:
        engine.dispose()
    if not plan['items']:
        raise ValueError('所选广告候选均未通过原执行安全门')
    return {'analysis': analysis, 'plan': plan, 'signature': _signature(plan)}


def run(client, endpoint: str, job: dict, root: Path, lost) -> dict:
    if job.get('type') != 'ad_optimization_preview' or not re.fullmatch(r'[a-f0-9]{32}', job.get('id', '')):
        raise ValueError('广告预览任务类型无效')
    payload = job.get('payload', {})
    store_id = payload.get('store_id', '')
    action = payload.get('action', '')
    state_ids = payload.get('state_ids', [])
    work = root / 'ad-tasks' / job['id']
    database, snapshot_hash, source_hash = download_snapshot(
        client, endpoint, job['attempts'], work, lost)
    result_file = work / 'preview-result.json.gz'
    if not result_file.exists():
        preview = preview_snapshot(database, store_id, action, state_ids)
        if lost.is_set():
            raise ValueError('广告预览任务租约失效')
        export_analysis_result(database, preview['analysis'], snapshot_hash, source_hash,
                               result_file, plan=preview['plan'])
    content = result_file.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    headers = {'X-Worker-Attempt': str(job['attempts'])}
    for retry in range(3):
        if lost.is_set():
            raise ValueError('广告预览任务租约失效，已保留本机结果')
        try:
            receipt = client.request('POST', endpoint + '/ad-analysis-result',
                                     {'content': base64.b64encode(content).decode('ascii')},
                                     extra_headers=headers)[0]
            if (receipt.get('store_id') != store_id or receipt.get('result_sha256') != digest
                    or not re.fullmatch(r'[a-f0-9]{32}', receipt.get('approval_id', ''))):
                raise ValueError('云端广告预览回执与本机结果不一致')
            return receipt
        except TemporaryConnectionError:
            if retry == 2 or lost.wait(5):
                raise
    raise AssertionError('广告预览回执重试次数异常')
