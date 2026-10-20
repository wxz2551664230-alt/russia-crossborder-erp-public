from datetime import date
from pathlib import Path
from datetime import timedelta
import base64
import gzip
import json
import pytest

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from erp.core.database import SessionLocal
from erp.main import app as raw_app
from erp.models import AdDailyMetric, AdSku, AdSkuState, AdExecutionBatch, Job, Store, WorkerNode, utc_now
from erp.services.ad_desktop_snapshot import create_analysis_snapshot
from test_personal_job_controls import team


def test_review_plan_uses_original_algorithm_on_isolated_pc_snapshot(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from ad_worker import analyze_snapshot
    from ad_preview_worker import preview_snapshot

    with SessionLocal() as source:
        store = Store(platform='ozon', name='Preview source', enabled=True)
        other = Store(platform='ozon', name='Unrelated', enabled=True)
        source.add_all([store, other])
        source.flush()
        sku = AdSku(store_id=store.id, sku_id='ONE', name='One')
        unrelated = AdSku(store_id=other.id, sku_id='TWO', name='Two')
        source.add_all([sku, unrelated])
        source.flush()
        source.add(AdDailyMetric(ad_sku_id=sku.id, metric_date=date(2026, 9, 23),
                                 impressions=10, clicks=1))
        source.commit()
        store_id = store.id
        snapshot = tmp_path / 'ad-preview.sqlite'
        create_analysis_snapshot(source, store_id, snapshot)
    analyze_snapshot(snapshot, store_id)
    engine = create_engine('sqlite:///' + snapshot.as_posix())
    try:
        with Session(engine) as db:
            state_id = db.scalar(select(AdSkuState.id).join(AdSku).where(AdSku.store_id == store_id))
            assert state_id
    finally:
        engine.dispose()
    with pytest.raises(ValueError, match='安全门'):
        preview_snapshot(snapshot, store_id, 'STOP_ADS_CANDIDATE', [state_id])


def test_preview_result_requires_owned_lease_and_creates_one_review_batch(team, tmp_path, monkeypatch):
    from erp.core.config import get_settings
    from erp.services import ad_execution
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from ad_worker import analyze_snapshot, export_analysis_result

    clients, worker = team
    monkeypatch.setattr(get_settings(), 'data_dir', tmp_path)
    owner = clients['owner']
    registration = owner.post('/api/v1/execution-devices/local/register',
                              json={'id': worker, 'name': 'PC', 'ad_preview_version': 1})
    assert registration.status_code == 200, registration.text
    with SessionLocal() as db:
        store = Store(platform='ozon', name='Preview store', enabled=True)
        db.add(store)
        db.flush()
        db.add(AdSku(store_id=store.id, sku_id='PREVIEW-SKU', name='Preview'))
        db.commit()
        store_id = store.id
        device = db.get(WorkerNode, worker)
        state_id = 'a' * 32
        job = Job(type='ad_optimization_preview', created_by=device.owner_user_id,
                  target_worker_id=worker, idempotency_key='ad-preview-personal',
                  payload_json=json.dumps({'store_id': store_id, 'action': 'STOP_ADS_CANDIDATE',
                                           'state_ids': [state_id]}),
                  status='running', lease_owner=worker, attempts=1,
                  lease_expires_at=utc_now() + timedelta(minutes=5))
        db.add(job)
        db.commit()
        job_id = job.id
    item = {'state_id': state_id, 'ad_sku_id': 'b' * 32, 'sku_id': 'PREVIEW-SKU',
            'campaign_id': 'campaign-1', 'previous_target_drr': 0.1,
            'proposed_target_drr': None, 'break_even_drr': 0.25,
            'reason': 'test', 'evidence': ['facts'], 'metrics': {}}
    plan = {'state_ids': [state_id], 'items': [item], 'blocked': []}
    monkeypatch.setattr(ad_execution, '_plan', lambda *args: plan)
    url = f'/api/v1/execution-devices/local/{worker}/jobs/{job_id}'
    headers = {'X-Worker-Attempt': '1'}
    snapshot = owner.get(url + '/ad-analysis-snapshot', headers=headers)
    assert snapshot.status_code == 200, snapshot.text
    database = tmp_path / 'pc-preview.sqlite'
    database.write_bytes(gzip.decompress(snapshot.content))
    analysis = analyze_snapshot(database, store_id)
    result_file = tmp_path / 'pc-preview.gz'
    export_analysis_result(database, analysis, snapshot.headers['x-content-sha256'],
                           snapshot.headers['x-source-sha256'], result_file, plan=plan)
    content = base64.b64encode(result_file.read_bytes()).decode()
    for name in ('other', 'viewer'):
        assert clients[name].post(url + '/ad-analysis-result', json={'content': content},
                                  headers=headers).status_code == 403
    tampered = json.loads(gzip.decompress(result_file.read_bytes()))
    tampered['preview_plan']['items'][0]['proposed_target_drr'] = 0.4
    forged = base64.b64encode(gzip.compress(json.dumps(tampered).encode())).decode()
    assert owner.post(url + '/ad-analysis-result', json={'content': forged},
                      headers=headers).status_code == 409
    receipt = owner.post(url + '/ad-analysis-result', json={'content': content}, headers=headers)
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()['approval_id']
    assert owner.post(url + '/ad-analysis-result', json={'content': content},
                      headers=headers).json() == receipt.json()
    with SessionLocal() as db:
        assert db.get(AdExecutionBatch, receipt.json()['approval_id']).status == 'preview'
        assert db.scalar(select(func.count()).select_from(AdExecutionBatch)) == 1
    completed = owner.post(url + '/complete', json={'result': receipt.json()}, headers=headers)
    assert completed.status_code == 200, completed.text
    with TestClient(raw_app) as direct:
        direct.cookies.update(owner.cookies)
        viewed = direct.get(f'/api/v1/ad-optimization/actions/previews/{job_id}')
        assert viewed.status_code == 200, viewed.text
        assert viewed.json()['items'] == [item]
    with TestClient(raw_app) as direct_other:
        direct_other.cookies.update(clients['other'].cookies)
        assert direct_other.get(f'/api/v1/ad-optimization/actions/previews/{job_id}').status_code == 404


def test_cloud_preview_enqueues_owned_pc_without_running_server_analysis(team, monkeypatch):
    from erp.services import ad_execution
    clients, worker = team
    owner = clients['owner']
    assert owner.post('/api/v1/execution-devices/local/register',
                      json={'id': worker, 'name': 'PC', 'ad_preview_version': 1}).status_code == 200
    with SessionLocal() as db:
        store = Store(platform='ozon', name='Queued preview', enabled=True)
        db.add(store)
        db.commit()
        store_id = store.id
    monkeypatch.setattr(ad_execution, 'analyze_store',
                        lambda *_: (_ for _ in ()).throw(AssertionError('server analysis ran')))
    with TestClient(raw_app) as direct:
        direct.cookies.update(owner.cookies)
        requested = direct.post('/api/v1/ad-optimization/actions/preview?store_id=' + store_id,
                                json={'action': 'STOP_ADS_CANDIDATE', 'state_ids': ['a' * 32]})
    assert requested.status_code == 200, requested.text
    with SessionLocal() as db:
        job = db.get(Job, requested.json()['job_id'])
        assert job.type == 'ad_optimization_preview'
        assert job.target_worker_id == worker
        assert job.status == 'queued'
