"""Durable personal-PC import; unknown submissions are never automatically resent."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path


class ImportOutcomeUnknown(ValueError):
    code = 'ozon_import_recovery_required'


def submit(snapshot, work, protect, post, check, adapter):
    if snapshot.get('schema') != 1:
        raise ValueError('Ozon 发布快照版本无效')
    path = Path(work) / 'import-receipt.dpapi'
    path.parent.mkdir(parents=True, exist_ok=True)
    def save(value, exclusive=False):
        if exclusive:
            try:
                with path.open('xb') as stream:
                    stream.write(protect(json.dumps(value).encode()))
                    stream.flush(); os.fsync(stream.fileno())
            except FileExistsError as error:
                raise ImportOutcomeUnknown('原电脑已有提交检查点，禁止并发重复建品') from error
            return
        temporary = path.with_suffix('.partial')
        temporary.write_bytes(protect(json.dumps(value).encode()))
        temporary.replace(path)
    state = {'job_id': snapshot['job_id'], 'item_hash': snapshot['item_hash'], 'state': 'new'}
    if path.exists():
        state = json.loads(protect(path.read_bytes(), decrypt=True))
        if state['job_id'] != snapshot['job_id'] or state['item_hash'] != snapshot['item_hash']:
            raise ValueError('原电脑回执与当前发布包不同')
    task_id = str(state.get('task_id') or snapshot.get('task_id') or '')
    check()
    if not task_id:
        if state['state'] != 'new' or snapshot.get('status') == 'import_uncertain':
            raise ImportOutcomeUnknown('建品结果未知，请先查询原 task_id，禁止重新建品')
        post('/catalog-ozon-import-intent', {'item_hash': snapshot['item_hash']})
        state['state'] = 'pending'
        save(state, exclusive=True)
        check()
        try:
            task_id = str(adapter.import_product(snapshot['item']))
            if not task_id.isdigit() or int(task_id) <= 0:
                raise ValueError('Missing task ID')
        except Exception as error:
            raise ImportOutcomeUnknown('Ozon 创建结果未确认，请先核对原任务，禁止自动重发') from error
        state.update(state='task_received', task_id=task_id)
        save(state)
    else:
        # Existing task ID is a read-only recovery, not another import.
        post('/catalog-ozon-import-intent', {'item_hash': snapshot['item_hash']})
    post('/catalog-ozon-import-task', {'item_hash': snapshot['item_hash'], 'task_id': int(task_id)})
    check()
    if state.get('state') == 'result_received':
        result = state['result']
    else:
        result = adapter.wait_import(int(task_id))
        from erp.integrations.ozon import import_failed, import_succeeded
        state.update(state='result_received' if import_failed(result) or import_succeeded(result) else 'pending_result',
                     task_id=task_id, result=result)
        save(state)
    return post('/catalog-ozon-import-result', {'item_hash': snapshot['item_hash'], 'task_id': int(task_id), 'result': result})


def run(client, endpoint, job, root, lost):
    from composition_worker import atomic_json
    if not re.fullmatch('[a-f0-9]{32}', job['id']) or job['type'] != 'catalog_ozon_publication':
        raise ValueError('产品库 Ozon 提交任务无效')
    work = Path(root) / 'catalog-ozon-imports' / job['id']
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
            error = ImportOutcomeUnknown(value['error'])
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
    if job['type'] != 'catalog_ozon_publication':
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
    snapshot = client.request('GET', endpoint + '/catalog-ozon-import-snapshot', extra_headers=headers)[0]
    result = submit(snapshot, work, protect, post, check, OzonSellerAdapter(snapshot['config']))
    finalized = post('/catalog-ozon-import-finish', {'result': result})
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
