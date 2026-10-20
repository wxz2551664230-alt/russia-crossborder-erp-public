from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock

import pytest
import requests

from erp.core.database import SessionLocal
from erp.core.jsonutil import loads
from erp.models import Store, AdSku, AdExecutionBatch, AdAction, utc_now
from erp.services import ad_execution


@pytest.fixture
def reviewed(client, monkeypatch):
    with SessionLocal() as session:
        store = Store(platform='ozon', name='Review test'); session.add(store); session.flush()
        sku = AdSku(store_id=store.id, sku_id='SKU-1', name='Product'); session.add(sku); session.commit()
        store_id, sku_id = store.id, sku.id
    item = {'state_id': 'state-1', 'ad_sku_id': sku_id, 'sku_id': 'SKU-1', 'name': 'Product',
            'campaign_id': 'campaign-1', 'previous_target_drr': .05, 'proposed_target_drr': None,
            'break_even_drr': .25, 'reason': 'review evidence', 'evidence': ['facts'], 'metrics': {}}
    plan = {'state_ids': ['state-1'], 'items': [item], 'blocked': []}
    monkeypatch.setattr(ad_execution, 'analyze_store', lambda *_: None)
    monkeypatch.setattr(ad_execution, '_plan', lambda *_: deepcopy(plan))
    remote = Mock(); remote.delete_campaign_products.return_value = {'ok': True}; remote.campaign_products.return_value = []
    monkeypatch.setattr(ad_execution, '_client', lambda *_: remote)
    body = {'action': 'STOP_ADS_CANDIDATE', 'state_ids': ['state-1']}
    preview = client.post(f'/api/v1/ad-optimization/actions/preview?store_id={store_id}', json=body)
    assert preview.status_code == 200, preview.text
    body.update(approval_id=preview.json()['approval_id'], confirmed=True)
    return store_id, body, remote, plan


def test_approval_is_bound_and_execute_is_idempotent(client, reviewed):
    store, body, remote, _ = reviewed
    url = f'/api/v1/ad-optimization/actions/execute-batch?store_id={store}'
    assert client.post(url, json={**body, 'state_ids': ['other']}).status_code == 409
    assert client.post(url, json={**body, 'approval_id': ''}).status_code == 409
    assert remote.delete_campaign_products.call_count == 0
    assert client.post(url, json=body).json()['succeeded'] == 1
    assert client.post(url, json=body).json()['succeeded'] == 1
    assert remote.delete_campaign_products.call_count == 1


def test_approval_expiry_and_changed_values_never_write(client, reviewed):
    store, body, remote, plan = reviewed
    plan['items'][0]['previous_target_drr'] = .09
    r = client.post(f'/api/v1/ad-optimization/actions/execute-batch?store_id={store}', json=body)
    assert r.status_code == 409
    remote.delete_campaign_products.assert_not_called()
    with SessionLocal() as s:
        batch = s.get(AdExecutionBatch, body['approval_id']); batch.status = 'preview'; batch.expires_at = utc_now() - timedelta(seconds=1); s.commit()
    assert client.post(f'/api/v1/ad-optimization/actions/execute-batch?store_id={store}', json=body).status_code == 409
    remote.delete_campaign_products.assert_not_called()


def test_unknown_is_durable_blocks_new_batch_and_reconciles_without_write(client, reviewed):
    store, body, remote, _ = reviewed
    def lost(*_):
        with SessionLocal() as s:
            batch = s.get(AdExecutionBatch, body['approval_id'])
            audit = s.get(AdAction, loads(batch.results_json, [])[0]['audit_id'])
            assert audit.status == 'executing'
        raise requests.ConnectionError('response lost')
    remote.delete_campaign_products.side_effect = lost
    url = f'/api/v1/ad-optimization/actions/execute-batch?store_id={store}'
    assert client.post(url, json=body).json()['unknown'] == 1
    assert client.post(url, json=body).json()['unknown'] == 1
    assert client.post(f'/api/v1/ad-optimization/actions/preview?store_id={store}', json=body).status_code == 409
    r = client.post(f"/api/v1/ad-optimization/actions/batches/{body['approval_id']}/reconcile?store_id={store}")
    assert r.json()['succeeded'] == 1
    assert remote.delete_campaign_products.call_count == 1
    with SessionLocal() as s:
        assert s.get(AdExecutionBatch, body['approval_id']).active_store is None


def test_store_execution_lock_blocks_second_preview_execution(client, reviewed):
    store, body, remote, _ = reviewed
    another = client.post(f'/api/v1/ad-optimization/actions/preview?store_id={store}', json=body).json()
    with SessionLocal() as s:
        first = s.get(AdExecutionBatch, body['approval_id']); first.status = 'executing'; first.active_store = store; s.commit()
    r = client.post(f'/api/v1/ad-optimization/actions/execute-batch?store_id={store}', json={**body, 'approval_id': another['approval_id']})
    assert r.status_code == 409
    remote.delete_campaign_products.assert_not_called()
