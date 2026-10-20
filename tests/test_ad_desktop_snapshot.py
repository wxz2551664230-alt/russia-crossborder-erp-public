from datetime import date
from datetime import timedelta
import base64
import gzip
import hashlib
import json
import os
import pytest
import sqlite3
from pathlib import Path
import subprocess
import sys
import threading
import urllib.request

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from erp.core.database import SessionLocal
from erp.models import AdDailyMetric, AdSku, Job, Store, SystemSetting, utc_now
from erp.services.ad_desktop_snapshot import create_analysis_snapshot, analysis_download
from erp.services.ad_optimization import analyze_store
from test_personal_job_controls import team


def test_single_store_snapshot_runs_original_analysis_without_secrets(tmp_path):
    with SessionLocal() as source:
        source.add_all([
            Store(id='a' * 32, platform='ozon', name='A', secrets_encrypted='private-token'),
            Store(id='b' * 32, platform='ozon', name='B', secrets_encrypted='other-token'),
            SystemSetting(key='defaults', value_json='{"financial_policy":{"planned_ad_drr":0.1}}'),
        ])
        source.flush()
        source.add_all([
            AdSku(id='c' * 32, store_id='a' * 32, sku_id='A-SKU', name='A'),
            AdSku(id='d' * 32, store_id='b' * 32, sku_id='B-SKU', name='B'),
        ])
        source.flush()
        source.add(AdDailyMetric(id='e' * 32, ad_sku_id='c' * 32, metric_date=date(2026, 9, 23), impressions=100, clicks=3))
        source.commit()
        path = tmp_path / 'ad-task.sqlite'
        counts = create_analysis_snapshot(source, 'a' * 32, path)
        assert counts['ad_skus'] == counts['ad_daily_metrics'] == 1
        with sqlite3.connect(path) as check:
            assert check.execute('select count(*) from stores').fetchone()[0] == 1
            assert check.execute('select secrets_encrypted from stores').fetchone()[0] == ''
            assert check.execute('select sku_id from ad_skus').fetchone()[0] == 'A-SKU'
            assert check.execute('select count(*) from ad_skus where store_id=?', ('b' * 32,)).fetchone()[0] == 0
        engine = create_engine('sqlite:///' + path.as_posix())
        try:
            with Session(engine) as worker:
                assert analyze_store(worker, 'a' * 32)['analyzed'] == 1
        finally:
            engine.dispose()


def test_private_ad_snapshot_requires_owner_and_live_lease(team, tmp_path, monkeypatch):
    from erp.core.config import get_settings
    clients, worker = team
    monkeypatch.setattr(get_settings(), 'data_dir', tmp_path)
    with SessionLocal() as db:
        store = Store(platform='ozon', name='private ad store', enabled=True,
                      secrets_encrypted='never-send-this-key')
        db.add(store)
        db.flush()
        db.add(AdSku(store_id=store.id, sku_id='PRIVATE-SKU', name='test'))
        db.commit()
        store_id = store.id
    owner = clients['owner']
    owner.post('/api/v1/execution-devices/local/register', json={'id': worker, 'name': 'PC'})
    with SessionLocal() as db:
        from erp.models import WorkerNode
        device = db.get(WorkerNode, worker)
        job = Job(type='ad_optimization_analyze', created_by=device.owner_user_id,
                  target_worker_id=worker, idempotency_key='test-ad-snapshot',
                  payload_json='{"store_id":"' + store_id + '"}',
                  status='running', lease_owner=worker, attempts=1,
                  lease_expires_at=utc_now() + timedelta(minutes=5))
        db.add(job)
        db.commit()
        job_id = job.id
    url = f'/api/v1/execution-devices/local/{worker}/jobs/{job_id}/ad-analysis-snapshot'
    headers = {'X-Worker-Attempt': '1'}
    for name in ('other', 'admin', 'viewer'):
        assert clients[name].get(url, headers=headers).status_code == 403
    assert owner.get(url, headers={'X-Worker-Attempt': '2'}).status_code == 409
    response = owner.get(url, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    assert hashlib.sha256(response.content).hexdigest() == response.headers['x-content-sha256']
    assert owner.get(url, headers=headers).content == response.content
    extracted = tmp_path / 'received.sqlite'
    extracted.write_bytes(gzip.decompress(response.content))
    with sqlite3.connect(extracted) as check:
        assert check.execute('select count(*) from stores').fetchone()[0] == 1
        assert check.execute('select secrets_encrypted from stores').fetchone()[0] == ''
        assert check.execute('select sku_id from ad_skus').fetchone()[0] == 'PRIVATE-SKU'
    with SessionLocal() as db:
        db.get(Job, job_id).lease_expires_at = utc_now() - timedelta(seconds=1)
        db.commit()
    assert owner.get(url, headers=headers).status_code == 409


def test_desktop_original_analysis_runs_on_downloaded_store_only(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from ad_worker import analyze_snapshot, export_analysis_result
    with SessionLocal() as source:
        store = Store(id='1' * 32, platform='ozon', name='A', enabled=True)
        unrelated = Store(id='2' * 32, platform='ozon', name='B', enabled=True)
        source.add_all([store, unrelated])
        source.flush()
        source.add_all([AdSku(id='3' * 32, store_id=store.id, sku_id='ONE'),
                        AdSku(id='4' * 32, store_id=unrelated.id, sku_id='TWO')])
        source.commit()
        database = tmp_path / 'personal.sqlite'
        create_analysis_snapshot(source, store.id, database)
    result = analyze_snapshot(database, '1' * 32)
    assert result['analyzed'] == 1
    with sqlite3.connect(database) as check:
        assert check.execute('select count(*) from ad_sku_states').fetchone()[0] == 1
        assert check.execute('select count(*) from ad_skus').fetchone()[0] == 1
    result_file = tmp_path / 'result.json.gz'
    receipt = export_analysis_result(database, result, 'f' * 64, 'e' * 64, result_file)
    assert receipt['analyzed'] == 1
    assert hashlib.sha256(result_file.read_bytes()).hexdigest() == receipt['result_sha256']
    import json
    payload = json.loads(gzip.decompress(result_file.read_bytes()))
    assert payload['snapshot_sha256'] == 'f' * 64
    assert payload['source_sha256'] == 'e' * 64
    assert len(payload['thresholds']) == len(payload['states']) == len(payload['histories']) == 1
    assert len(payload['labels']) == 1
    assert payload['states'][0]['ad_sku_id'] == '3' * 32
    assert b'TWO' not in gzip.decompress(result_file.read_bytes())


def test_desktop_download_checks_digest_and_sqlite(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from ad_worker import download_snapshot
    source = tmp_path / 'source.sqlite'
    with sqlite3.connect(source) as db:
        db.execute('create table sample (value text)')
        db.execute("insert into sample values ('ok')")
    packed = gzip.compress(source.read_bytes())
    correct = hashlib.sha256(packed).hexdigest()
    expected = [correct]
    class Response:
        def __init__(self):
            from io import BytesIO
            self.stream = BytesIO(packed)
            self.headers = {'X-Content-SHA256': expected[0], 'X-Source-SHA256': 'e' * 64}
        def __enter__(self): return self
        def __exit__(self, *_): self.stream.close()
        def read(self, n): return self.stream.read(n)
    class Opener:
        def open(self, request, timeout):
            assert request.get_header('X-worker-attempt') == '1'
            assert request.get_header('Cookie') == 'erp_session=test-cookie'
            return Response()
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *_: Opener())
    class Client:
        base = 'https://erp.example.test'
        profile = {'cookie': 'test-cookie'}
    endpoint = '/execution-devices/local/pc-' + 'a' * 32 + '/jobs/' + 'b' * 32
    work = tmp_path / 'task'
    expected[0] = '0' * 64
    try:
        download_snapshot(Client(), endpoint, 1, work, threading.Event())
        assert False, 'bad digest accepted'
    except ValueError as error:
        assert 'SHA256' in str(error)
    assert not (work / 'input.sqlite').exists()
    expected[0] = correct
    database, digest, source_digest = download_snapshot(Client(), endpoint, 1, work, threading.Event())
    assert digest == correct
    assert source_digest == 'e' * 64
    with sqlite3.connect(database) as check:
        assert check.execute('select value from sample').fetchone()[0] == 'ok'


def test_ad_result_applies_once_and_rejects_stale_source(team, tmp_path, monkeypatch):
    from erp.core.config import get_settings
    from erp.models import WorkerNode, AdSkuState, AdSkuStateHistory
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from ad_worker import analyze_snapshot, export_analysis_result
    clients, worker = team
    monkeypatch.setattr(get_settings(), 'data_dir', tmp_path)
    owner = clients['owner']
    with SessionLocal() as db:
        store = Store(platform='ozon', name='result store', enabled=True)
        db.add(store); db.flush()
        sku = AdSku(store_id=store.id, sku_id='RESULT-SKU', name='test')
        db.add(sku); db.commit()
        store_id, sku_id = store.id, sku.id
        device = db.get(WorkerNode, worker)
        job = Job(type='ad_optimization_analyze', created_by=device.owner_user_id,
                  target_worker_id=worker, idempotency_key='test-ad-result',
                  payload_json='{"store_id":"' + store_id + '"}',
                  status='running', lease_owner=worker, attempts=1,
                  lease_expires_at=utc_now() + timedelta(minutes=5))
        db.add(job); db.commit(); job_id = job.id
    url = f'/api/v1/execution-devices/local/{worker}/jobs/{job_id}'
    headers = {'X-Worker-Attempt': '1'}
    response = owner.get(url + '/ad-analysis-snapshot', headers=headers)
    assert response.status_code == 200, response.text
    database = tmp_path / 'worker.sqlite'
    database.write_bytes(gzip.decompress(response.content))
    result = analyze_snapshot(database, store_id)
    file = tmp_path / 'result.gz'
    receipt = export_analysis_result(database, result, response.headers['x-content-sha256'],
                                     response.headers['x-source-sha256'], file)
    content = base64.b64encode(file.read_bytes()).decode('ascii')
    for name in ('other', 'admin', 'viewer'):
        assert clients[name].post(url + '/ad-analysis-result', headers=headers,
                                  json={'content': content}).status_code == 403
    assert owner.post(url + '/ad-analysis-result', headers={'X-Worker-Attempt': '2'},
                      json={'content': content}).status_code == 409
    applied = owner.post(url + '/ad-analysis-result', headers=headers, json={'content': content})
    assert applied.status_code == 200, applied.text
    assert applied.json() == receipt
    assert owner.post(url + '/ad-analysis-result', headers=headers, json={'content': content}).json() == receipt
    with SessionLocal() as db:
        assert db.scalar(select(AdSkuState).where(AdSkuState.ad_sku_id == sku_id)) is not None
        assert len(db.scalars(select(AdSkuStateHistory).where(AdSkuStateHistory.ad_sku_id == sku_id)).all()) == 1
    assert owner.post(url + '/complete', headers=headers, json={'result': {}}).status_code == 409
    completed = owner.post(url + '/complete', headers=headers, json={'result': receipt})
    assert completed.status_code == 200, completed.text
    assert owner.post(url + '/complete', headers=headers, json={'result': receipt}).status_code == 200


def test_ad_result_rejects_source_changed_after_snapshot(team, tmp_path, monkeypatch):
    from erp.core.config import get_settings
    from erp.models import WorkerNode, AdSkuState
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from ad_worker import analyze_snapshot, export_analysis_result
    clients, worker = team
    monkeypatch.setattr(get_settings(), 'data_dir', tmp_path)
    owner = clients['owner']
    with SessionLocal() as db:
        store = Store(platform='ozon', name='changed store', enabled=True)
        db.add(store); db.flush()
        sku = AdSku(store_id=store.id, sku_id='CHANGED-SKU', name='before')
        db.add(sku); db.commit()
        store_id, sku_id = store.id, sku.id
        device = db.get(WorkerNode, worker)
        job = Job(type='ad_optimization_analyze', created_by=device.owner_user_id,
                  target_worker_id=worker, idempotency_key='test-ad-stale',
                  payload_json='{"store_id":"' + store_id + '"}', status='running',
                  lease_owner=worker, attempts=1, lease_expires_at=utc_now() + timedelta(minutes=5))
        db.add(job); db.commit(); job_id = job.id
    url = f'/api/v1/execution-devices/local/{worker}/jobs/{job_id}'
    headers = {'X-Worker-Attempt': '1'}
    response = owner.get(url + '/ad-analysis-snapshot', headers=headers)
    database = tmp_path / 'worker.sqlite'
    database.write_bytes(gzip.decompress(response.content))
    result = analyze_snapshot(database, store_id)
    file = tmp_path / 'result.gz'
    export_analysis_result(database, result, response.headers['x-content-sha256'],
                           response.headers['x-source-sha256'], file)
    with SessionLocal() as db:
        db.get(AdSku, sku_id).name = 'after'
        db.commit()
    rejected = owner.post(url + '/ad-analysis-result', headers=headers,
                          json={'content': base64.b64encode(file.read_bytes()).decode('ascii')})
    assert rejected.status_code == 409, rejected.text
    with SessionLocal() as db:
        assert db.scalar(select(AdSkuState).where(AdSkuState.ad_sku_id == sku_id)) is None


def test_ad_job_is_claimed_and_run_only_on_initiating_pc(team, tmp_path, monkeypatch):
    from erp.core.config import get_settings
    from erp.models import AdSkuState
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from ad_worker import run
    clients, worker = team
    monkeypatch.setattr(get_settings(), 'data_dir', tmp_path)
    owner = clients['owner']
    with SessionLocal() as db:
        store = Store(platform='ozon', name='personal run', enabled=True)
        db.add(store); db.flush()
        sku = AdSku(store_id=store.id, sku_id='RUN-SKU', name='test')
        db.add(sku); db.commit(); store_id, sku_id = store.id, sku.id
    assert owner.post('/api/v1/execution-devices/local/register',
                      json={'id': worker, 'name': 'PC', 'ad_analysis_version': 1}).status_code == 200
    queued = owner.post(f'/api/v1/ad-optimization/jobs/analyze?store_id={store_id}')
    assert queued.status_code == 202, queued.text
    job_id = queued.json()['job_id']
    with SessionLocal() as db:
        row = db.get(Job, job_id)
        assert row.target_worker_id == worker and row.created_by
    base = f'/api/v1/execution-devices/local/{worker}'
    claimed = owner.post(base + '/claim').json()
    assert claimed['id'] == job_id and claimed['attempts'] == 1
    endpoint = f'/execution-devices/local/{worker}/jobs/{job_id}'
    class Response:
        def __init__(self, result):
            from io import BytesIO
            self.stream = BytesIO(result.content)
            self.headers = result.headers
        def __enter__(self): return self
        def __exit__(self, *_): self.stream.close()
        def read(self, n): return self.stream.read(n)
    class Opener:
        def open(self, request, timeout):
            response = owner.get(request.full_url.removeprefix('https://erp.example.test'),
                                 headers={'X-Worker-Attempt': '1'})
            assert response.status_code == 200, response.text
            return Response(response)
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *_: Opener())
    class Client:
        base = 'https://erp.example.test'
        profile = {'cookie': 'test-cookie'}
        def request(self, method, path, body, *, extra_headers=None):
            response = owner.request(method, '/api/v1' + path, json=body, headers=extra_headers)
            assert response.status_code == 200, response.text
            return response.json(), response.headers
    receipt = run(Client(), endpoint, claimed, tmp_path / 'pc', threading.Event())
    assert receipt['analyzed'] == 1
    completed = owner.post(base + f'/jobs/{job_id}/complete', headers={'X-Worker-Attempt': '1'},
                           json={'result': receipt})
    assert completed.status_code == 200, completed.text
    assert not (tmp_path / 'ad_task_snapshots' / (job_id + '.sqlite.gz')).exists()
    with SessionLocal() as db:
        assert db.get(Job, job_id).status == 'completed'
        assert db.scalar(select(AdSkuState).where(AdSkuState.ad_sku_id == sku_id)) is not None


def test_ad_completed_input_cleanup_keeps_audit_receipt(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from ad_worker import cleanup_completed_input
    job_id = 'a' * 32
    work = tmp_path / 'ad-tasks' / job_id
    work.mkdir(parents=True)
    for name in ('input.sqlite', 'input.sqlite.gz', 'input.sha256', 'source.sha256',
                 'analysis-result.json.gz'):
        (work / name).write_bytes(b'x')
    cleanup_completed_input(tmp_path, job_id)
    assert [p.name for p in work.iterdir()] == ['analysis-result.json.gz']


def test_ad_backtest_runs_on_original_pc_and_writes_one_cloud_run(team, tmp_path, monkeypatch):
    from erp.core.config import get_settings
    from erp.models import AdBacktestRun, AdSkuState, AdThresholdVersion
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from ad_worker import run
    clients, worker = team
    monkeypatch.setattr(get_settings(), 'data_dir', tmp_path)
    owner = clients['owner']
    with SessionLocal() as db:
        store = Store(platform='ozon', name='backtest store', enabled=True)
        db.add(store); db.flush()
        sku = AdSku(store_id=store.id, sku_id='BACKTEST-SKU', name='test')
        db.add(sku); db.flush()
        db.add_all([
            AdDailyMetric(ad_sku_id=sku.id, metric_date=date(2026, 9, 22), impressions=100, clicks=5),
            AdDailyMetric(ad_sku_id=sku.id, metric_date=date(2026, 9, 23), impressions=200, clicks=9),
            AdSkuState(ad_sku_id=sku.id, state='OBSERVATION'),
            AdThresholdVersion(store_id=store.id, version='TV_TEST', cohort_key='全店',
                               cohort_level='store', active=True),
        ])
        db.commit(); store_id = store.id
    assert owner.post('/api/v1/execution-devices/local/register',
                      json={'id': worker, 'name': 'PC', 'ad_backtest_version': 1}).status_code == 200
    queued = owner.post(f'/api/v1/ad-optimization/jobs/backtest?store_id={store_id}')
    assert queued.status_code == 202, queued.text
    job_id = queued.json()['job_id']
    base = f'/api/v1/execution-devices/local/{worker}'
    claimed = owner.post(base + '/claim').json()
    assert claimed['id'] == job_id
    endpoint = f'/execution-devices/local/{worker}/jobs/{job_id}'
    class Response:
        def __init__(self, result):
            from io import BytesIO
            self.stream = BytesIO(result.content)
            self.headers = result.headers
        def __enter__(self): return self
        def __exit__(self, *_): self.stream.close()
        def read(self, n): return self.stream.read(n)
    class Opener:
        def open(self, request, timeout):
            response = owner.get(request.full_url.removeprefix('https://erp.example.test'),
                                 headers={'X-Worker-Attempt': '1'})
            assert response.status_code == 200, response.text
            return Response(response)
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *_: Opener())
    class Client:
        base = 'https://erp.example.test'
        profile = {'cookie': 'test-cookie'}
        def request(self, method, path, body, *, extra_headers=None):
            response = owner.request(method, '/api/v1' + path, json=body, headers=extra_headers)
            assert response.status_code == 200, response.text
            return response.json(), response.headers
    receipt = run(Client(), endpoint, claimed, tmp_path / 'pc', threading.Event())
    assert receipt['sample_size'] == 1
    encoded = base64.b64encode((tmp_path / 'pc' / 'ad-tasks' / job_id / 'backtest-result.json.gz').read_bytes()).decode('ascii')
    assert owner.post(base + f'/jobs/{job_id}/ad-backtest-result', headers={'X-Worker-Attempt': '1'},
                      json={'content': encoded}).json() == receipt
    assert clients['other'].post(base + f'/jobs/{job_id}/ad-backtest-result',
                                 headers={'X-Worker-Attempt': '1'}, json={'content': encoded}).status_code == 403
    assert owner.post(base + f'/jobs/{job_id}/ad-backtest-result',
                      headers={'X-Worker-Attempt': '2'}, json={'content': encoded}).status_code == 409
    with SessionLocal() as db:
        assert db.get(AdBacktestRun, receipt['run_id']) is not None
    assert owner.post(base + f'/jobs/{job_id}/complete', headers={'X-Worker-Attempt': '1'},
                      json={'result': {}}).status_code == 409
    assert owner.post(base + f'/jobs/{job_id}/complete', headers={'X-Worker-Attempt': '1'},
                      json={'result': receipt}).status_code == 200
    with SessionLocal() as db:
        assert db.get(Job, job_id).status == 'completed'
        assert len(db.scalars(select(AdBacktestRun).where(AdBacktestRun.store_id == store_id)).all()) == 1


def test_ad_sync_input_and_credentials_are_leased_to_one_pc(team, tmp_path, monkeypatch):
    from erp.core.config import get_settings
    from erp.core.security import vault
    from erp.models import WorkerNode, IntegrationConnection
    clients, worker = team
    monkeypatch.setattr(get_settings(), 'data_dir', tmp_path)
    owner = clients['owner']
    with SessionLocal() as db:
        connection = IntegrationConnection(provider='ozon', name='sync test',
                                           base_url='https://api-seller.ozon.ru',
                                           secrets_encrypted=vault.encrypt({'api_key': 'connection-private'}))
        db.add(connection); db.flush()
        store = Store(platform='ozon', name='sync store', enabled=True,
                      connection_id=connection.id, external_account_id='seller-client-id',
                      config_json='{"request_timeout":55}',
                      secrets_encrypted=vault.encrypt({
                          'client_id': 'seller-client-id',
                          'performance_client_id': 'ad-client-id',
                          'performance_client_secret': 'ad-private-secret'}))
        unrelated = Store(platform='ozon', name='other sync store', enabled=True,
                          secrets_encrypted=vault.encrypt({'api_key': 'other-private'}))
        db.add_all([store, unrelated]); db.flush()
        db.add_all([AdSku(store_id=store.id, sku_id='THIS-SKU'),
                    AdSku(store_id=unrelated.id, sku_id='OTHER-SKU')])
        db.commit(); store_id = store.id
        device = db.get(WorkerNode, worker)
        job = Job(type='ad_optimization_sync', created_by=device.owner_user_id,
                  target_worker_id=worker, idempotency_key='test-ad-sync-private',
                  payload_json='{"store_id":"' + store_id + '"}', status='running',
                  lease_owner=worker, attempts=1, lease_expires_at=utc_now() + timedelta(minutes=5))
        db.add(job); db.commit(); job_id = job.id
    url = f'/api/v1/execution-devices/local/{worker}/jobs/{job_id}'
    headers = {'X-Worker-Attempt': '1'}
    for suffix in ('ad-sync-snapshot', 'ad-sync-credentials'):
        for name in ('other', 'admin', 'viewer'):
            assert clients[name].get(url + '/' + suffix, headers=headers).status_code == 403
        assert owner.get(url + '/' + suffix, headers={'X-Worker-Attempt': '2'}).status_code == 409
    response = owner.get(url + '/ad-sync-snapshot', headers=headers)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    database = tmp_path / 'sync-input.sqlite'
    database.write_bytes(gzip.decompress(response.content))
    with sqlite3.connect(database) as check:
        assert check.execute('select count(*) from stores').fetchone()[0] == 1
        assert check.execute('select secrets_encrypted,config_json from stores').fetchone() == ('', '{}')
        assert check.execute('select sku_id from ad_skus').fetchone()[0] == 'THIS-SKU'
    assert b'ad-private-secret' not in database.read_bytes()
    credentials = owner.get(url + '/ad-sync-credentials', headers=headers)
    assert credentials.status_code == 200 and credentials.headers['cache-control'] == 'no-store'
    config = credentials.json()['config']
    assert credentials.json()['store_id'] == store_id
    assert config['secrets']['performance_client_secret'] == 'ad-private-secret'
    assert config['secrets']['api_key'] == 'connection-private'
    assert config['request_timeout'] == 55


def test_sync_task_recreates_original_config_without_server_master_key(tmp_path):
    from cryptography.fernet import Fernet
    from erp.core.security import vault
    from erp.services.config_resolution import build_job_snapshot, resolve_job_snapshot
    with SessionLocal() as source:
        store = Store(id='9' * 32, platform='ozon', name='sync config test', enabled=True,
                      external_account_id='seller-id', config_json='{"request_timeout":52}',
                      secrets_encrypted=vault.encrypt({'client_id': 'seller-id', 'api_key': 'seller-secret',
                                                       'performance_client_id': 'ad-id',
                                                       'performance_client_secret': 'ad-secret'}))
        source.add(store); source.commit()
        config = resolve_job_snapshot(build_job_snapshot(source, store_id=store.id))
        database = tmp_path / 'input.sqlite'
        create_analysis_snapshot(source, store.id, database, sync_input=True)
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env['ERP_SECRET_KEY'] = Fernet.generate_key().decode('ascii')
    env['ERP_DATA_DIR'] = str(tmp_path)
    env['ERP_DATABASE_URL'] = 'sqlite:///' + database.as_posix()
    env['ERP_RUNTIME_MODE'] = 'worker'
    env['PYTHONPATH'] = os.pathsep.join((str(root / 'client' / 'desktop'), str(root / 'backend')))
    script = '''import json,sys
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from ad_sync_worker import prepare_sync_database,scrub_sync_credentials
from erp.services.config_resolution import build_job_snapshot,resolve_job_snapshot
database=Path(sys.argv[1]); store_id=sys.argv[2]; config=json.load(sys.stdin)
prepare_sync_database(database,store_id,config)
engine=create_engine('sqlite:///'+database.as_posix())
with Session(engine) as db:
    actual=resolve_job_snapshot(build_job_snapshot(db,store_id=store_id))
    assert actual['request_timeout']==config['request_timeout']
    assert actual['secrets']==config['secrets']
engine.dispose()
scrub_sync_credentials(database,store_id)
'''
    result = subprocess.run([sys.executable, '-c', script, str(database), '9' * 32],
                            input=json.dumps(config), text=True, env=env, capture_output=True)
    assert result.returncode == 0, result.stderr[-500:]
    with sqlite3.connect(database) as check:
        assert check.execute('select secrets_encrypted,config_json from stores').fetchone() == ('', '{}')


def test_original_ad_sync_runs_only_in_isolated_database(tmp_path, monkeypatch):
    from erp.core.security import vault
    from erp.services.config_resolution import build_job_snapshot, resolve_job_snapshot
    from erp.api.routes import ad_optimization
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from ad_sync_worker import prepare_sync_database, execute_sync_database, export_sync_delta
    with SessionLocal() as source:
        store = Store(id='8' * 32, platform='ozon', name='isolated sync', enabled=True,
                      external_account_id='seller-id',
                      secrets_encrypted=vault.encrypt({'client_id': 'seller-id', 'api_key': 'seller-key',
                                                       'performance_client_id': 'ad-id',
                                                       'performance_client_secret': 'ad-key'}))
        source.add(store); source.commit()
        source.add(AdSku(store_id=store.id, sku_id='OLD', name='Old'))
        source.commit()
        config = resolve_job_snapshot(build_job_snapshot(source, store_id=store.id))
        database = tmp_path / 'input.sqlite'
        create_analysis_snapshot(source, store.id, database, sync_input=True)
    frozen = tmp_path / 'input.sqlite.gz'
    frozen.write_bytes(gzip.compress(database.read_bytes(), mtime=0))
    frozen_hash = hashlib.sha256(frozen.read_bytes()).hexdigest()
    monkeypatch.setenv('ERP_SECRET_KEY', 'isolated-task-key-marker')
    monkeypatch.setenv('ERP_DATA_DIR', str(tmp_path))
    prepare_sync_database(database, '8' * 32, config)
    seen = []
    class FakePerformance:
        def __init__(self, client_id, secret):
            seen.append((client_id, secret))
        def campaigns(self): return [{'id': 'campaign-1', 'title': 'Test'}]
        def report_files(self, *_args, **_kwargs): return [('report.csv', b'fake')]
        def parse_report(self, _content):
            return [{'campaign_id': 'campaign-1', 'sku_id': 'ONE',
                     'metric_date': date(2026, 9, 23), 'name': 'One', 'price': 100,
                     'impressions': 100, 'clicks': 5, 'ad_spend': 10, 'ad_sales': 0}]
        def campaign_products(self, _campaign_id): return []
    monkeypatch.setattr(ad_optimization, 'OzonPerformanceClient', FakePerformance)
    monkeypatch.setattr(ad_optimization, 'sync_seller_catalog',
                        lambda *_args, **_kwargs: {'products': 0})
    monkeypatch.setattr(ad_optimization, 'sync_campaign_configuration',
                        lambda *_args, **_kwargs: {'campaigns': 1})
    result = execute_sync_database(database, '8' * 32, 3, '7' * 32)
    assert result['imported'] == 1 and result['analysis']['analyzed'] == 2
    assert seen == [('ad-id', 'ad-key')]
    with sqlite3.connect(database) as check:
        assert check.execute('select count(*) from ad_daily_metrics').fetchone()[0] == 1
        assert check.execute('select count(*) from ad_sku_states').fetchone()[0] == 2
        assert check.execute('select secrets_encrypted,config_json from stores').fetchone() == ('', '{}')
    result_file = tmp_path / 'sync-result.json.gz'
    changed = export_sync_delta(database, frozen, result_file, store_id='8' * 32,
                                snapshot_sha256=frozen_hash, source_sha256='1' * 64,
                                summary=result)
    assert changed['ad_skus'] >= 1 and changed['ad_daily_metrics'] == 1
    assert changed['ad_sku_states'] == 2
    payload = json.loads(gzip.decompress(result_file.read_bytes()))
    assert payload['schema'] == 3 and payload['store_id'] == '8' * 32
    assert 'ONE' in {row['sku_id'] for row in payload['changes']['ad_skus']['upserts']}
    assert 'seller-key' not in str(payload) and 'ad-key' not in str(payload)
    assert not (tmp_path / 'input.baseline.sqlite').exists()
    with sqlite3.connect(database) as check:
        check.execute("delete from ad_skus where sku_id='OLD'")
        check.commit()
    with pytest.raises(ValueError, match='意外删除'):
        export_sync_delta(database, frozen, tmp_path / 'invalid-result.json.gz',
                          store_id='8' * 32, snapshot_sha256=frozen_hash,
                          source_sha256='1' * 64, summary=result)
    with SessionLocal() as source:
        assert [row.sku_id for row in source.scalars(select(AdSku).where(
            AdSku.store_id == '8' * 32)).all()] == ['OLD']


def test_ad_sync_result_upload_requires_original_pc_lease(team, tmp_path, monkeypatch):
    from erp.core.config import get_settings
    from erp.services.ad_sync_result import TABLES
    from erp.services.ad_desktop_snapshot import analysis_download
    from erp.models import WorkerNode

    clients, worker = team
    monkeypatch.setattr(get_settings(), 'data_dir', tmp_path)
    with SessionLocal() as db:
        store = Store(platform='ozon', name='sync leased', enabled=True)
        db.add(store); db.flush()
        owner_id = db.get(WorkerNode, worker).owner_user_id
        job = Job(type='ad_optimization_sync', created_by=owner_id,
                  target_worker_id=worker, idempotency_key='sync-upload-leased',
                  payload_json=json.dumps({'store_id': store.id, 'days': 3}),
                  status='running', lease_owner=worker, attempts=1,
                  lease_expires_at=utc_now() + timedelta(minutes=5))
        db.add(job); db.commit()
        archive, snapshot_hash, source_hash = analysis_download(db, job.id, store.id, sync_input=True)
        job_id, store_id = job.id, store.id
    packed = gzip.compress(json.dumps({
        'schema': 3, 'store_id': store_id,
        'snapshot_sha256': snapshot_hash, 'source_sha256': source_hash,
        'summary': {'mode': 'live_reviewed', 'executed': False},
        'changes': {model.__tablename__: {'upserts': [], 'deletes': []} for model in TABLES},
    }).encode(), mtime=0)
    digest = hashlib.sha256(packed).hexdigest()
    path = f'/api/v1/execution-devices/local/{worker}/jobs/{job_id}'
    body = {'index': 0, 'total': 1, 'sha256': digest,
            'content': base64.b64encode(packed).decode()}
    assert clients['other'].post(path + '/ad-sync-chunk', json=body,
                                 headers={'X-Worker-Attempt': '1'}).status_code == 403
    assert clients['owner'].post(path + '/ad-sync-chunk', json=body,
                                 headers={'X-Worker-Attempt': '2'}).status_code == 409
    assert clients['owner'].post(path + '/ad-sync-chunk', json=body,
                                 headers={'X-Worker-Attempt': '1'}).status_code == 200
    assert clients['owner'].post(path + '/ad-sync-chunk', json=body,
                                 headers={'X-Worker-Attempt': '1'}).status_code == 200
    denied = clients['other'].post(path + '/ad-sync-result', json={'sha256': digest},
                                    headers={'X-Worker-Attempt': '1'})
    assert denied.status_code == 403
    result = clients['owner'].post(path + '/ad-sync-result', json={'sha256': digest},
                                    headers={'X-Worker-Attempt': '1'})
    assert result.status_code == 200, result.text
    assert result.json()['changed'] == 0
    with SessionLocal() as db:
        saved = db.get(Job, job_id)
        assert saved.status == 'completed'
        assert saved.progress == 100
        assert saved.lease_owner == ''
    assert clients['owner'].post(path + '/ad-sync-result', json={'sha256': digest},
                                 headers={'X-Worker-Attempt': '1'}).json() == result.json()
    assert clients['owner'].post(path + '/ad-sync-result', json={'sha256': '0' * 64},
                                 headers={'X-Worker-Attempt': '1'}).status_code == 409


def test_ad_sync_pc_to_cloud_end_to_end_with_fake_ozon(team, tmp_path, monkeypatch):
    from erp.core.config import get_settings
    from erp.core.security import vault
    from erp.models import WorkerNode

    clients, worker = team
    monkeypatch.setattr(get_settings(), 'data_dir', tmp_path / 'cloud')
    owner = clients['owner']
    with SessionLocal() as db:
        store = Store(platform='ozon', name='sync e2e', enabled=True,
                      external_account_id='seller-id', secrets_encrypted=vault.encrypt({
                          'client_id': 'seller-id', 'api_key': 'private',
                          'performance_client_id': 'ad-id',
                          'performance_client_secret': 'ad-secret'}))
        db.add(store); db.flush()
        sku = AdSku(store_id=store.id, sku_id='SKU', name='before')
        db.add(sku); db.flush()
        job = Job(type='ad_optimization_sync', created_by=db.get(WorkerNode, worker).owner_user_id,
                  target_worker_id=worker, idempotency_key='sync-e2e',
                  payload_json=json.dumps({'store_id': store.id, 'days': 3}),
                  status='running', lease_owner=worker, attempts=1,
                  lease_expires_at=utc_now() + timedelta(minutes=5))
        db.add(job); db.commit()
        job_id, store_id, sku_id = job.id, store.id, sku.id
    endpoint = f'/execution-devices/local/{worker}/jobs/{job_id}'
    class Response:
        def __init__(self, response):
            from io import BytesIO
            self.stream = BytesIO(response.content)
            self.headers = response.headers
        def __enter__(self): return self
        def __exit__(self, *_): self.stream.close()
        def read(self, count): return self.stream.read(count)
    class Opener:
        def open(self, request, timeout):
            response = owner.get(request.full_url.removeprefix('https://erp.example.test'),
                                 headers={'X-Worker-Attempt': '1'})
            assert response.status_code == 200, response.text
            return Response(response)
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *_: Opener())
    class Client:
        base = 'https://erp.example.test'
        profile = {'cookie': 'test-cookie'}
        def request(self, method, path, body=None, *, extra_headers=None):
            response = owner.request(method, '/api/v1' + path, json=body,
                                     headers=extra_headers)
            assert response.status_code == 200, response.text
            return response.json(), response.headers
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    import ad_sync_worker
    calls = []
    def fake_child(command, *, input, text, encoding, errors, capture_output, env, timeout):
        calls.append(command)
        assert json.loads(input)['secrets']['performance_client_secret'] == 'ad-secret'
        assert env['ERP_DATA_DIR'].endswith(job_id)
        with sqlite3.connect(command[2]) as check:
            check.execute('update ad_skus set name=? where id=?', ('after', sku_id))
            check.commit()
        class Done:
            returncode = 0
            stdout = json.dumps({'mode': 'live_reviewed', 'executed': False,
                                 'store_id': store_id, 'imported': 1})
        return Done()
    monkeypatch.setattr(subprocess, 'run', fake_child)
    result = ad_sync_worker.run(Client(), endpoint,
                                {'id': job_id, 'type': 'ad_optimization_sync',
                                 'payload': {'store_id': store_id, 'days': 3},
                                 'attempts': 1}, tmp_path / 'pc', threading.Event())
    assert len(calls) == 1 and result['changed'] == 1
    complete = owner.post('/api/v1' + endpoint + '/complete', json={'result': result},
                          headers={'X-Worker-Attempt': '1'})
    assert complete.status_code == 200, complete.text
    with SessionLocal() as db:
        assert db.get(AdSku, sku_id).name == 'after'
        assert db.get(Job, job_id).status == 'completed'
    assert not (tmp_path / 'cloud' / 'ad_sync_uploads' / job_id).exists()


def test_ad_sync_queues_only_on_selected_personal_pc(team):
    clients, worker = team
    owner = clients['owner']
    with SessionLocal() as db:
        store = Store(platform='ozon', name='sync queue', enabled=True)
        db.add(store); db.commit(); store_id = store.id
    assert owner.post('/api/v1/execution-devices/local/register', json={
        'id': worker, 'name': 'own PC', 'ad_sync_version': 1}).status_code == 200
    queued = owner.post(f'/api/v1/ad-optimization/jobs/sync?store_id={store_id}&days=1')
    assert queued.status_code == 202, queued.text
    job_id = queued.json()['job_id']
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.type == 'ad_optimization_sync'
        assert job.target_worker_id == worker
        assert job.created_by
    assert clients['other'].post(f'/api/v1/execution-devices/local/{worker}/claim').status_code == 403
    claimed = owner.post(f'/api/v1/execution-devices/local/{worker}/claim')
    assert claimed.status_code == 200 and claimed.json()['id'] == job_id


def test_ad_snapshot_network_timeout_is_retryable(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from erp_desktop import TemporaryConnectionError
    from ad_worker import download_snapshot

    class Client:
        base = 'https://erp.example.test'
        profile = {'cookie': 'test-cookie'}
    class Opener:
        def open(self, *_args, **_kwargs):
            raise urllib.error.URLError('temporary timeout')
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *_: Opener())
    endpoint = '/execution-devices/local/pc-' + 'a' * 32 + '/jobs/' + 'b' * 32
    with pytest.raises(TemporaryConnectionError, match='连接暂时中断'):
        download_snapshot(Client(), endpoint, 1, tmp_path / 'pc', threading.Event(), sync_input=True)
    assert not list((tmp_path / 'pc').glob('*.partial'))


def test_original_ad_outcome_field_writes_shared_row_with_role_guard(team):
    clients, _ = team
    with SessionLocal() as db:
        store = Store(platform='ozon', name='outcome test', enabled=True)
        db.add(store); db.flush()
        sku = AdSku(store_id=store.id, sku_id='OUTCOME', outcome_label='')
        db.add(sku); db.commit(); sku_id = sku.id
    url = f'/api/v1/ad-optimization/skus/{sku_id}/outcome'
    assert clients['viewer'].put(url, json={'outcome_label': 'SUCCESS'}).status_code == 403
    assert clients['owner'].put(url, json={'outcome_label': 'WRONG'}).status_code == 422
    changed = clients['owner'].put(url, json={'outcome_label': 'SUCCESS'})
    assert changed.status_code == 200 and changed.json()['outcome_label'] == 'SUCCESS'
    with SessionLocal() as db:
        assert db.get(AdSku, sku_id).outcome_label == 'SUCCESS'
    reset = clients['owner'].put(url, json={'outcome_label': ''})
    assert reset.status_code == 200
    with SessionLocal() as db:
        assert db.get(AdSku, sku_id).outcome_label == ''
