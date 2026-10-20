"""Stock and promotion writes on the initiating computer with durable receipts."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path


class FollowupOutcomeUnknown(ValueError):
    code = 'ozon_followup_outcome_unknown'


def execute_operation(snapshot, work, protect, post, check, adapter):
    path = Path(work) / 'operation.dpapi'; path.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.get('schema') != 1 or snapshot.get('phase') not in {'stock', 'promotion'}:
        raise ValueError('Ozon 后续阶段快照无效')
    state = None
    if path.exists():
        state = json.loads(protect(path.read_bytes(), decrypt=True))
        if state['job_id'] != snapshot['job_id'] or state['operation_hash'] != snapshot['operation_hash']:
            raise ValueError('阶段检查点与原任务不同')
        if state['state'] != 'done': raise FollowupOutcomeUnknown('原阶段写入结果未知，禁止自动重发')
    check()
    post('/catalog-ozon-followup-intent', {'operation_hash': snapshot['operation_hash']})
    if state is None:
        params = snapshot['params']; eligible = True
        if snapshot['phase'] == 'promotion':
            check()
            eligible = bool(adapter.find_promotion_candidate(params['action_id'], int(snapshot['product_id'])))
        if eligible:
            state = {'job_id': snapshot['job_id'], 'operation_hash': snapshot['operation_hash'], 'state': 'pending'}
            try:
                with path.open('xb') as stream:
                    stream.write(protect(json.dumps(state).encode())); stream.flush(); os.fsync(stream.fileno())
            except FileExistsError as error:
                raise FollowupOutcomeUnknown('已有原电脑写入检查点，禁止重复提交') from error
            check()
            try:
                if snapshot['phase'] == 'stock':
                    adapter.update_stock(snapshot['offer_id'], params['warehouse_id'], params['stock'])
                else:
                    adapter.activate_promotion(params['action_id'], int(snapshot['product_id']), params['action_price'], params['stock'])
            except Exception as error:
                raise FollowupOutcomeUnknown('Ozon 阶段写入未确认，请先核对平台，不要重发') from error
        state = {'job_id': snapshot['job_id'], 'operation_hash': snapshot['operation_hash'],
                 'state': 'done', 'result': {'ok': True, 'eligible': eligible}}
        temporary = path.with_suffix('.partial')
        temporary.write_bytes(protect(json.dumps(state).encode())); temporary.replace(path)
    return post('/catalog-ozon-followup-result', {'operation_hash': snapshot['operation_hash'], 'result': state['result']})


def run(client, endpoint, job, root, lost):
    from composition_worker import atomic_json
    if not re.fullmatch('[a-f0-9]{32}', job['id']) or job['type'] != 'catalog_ozon_followup':
        raise ValueError('产品库 Ozon 提交任务无效')
    work = Path(root) / 'catalog-ozon-followups' / job['id']
    work.mkdir(parents=True, exist_ok=True)
    source = work / 'input.json'; output = work / 'transport-result.json'
    atomic_json(source, {'endpoint': endpoint, 'job': job})
    output.unlink(missing_ok=True)
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--child', str(source.resolve())],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        while process.poll() is None:
            if lost.wait(.5):
                raise ValueError('任务连接失效，已保留原电脑建品回执')
        value = json.loads(output.read_text(encoding='utf-8'))
        if 'error' in value:
            error = FollowupOutcomeUnknown(value['error'])
            error.code = value['code']
            raise error
        return value['result']
    finally:
        if process.poll() is None:
            process.terminate(); process.wait(timeout=15)


def execute(source):
    from erp_desktop import Client, protect
    value = json.loads(source.read_text(encoding='utf-8'))
    job = value['job']; endpoint = value['endpoint']; work = source.parent
    if job['type'] != 'catalog_ozon_followup':
        raise ValueError('不是产品库 Ozon 正式提交任务')
    os.environ.update(ERP_DATA_DIR=str(work), ERP_DATABASE_URL='sqlite:///' + (work / 'unused.db').as_posix(),
                      ERP_MULTIUSER_ENABLED='false', ERP_RUNTIME_MODE='worker')
    runtime = Path(__file__).parent / 'runtime'
    sys.path.insert(0, str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2] / 'backend'))
    from erp.integrations.ozon import OzonSellerAdapter
    client = Client(); headers = {'X-Worker-Attempt': str(job['attempts'])}
    def post(suffix, body):
        from erp_desktop import TemporaryConnectionError
        import time
        for attempt in range(3):
            try:
                return client.request('POST', endpoint + suffix, body, extra_headers=headers)[0]
            except TemporaryConnectionError:
                if attempt == 2: raise
                time.sleep(2 ** attempt)

    def check(): post('/heartbeat', {})
    snapshot = client.request('GET', endpoint + '/catalog-ozon-followup-snapshot', extra_headers=headers)[0]
    finalized = execute_operation(snapshot, work, protect, post, check, OzonSellerAdapter(snapshot['config']))
    return {'catalog_ozon_finalized': True, 'status': finalized['status'], 'job_id': job['id']}


if __name__ == '__main__':
    from composition_worker import atomic_json
    source = Path(sys.argv[2]).resolve()
    try:
        value = {'result': execute(source)}
    except Exception as error:
        value = {'error': 'Ozon 建品执行或回传未完成，请保留原电脑检查点并核对原任务',
                 'code': getattr(error, 'code', 'local_execution_failed')}
    atomic_json(source.parent / 'transport-result.json', value)
