"""Two personal computers can run the same kind without sharing a queue/lease."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from deploy.pilot_gateway import app
from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.models import Job, utc_now
from erp.services.accounts import create_user


@pytest.fixture
def computers(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, 'multiuser_enabled', True)
    monkeypatch.setattr(settings, 'runtime_mode', 'server')
    monkeypatch.setattr(settings, 'session_cookie_secure', False)
    with SessionLocal() as db:
        create_user(db, username='same-user', display_name='Same user',
                    password='parallel-test-password', role='operator')
    with ExitStack() as stack:
        devices = []
        for digit in ('a', 'b'):
            client = stack.enter_context(TestClient(app))
            assert client.post('/api/v1/auth/login', json={
                'username': 'same-user', 'password': 'parallel-test-password'}).status_code == 200
            worker = 'pc-' + digit * 32
            assert client.post('/api/v1/execution-devices/local/register', json={
                'id': worker, 'name': 'PC ' + digit, 'composition_version': 1}).status_code == 200
            devices.append((client, worker))
        yield devices


def submit(client):
    response = client.post('/api/v1/execution-devices/probe')
    assert response.status_code == 202, response.text
    return response.json()['job_id']


def test_same_account_same_type_runs_on_two_computers_at_once(computers):
    (a, wa), (b, wb) = computers
    ja, jb = submit(a), submit(b)
    def claim(device):
        client, worker = device
        return client.post(f'/api/v1/execution-devices/local/{worker}/claim').json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        ca, cb = list(pool.map(claim, computers))
    assert (ca['id'], cb['id']) == (ja, jb)
    assert ca['status'] == cb['status'] == 'running'
    assert (ca['target_worker_id'], cb['target_worker_id']) == (wa, wb)
    # Starting PC B does not change the selection in PC A's session.
    assert a.get('/api/v1/execution-devices').json()['current_worker_id'] == wa
    headers = {'X-Worker-Attempt': '1'}
    assert b.post(f'/api/v1/execution-devices/local/{wb}/jobs/{ja}/heartbeat',
                  headers=headers).status_code == 403
    assert b.post(f'/api/v1/execution-devices/local/{wb}/jobs/{jb}/complete',
                  headers=headers, json={'result': {'ok': True}}).json()['status'] == 'completed'
    assert a.get(f'/api/v1/jobs/{ja}').json()['status'] == 'running'
    assert a.post(f'/api/v1/execution-devices/local/{wa}/jobs/{ja}/heartbeat',
                  headers={'X-Worker-Attempt': '2'}).status_code == 409
    assert a.post(f'/api/v1/execution-devices/local/{wa}/jobs/{ja}/heartbeat',
                  headers=headers).status_code == 200


def test_offline_peer_does_not_block_claim_or_expire_other_computers_task(computers):
    (a, wa), (b, wb) = computers
    ja = submit(a)
    assert a.post(f'/api/v1/execution-devices/local/{wa}/claim').json()['id'] == ja
    with SessionLocal() as db:
        row = db.get(Job, ja)
        row.lease_expires_at = utc_now() - timedelta(minutes=5)
        db.commit()
    jb = submit(b)
    assert b.post(f'/api/v1/execution-devices/local/{wb}/claim').json()['id'] == jb
    with SessionLocal() as db:
        row = db.get(Job, ja)
        assert row.status == 'running' and row.lease_owner == wa and row.attempts == 1
    assert b.post(f'/api/v1/execution-devices/local/{wb}/claim').json() is None


def test_progress_from_one_computer_cannot_overwrite_the_other(computers):
    (a, wa), (b, wb) = computers
    ja, jb = submit(a), submit(b)
    with SessionLocal() as db:
        for identity in (ja, jb):
            db.get(Job, identity).type = 'pod_native_composition'
        db.commit()
    for client, worker in computers:
        assert client.post(f'/api/v1/execution-devices/local/{worker}/claim').status_code == 200
    # These are isolated progress-only fixtures; no business handler is run.
    body = {'key': 'probe', 'progress': 65, 'completed': False}
    headers = {'X-Worker-Attempt': '1'}
    path = f'/api/v1/execution-devices/local/{wa}/jobs/{ja}/composition-progress'
    assert a.post(path, headers=headers, json=body).status_code == 200
    assert b.post(path, headers=headers, json=body).status_code == 403
    assert a.get(f'/api/v1/jobs/{ja}').json()['progress'] == 65
    assert b.get(f'/api/v1/jobs/{jb}').json()['progress'] == 0
