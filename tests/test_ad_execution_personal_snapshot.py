import sqlite3
import gzip
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from unittest.mock import Mock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from erp.core.database import SessionLocal
from erp.core.errors import ConflictError
from erp.core.config import get_settings
from erp.core.security import vault
from erp.models import AdAction, AdExecutionBatch, AdSku, Job, Store, WorkerNode, utc_now
from erp.services.ad_desktop_snapshot import (create_execution_snapshot, create_reconcile_snapshot,
                                               reconcile_source_fingerprint, analysis_download)
from erp.services.ad_desktop_snapshot import analysis_source_fingerprint
from erp.schemas import AdBatchExecutionRequest
from test_personal_job_controls import team
from fastapi.testclient import TestClient
from erp.main import app as raw_app


def test_execution_snapshot_contains_only_locked_batch_and_no_credentials(tmp_path):
    with SessionLocal() as db:
        own = Store(platform='ozon', name='Own', enabled=True,
                    secrets_encrypted='private-performance-key')
        other = Store(platform='ozon', name='Other', enabled=True,
                      secrets_encrypted='other-private-key')
        db.add_all([own, other])
        db.flush()
        db.add_all([AdSku(store_id=own.id, sku_id='OWN-SKU'),
                    AdSku(store_id=other.id, sku_id='OTHER-SKU')])
        batch = AdExecutionBatch(store_id=own.id, action='STOP_ADS_CANDIDATE',
                                 actor='operator', status='preview',
                                 preview_json='{"state_ids":[],"items":[],"blocked":[]}',
                                 expires_at=utc_now() + timedelta(minutes=10))
        db.add(batch)
        db.commit()
        path = tmp_path / 'execution.sqlite'
        with pytest.raises(ConflictError):
            create_execution_snapshot(db, batch, path)
        batch.status = 'executing'
        batch.active_store = own.id
        db.commit()
        counts = create_execution_snapshot(db, batch, path)
    assert counts['ad_execution_batches'] == 1
    with sqlite3.connect(path) as check:
        assert check.execute('select count(*) from stores').fetchone()[0] == 1
        assert check.execute('select secrets_encrypted from stores').fetchone()[0] == ''
        assert check.execute('select sku_id from ad_skus').fetchone()[0] == 'OWN-SKU'
        assert check.execute('select count(*),status,active_store from ad_execution_batches').fetchone() == (1, 'preview', None)
        assert check.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_original_reviewed_write_runs_once_inside_pc_snapshot(tmp_path, monkeypatch):
    from erp.services import ad_execution
    with SessionLocal() as db:
        store = Store(platform='ozon', name='Local execution', enabled=True)
        db.add(store)
        db.flush()
        sku = AdSku(store_id=store.id, sku_id='SKU-1', name='One')
        db.add(sku)
        db.flush()
        item = {'state_id': 'a' * 32, 'ad_sku_id': sku.id, 'sku_id': 'SKU-1',
                'name': 'One', 'campaign_id': 'campaign-1',
                'previous_target_drr': 0.05, 'proposed_target_drr': None,
                'break_even_drr': 0.25, 'reason': 'evidence',
                'evidence': ['facts'], 'metrics': {}}
        plan = {'state_ids': ['a' * 32], 'items': [item], 'blocked': []}
        import json
        batch = AdExecutionBatch(store_id=store.id, action='STOP_ADS_CANDIDATE',
                                 actor='operator', status='executing', active_store=store.id,
                                 preview_json=json.dumps(plan),
                                 expires_at=utc_now() + timedelta(minutes=10))
        db.add(batch)
        db.commit()
        store_id, batch_id = store.id, batch.id
        path = tmp_path / 'pc-execution.sqlite'
        create_execution_snapshot(db, batch, path)
    remote = Mock()
    remote.delete_campaign_products.return_value = {'ok': True}
    monkeypatch.setattr(ad_execution, '_client', lambda *_: remote)
    monkeypatch.setattr(ad_execution, 'analyze_store', lambda *_: None)
    monkeypatch.setattr(ad_execution, '_plan', lambda *_: plan)
    request = AdBatchExecutionRequest(action='STOP_ADS_CANDIDATE', state_ids=['a' * 32],
                                      approval_id=batch_id, confirmed=True)
    engine = create_engine('sqlite:///' + path.as_posix())
    try:
        with Session(engine) as pc:
            assert ad_execution.execute(pc, store_id, request, 'operator')['succeeded'] == 1
            assert ad_execution.execute(pc, store_id, request, 'operator')['succeeded'] == 1
            assert remote.delete_campaign_products.call_count == 1
    finally:
        engine.dispose()
    with SessionLocal() as cloud:
        assert cloud.get(AdExecutionBatch, batch_id).status == 'executing'


def test_execution_snapshot_and_credentials_require_owned_live_lease(team, tmp_path, monkeypatch):
    clients, worker = team
    monkeypatch.setattr(get_settings(), 'data_dir', tmp_path)
    owner = clients['owner']
    assert owner.post('/api/v1/execution-devices/local/register',
                      json={'id': worker, 'name': 'PC', 'ad_execute_version': 1}).status_code == 200
    with SessionLocal() as db:
        store = Store(platform='ozon', name='Execution store', enabled=True,
                      secrets_encrypted=vault.encrypt({'performance_client_id': 'task-id',
                                                       'performance_client_secret': 'task-secret'}))
        db.add(store)
        db.flush()
        db.add(AdSku(store_id=store.id, sku_id='ONE'))
        device = db.get(WorkerNode, worker)
        batch = AdExecutionBatch(store_id=store.id, action='STOP_ADS_CANDIDATE',
                                 actor=device.owner_user_id, status='executing', active_store=store.id,
                                 preview_json='{"state_ids":[],"items":[],"blocked":[]}',
                                 expires_at=utc_now() + timedelta(minutes=10))
        db.add(batch)
        db.flush()
        import json
        job = Job(type='ad_optimization_execute', created_by=device.owner_user_id,
                  target_worker_id=worker, idempotency_key='private-execution',
                  payload_json=json.dumps({'store_id': store.id, 'approval_id': batch.id}),
                  status='running', lease_owner=worker, attempts=1,
                  lease_expires_at=utc_now() + timedelta(minutes=5))
        db.add(job)
        db.commit()
        job_id = job.id
    base = f'/api/v1/execution-devices/local/{worker}/jobs/{job_id}'
    headers = {'X-Worker-Attempt': '1'}
    for suffix in ('ad-execution-snapshot', 'ad-execution-credentials'):
        assert clients['other'].get(base + '/' + suffix, headers=headers).status_code == 403
        assert owner.get(base + '/' + suffix,
                         headers={'X-Worker-Attempt': '2'}).status_code == 409
    snapshot = owner.get(base + '/ad-execution-snapshot', headers=headers)
    assert snapshot.status_code == 200, snapshot.text
    packed = tmp_path / 'downloaded.sqlite'
    packed.write_bytes(gzip.decompress(snapshot.content))
    with sqlite3.connect(packed) as check:
        assert check.execute('select secrets_encrypted from stores').fetchone()[0] == ''
        assert check.execute('select status,active_store from ad_execution_batches').fetchone() == ('preview', None)
    credentials = owner.get(base + '/ad-execution-credentials', headers=headers)
    assert credentials.status_code == 200
    assert credentials.json() == {'store_id': store.id,
                                  'performance_client_id': 'task-id',
                                  'performance_client_secret': 'task-secret'}
    assert credentials.headers['cache-control'] == 'no-store'
    assert clients['other'].post(base + '/ad-execution-result',
                                 json={'content': 'bad'}, headers=headers).status_code == 403
    assert owner.post(base + '/ad-execution-result', json={'content': 'bad'},
                      headers={'X-Worker-Attempt': '2'}).status_code == 409


def test_expired_approval_on_pc_becomes_stale_before_any_platform_call(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from ad_execution_worker import execute_snapshot
    from erp.integrations import ozon_performance
    from unittest.mock import Mock

    with SessionLocal() as db:
        store = Store(platform='ozon', name='Expired', enabled=True)
        db.add(store)
        db.flush()
        batch = AdExecutionBatch(store_id=store.id, action='STOP_ADS_CANDIDATE',
                                 actor='a' * 32, status='executing', active_store=store.id,
                                 preview_json='{"state_ids":["' + 'b' * 32 + '"],"items":[],"blocked":[]}',
                                 expires_at=utc_now() - timedelta(minutes=1))
        db.add(batch)
        db.commit()
        path = tmp_path / 'expired.sqlite'
        create_execution_snapshot(db, batch, path)
        payload = {'store_id': store.id, 'approval_id': batch.id,
                   'action': batch.action, 'state_ids': ['b' * 32]}
    remote = Mock()
    monkeypatch.setattr(ozon_performance, 'OzonPerformanceClient', lambda *_: remote)
    result = execute_snapshot(path, payload, 'a' * 32, 'id', 'secret')
    assert result['status'] == 'stale'
    remote.delete_campaign_products.assert_not_called()
    remote.update_campaign_products.assert_not_called()


def test_platform_target_changed_after_preview_stops_before_ad_write(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from ad_execution_worker import execute_snapshot
    from erp.integrations import ozon_performance
    with SessionLocal() as db:
        store = Store(platform='ozon', name='Changed live target', enabled=True)
        db.add(store)
        db.flush()
        sku = AdSku(store_id=store.id, sku_id='5020588246')
        db.add(sku)
        db.flush()
        item = {'state_id': 'b' * 32, 'ad_sku_id': sku.id, 'sku_id': sku.sku_id,
                'campaign_id': '32788230', 'previous_target_drr': 0.05,
                'proposed_target_drr': 0.06, 'break_even_drr': 0.25,
                'reason': 'facts', 'evidence': ['facts'], 'metrics': {}}
        batch = AdExecutionBatch(store_id=store.id, action='SCALE_CANDIDATES',
                                 actor='a' * 32, status='executing', active_store=store.id,
                                 preview_json=json.dumps({'state_ids': [item['state_id']],
                                                          'items': [item], 'blocked': []}),
                                 expires_at=utc_now() + timedelta(minutes=5))
        db.add(batch)
        db.commit()
        path = tmp_path / 'changed-target.sqlite'
        create_execution_snapshot(db, batch, path)
        payload = {'store_id': store.id, 'approval_id': batch.id,
                   'action': batch.action, 'state_ids': [item['state_id']]}
    remote = Mock()
    remote.campaign_products.return_value = [{'sku': '5020588246', 'targetCir': 9}]
    monkeypatch.setattr(ozon_performance, 'OzonPerformanceClient', lambda *_: remote)
    result = execute_snapshot(path, payload, 'a' * 32, 'id', 'secret')
    assert result['status'] == 'stale'
    remote.campaign_products.assert_called_once_with('32788230')
    remote.update_campaign_products.assert_not_called()
    remote.delete_campaign_products.assert_not_called()


def test_confirmed_batch_is_locked_and_queued_for_same_pc_without_server_write(team, monkeypatch):
    from erp.services import ad_execution
    clients, worker = team
    owner = clients['owner']
    assert owner.post('/api/v1/execution-devices/local/register',
                      json={'id': worker, 'name': 'PC', 'ad_execute_version': 1}).status_code == 200
    with SessionLocal() as db:
        store = Store(platform='ozon', name='Queue execution', enabled=True,
                      secrets_encrypted=vault.encrypt({'performance_client_id': 'task-id',
                                                       'performance_client_secret': 'task-secret'}))
        db.add(store)
        db.flush()
        db.add(AdSku(store_id=store.id, sku_id='ONE'))
        db.flush()
        device = db.get(WorkerNode, worker)
        import json
        plan = {'state_ids': ['a' * 32], 'items': [{'state_id': 'a' * 32}], 'blocked': [],
                'source_fingerprint': analysis_source_fingerprint(db, store.id)}
        batch = AdExecutionBatch(store_id=store.id, action='STOP_ADS_CANDIDATE',
                                 actor=device.owner_user_id, status='preview',
                                 preview_json=json.dumps(plan),
                                 expires_at=utc_now() + timedelta(minutes=10))
        db.add(batch)
        db.commit()
        store_id, batch_id = store.id, batch.id
    monkeypatch.setattr(ad_execution, '_client',
                        lambda *_: (_ for _ in ()).throw(AssertionError('server called Ozon')))
    body = {'action': 'STOP_ADS_CANDIDATE', 'confirmed': True,
            'approval_id': batch_id, 'state_ids': ['a' * 32]}
    with TestClient(raw_app) as direct:
        direct.cookies.update(owner.cookies)
        queued = direct.post('/api/v1/ad-optimization/actions/execute-batch?store_id=' + store_id,
                             json=body)
        assert queued.status_code == 200, queued.text
        repeated = direct.post('/api/v1/ad-optimization/actions/execute-batch?store_id=' + store_id,
                               json=body)
        assert repeated.json() == queued.json()
    with SessionLocal() as db:
        job = db.get(Job, queued.json()['job_id'])
        batch = db.get(AdExecutionBatch, batch_id)
        assert job.type == 'ad_optimization_execute'
        assert job.target_worker_id == worker
        assert job.status == 'queued'
        assert batch.status == 'executing' and batch.active_store == store_id


def test_unknown_batch_is_checked_read_only_on_pc_snapshot(tmp_path, monkeypatch):
    from erp.services import ad_execution
    from unittest.mock import Mock
    import json
    with SessionLocal() as db:
        store = Store(platform='ozon', name='Unknown', enabled=True)
        db.add(store)
        db.flush()
        sku = AdSku(store_id=store.id, sku_id='SKU-1')
        db.add(sku)
        db.flush()
        item = {'state_id': 'a' * 32, 'ad_sku_id': sku.id, 'sku_id': 'SKU-1',
                'campaign_id': 'campaign-1', 'previous_target_drr': 0.05,
                'proposed_target_drr': None, 'break_even_drr': 0.25,
                'reason': 'facts', 'evidence': ['facts'], 'metrics': {}}
        plan = {'state_ids': ['a' * 32], 'items': [item], 'blocked': []}
        batch = AdExecutionBatch(store_id=store.id, action='STOP_ADS_CANDIDATE',
                                 actor='operator', status='unknown', active_store=store.id,
                                 preview_json=json.dumps(plan),
                                 expires_at=utc_now() + timedelta(minutes=5))
        db.add(batch)
        db.flush()
        audit = AdAction(ad_sku_id=sku.id, action='STOP_ADS_CANDIDATE',
                         status='unknown', before_metrics_json=json.dumps({'approval_id': batch.id,
                                                                            'proposal': item}),
                         decided_at=utc_now())
        db.add(audit)
        db.flush()
        batch.results_json = json.dumps([{'sku_id': 'SKU-1', 'campaign_id': 'campaign-1',
                                         'status': 'unknown', 'audit_id': audit.id}])
        db.commit()
        path = tmp_path / 'reconcile.sqlite'
        counts = create_reconcile_snapshot(db, batch, path)
        source_before = reconcile_source_fingerprint(db, batch)
        archive, packed_digest, source_digest = analysis_download(
            db, uuid4().hex, store.id, reconcile_batch=batch)
        assert archive.exists() and len(packed_digest) == 64
        assert source_digest == source_before
        audit.reason = 'new evidence after snapshot'
        db.flush()
        assert reconcile_source_fingerprint(db, batch) != source_before
        db.rollback()
        batch_id, store_id = batch.id, store.id
    assert counts['ad_actions'] == 1
    remote = Mock()
    remote.campaign_products.return_value = []
    monkeypatch.setattr(ad_execution, '_client', lambda *_: remote)
    monkeypatch.setattr(ad_execution, 'analyze_store', lambda *_: None)
    engine = create_engine('sqlite:///' + path.as_posix())
    try:
        with Session(engine) as pc:
            result = ad_execution.reconcile(pc, batch_id, store_id)
            assert result['status'] == 'completed' and result['succeeded'] == 1
    finally:
        engine.dispose()
    assert remote.campaign_products.call_count == 1
    remote.delete_campaign_products.assert_not_called()
    remote.update_campaign_products.assert_not_called()
    with SessionLocal() as cloud:
        assert cloud.get(AdExecutionBatch, batch_id).status == 'unknown'


def test_reconcile_queue_stays_on_original_pc_and_requires_live_lease(team, monkeypatch):
    from erp.services import ad_execution
    clients, worker = team
    owner = clients['owner']
    assert owner.post('/api/v1/execution-devices/local/register',
                      json={'id': worker, 'name': 'original PC',
                            'ad_execute_version': 1, 'ad_reconcile_version': 1}).status_code == 200
    with SessionLocal() as db:
        device = db.get(WorkerNode, worker)
        store = Store(platform='ozon', name='Reconcile scope', enabled=True,
                      secrets_encrypted=vault.encrypt({'performance_client_id': 'id',
                                                       'performance_client_secret': 'secret'}))
        db.add(store)
        db.flush()
        sku = AdSku(store_id=store.id, sku_id='SKU-1')
        db.add(sku)
        db.flush()
        item = {'state_id': 'a' * 32, 'ad_sku_id': sku.id, 'sku_id': 'SKU-1',
                'campaign_id': 'campaign-1', 'previous_target_drr': 0.05,
                'proposed_target_drr': None, 'break_even_drr': 0.25,
                'reason': 'review', 'evidence': ['facts'], 'metrics': {}}
        batch = AdExecutionBatch(store_id=store.id, action='STOP_ADS_CANDIDATE',
                                 actor=device.owner_user_id, status='unknown', active_store=store.id,
                                 preview_json=json.dumps({'state_ids': [item['state_id']],
                                                          'items': [item], 'blocked': []}),
                                 expires_at=utc_now() + timedelta(minutes=5))
        db.add(batch)
        db.flush()
        audit = AdAction(ad_sku_id=sku.id, action=batch.action, status='unknown',
                         before_metrics_json=json.dumps({'approval_id': batch.id, 'proposal': item}),
                         decided_at=utc_now())
        db.add(audit)
        db.flush()
        batch.results_json = json.dumps([{'sku_id': 'SKU-1', 'campaign_id': 'campaign-1',
                                          'status': 'unknown', 'audit_id': audit.id}])
        db.add(Job(type='ad_optimization_execute', created_by=device.owner_user_id,
                   target_worker_id=worker, status='completed',
                   idempotency_key='original-reconcile-execution-' + batch.id,
                   payload_json=json.dumps({'store_id': store.id, 'approval_id': batch.id})))
        db.commit()
        store_id, batch_id = store.id, batch.id
    monkeypatch.setattr(ad_execution, '_client',
                        lambda *_: (_ for _ in ()).throw(AssertionError('server queried Ozon')))
    path = f'/api/v1/ad-optimization/actions/batches/{batch_id}/reconcile?store_id={store_id}'
    with TestClient(raw_app) as direct:
        direct.cookies.update(clients['other'].cookies)
        assert direct.post(path).status_code in {403, 409}
        direct.cookies.update(owner.cookies)
        queued = direct.post(path)
        assert queued.status_code == 200, queued.text
        assert direct.post(path).json() == queued.json()
        job_id = queued.json()['job_id']
        snapshot = f'/api/v1/execution-devices/local/{worker}/jobs/{job_id}/ad-reconcile-snapshot'
        assert direct.get(snapshot, headers={'X-Worker-Attempt': '1'}).status_code == 409
        claimed = direct.post(f'/api/v1/execution-devices/local/{worker}/claim')
        assert claimed.status_code == 200 and claimed.json()['id'] == job_id
        assert direct.get(snapshot, headers={'X-Worker-Attempt': '2'}).status_code == 409
        assert direct.get(snapshot, headers={'X-Worker-Attempt': '1'}).status_code == 200
        assert direct.get(snapshot, headers={'X-Worker-Attempt': '1'}).headers['X-Source-SHA256']
        with SessionLocal() as db:
            db.get(Job, job_id).status = 'completed'
            db.commit()
        repeated_read = direct.post(path)
        assert repeated_read.status_code == 200
        assert repeated_read.json()['job_id'] != job_id
        direct.cookies.update(clients['other'].cookies)
        assert direct.get(snapshot, headers={'X-Worker-Attempt': '1'}).status_code == 403
        assert direct.get(snapshot.replace('ad-reconcile-snapshot', 'ad-execution-credentials'),
                          headers={'X-Worker-Attempt': '1'}).status_code == 403
    with SessionLocal() as db:
        assert db.get(Job, job_id).target_worker_id == worker
