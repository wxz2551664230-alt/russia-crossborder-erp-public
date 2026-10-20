from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from fastapi.testclient import TestClient

from erp.main import app
from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.core.endpoints import ai_endpoint
from erp.core.errors import AppError
from erp.core.paths import require_writable_path
from erp.integrations.ozon import OzonSellerAdapter, OzonImportUncertain
from erp.models import Job, utc_now


@pytest.mark.parametrize('payload', [{}, {'products': None}, {'products': [{}]}])
def test_ad_reconciliation_rejects_incomplete_platform_response(payload):
    from erp.integrations.ozon_performance import OzonPerformanceClient
    adapter = OzonPerformanceClient('test', 'test')
    adapter._access_token = 'test'
    adapter.session.get = Mock(return_value=Mock(ok=True, json=Mock(return_value=payload)))
    with pytest.raises(AppError, match='响应格式不完整'):
        adapter.campaign_products('123')


def test_ad_write_invalid_success_body_is_not_confirmed():
    from erp.integrations.ozon_performance import OzonPerformanceClient
    adapter = OzonPerformanceClient('test', 'test')
    adapter._access_token = 'test'
    adapter.session.post = Mock(return_value=Mock(ok=True, status_code=200, json=Mock(side_effect=ValueError())))
    with pytest.raises(AppError):
        adapter.delete_campaign_products('123', ['456'])


def test_operator_auth_and_cookie_login():
    with TestClient(app) as c:
        assert c.get('/api/v1/stores').status_code == 401
        assert c.put('/api/v1/settings/probe', json={'value': {}}).status_code == 401
        assert c.post('/api/v1/auth/login', json={'token': '错误密钥'}).status_code == 401
        r = c.post('/api/v1/auth/login', json={'token': 'test-operator-token'})
        assert r.status_code == 200
        assert 'httponly' in r.headers['set-cookie'].lower()
        assert c.get('/api/v1/stores').status_code == 200
        assert c.post('/api/v1/auth/logout').status_code == 200
        assert c.get('/api/v1/stores').status_code == 401


@pytest.mark.parametrize('origin', ['http://localhost.evil.invalid', 'http://testserver.evil.invalid', 'https://testserver', 'null'])
def test_origin_must_match_exactly(client, origin):
    assert client.put('/api/v1/settings/probe', headers={'Origin': origin}, json={'value': {}}).status_code == 403


def test_worker_key_cannot_operate_business_api():
    with TestClient(app, headers={'Authorization': 'Bearer test-worker-token'}) as c:
        assert c.get('/api/v1/stores').status_code == 401


def test_asset_paths_restrict_registration_and_download(client, tmp_path):
    outside = tmp_path / 'outside.png'; outside.write_bytes(b'not-sensitive')
    assert client.post('/api/v1/assets', json={'path': str(outside)}).status_code == 403
    inside = get_settings().data_dir / 'allowed.png'; inside.write_bytes(b'not-sensitive')
    r = client.post('/api/v1/assets', json={'path': str(inside)})
    assert r.status_code == 201
    assert client.get(f"/api/v1/assets/{r.json()['id']}/content").status_code == 200
    secret = get_settings().data_dir / 'fake.key'; secret.write_text('not-sensitive')
    assert client.post('/api/v1/assets', json={'path': str(secret)}).status_code == 403
    from erp.models import Asset
    with SessionLocal() as s:
        asset = s.get(Asset, r.json()['id']); asset.local_path = str(outside); s.commit()
    assert client.get(f"/api/v1/assets/{r.json()['id']}/content").status_code == 403


def test_old_program_never_started_or_mutated(client, monkeypatch):
    started = Mock(); monkeypatch.setattr('erp.domains.pod.runtime.subprocess.Popen', started)
    assert client.delete('/api/v1/pod/legacy-jobs/finished').status_code == 403
    assert client.post('/api/v1/pod/legacy-jobs/example/retry').status_code == 403
    assert client.post('/api/v1/pod/legacy-api/unported-write', json={}).status_code == 409
    started.assert_not_called()
    with pytest.raises(AppError):
        require_writable_path(get_settings().legacy_composer_root / 'new-output.png')


def test_invalid_pod_form_is_validation_error(client):
    r = client.post('/api/v1/pod/legacy-api/jobs/multi', data={'placements': '[invalid'})
    assert r.status_code == 422, r.text


@pytest.mark.parametrize('error', [requests.ConnectionError('lost response'), requests.ReadTimeout('timeout')])
def test_product_create_network_failure_is_unknown(error):
    transport = Mock(); transport.post.side_effect = error
    adapter = OzonSellerAdapter({'external_account_id': 'fake', 'secrets': {'api_key': 'fake'}}, session=transport, repository=object())
    with pytest.raises(OzonImportUncertain):
        adapter.post('/v3/product/import', {}, product_create=True)
    assert transport.post.call_count == 1


def test_seller_analytics_waits_and_retries_rate_limit(monkeypatch):
    sleeps = []
    monkeypatch.setattr('erp.integrations.ozon.time.sleep', sleeps.append)
    limited = Mock(status_code=429, headers={}, text='rate limit')
    success = Mock(status_code=200, headers={})
    success.json.return_value = {'result': {'data': []}}
    transport = Mock()
    transport.post.side_effect = [limited, success, success]
    adapter = OzonSellerAdapter({'external_account_id': 'fake', 'secrets': {'api_key': 'fake'}}, session=transport, repository=object())
    adapter.analytics_by_sku('2026-09-01', '2026-09-07')
    adapter.analytics_by_sku('2026-09-08', '2026-09-14')
    assert transport.post.call_count == 3
    assert any(delay >= 5 for delay in sleeps)
    assert any(0 < delay <= 2 for delay in sleeps)


@pytest.mark.parametrize('url', ['https://api.example.com', 'https://api.example.com/v1', 'https://api.example.com/v1/chat/completions'])
def test_ai_endpoint_normalization(url):
    assert ai_endpoint(url) == 'https://api.example.com/v1/chat/completions'


def test_remote_worker_rejects_expired_and_previous_attempt(client):
    h = {'Authorization': 'Bearer test-worker-token', 'X-Worker-Attempt': '1'}
    client.post('/api/v1/workers/register', headers=h, json={'id': 'audit'})
    j = client.post('/api/v1/jobs', json={'type': 'worker_probe', 'idempotency_key': 'expiry'}).json()
    client.post('/api/v1/workers/audit/claim', headers=h, json={'capabilities': ['worker_probe']})
    with SessionLocal() as s:
        job = s.get(Job, j['id']); job.lease_expires_at = utc_now() - timedelta(seconds=2); s.commit()
    path = f"/api/v1/workers/audit/jobs/{j['id']}/complete"
    assert client.post(path, headers=h, json={'result': {}}).status_code == 409
    claimed = client.post('/api/v1/workers/audit/claim', headers=h, json={'capabilities': ['worker_probe']}).json()
    assert claimed is None  # Recovery observes the shared retry backoff.
    with SessionLocal() as s:
        job = s.get(Job, j['id'])
        job.heartbeat_at = utc_now() - timedelta(seconds=3)
        s.commit()
    claimed = client.post('/api/v1/workers/audit/claim', headers=h, json={'capabilities': ['worker_probe']}).json()
    assert claimed['attempts'] == 2
    assert client.post(path, headers=h, json={'result': {}}).status_code == 409
    assert client.post(path, headers={**h, 'X-Worker-Attempt': '2'}, json={'result': {}}).status_code == 200


def test_worker_cancel_does_not_become_completed(client):
    h = {'Authorization': 'Bearer test-worker-token', 'X-Worker-Attempt': '1'}
    client.post('/api/v1/workers/register', headers=h, json={'id': 'audit'})
    j = client.post('/api/v1/jobs', json={'type': 'worker_probe', 'idempotency_key': 'cancel'}).json()
    client.post('/api/v1/workers/audit/claim', headers=h, json={'capabilities': ['worker_probe']})
    client.post(f"/api/v1/jobs/{j['id']}/cancel")
    assert client.post(f"/api/v1/workers/audit/jobs/{j['id']}/complete", headers=h, json={'result': {}}).status_code == 409
    assert client.get(f"/api/v1/jobs/{j['id']}").json()['status'] == 'cancelled'
