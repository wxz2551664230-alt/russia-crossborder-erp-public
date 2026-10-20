from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import timedelta
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.main import app
from erp.models import Job, WorkerNode, utc_now
from erp.services.accounts import create_user, user_dict
from erp.services.jobs import acquire_job

PASSWORD = "isolated-device-test-password"


def worker_headers(name):
    return {"Authorization": f"Bearer device-{name}-test-secret", "X-Worker-Attempt": "1"}


@pytest.fixture
def team(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "multiuser_enabled", True)
    monkeypatch.setattr(settings, "runtime_mode", "server")
    monkeypatch.setattr(settings, "session_cookie_secure", False)
    monkeypatch.setattr(settings, "worker_require_device_identity", True)
    monkeypatch.setattr(settings, "worker_token_hashes", {name: hashlib.sha256(f"device-{name}-test-secret".encode()).hexdigest() for name in ("alpha", "beta", "alpha-two")})
    users = {}
    with SessionLocal() as db:
        for name, role in [("alice", "admin"), ("bob", "operator")]:
            users[name] = user_dict(create_user(db, username=name, display_name=name, password=PASSWORD, role=role))
    with ExitStack() as stack:
        clients = {name: stack.enter_context(TestClient(app)) for name in ("alice", "bob", "second-login", "worker")}
        for name in ("alice", "bob", "second-login"):
            assert clients[name].post("/api/v1/auth/login", json={"username": "alice" if name == "second-login" else name, "password": PASSWORD}).status_code == 200
        for device, owner in [("alpha", "alice"), ("beta", "bob"), ("alpha-two", "alice")]:
            assert clients["worker"].post("/api/v1/workers/register", headers=worker_headers(device), json={"id": device, "capabilities": ["worker_probe", "image_prepare"]}).status_code == 200
            assert clients["alice"].patch(f"/api/v1/execution-devices/{device}/owner", json={"user_id": users[owner]["id"]}).status_code == 200
        yield clients, users


def select_device(client, device):
    assert client.post("/api/v1/execution-devices/select", json={"worker_id": device}).status_code == 200


def submit(client):
    result = client.post("/api/v1/execution-devices/probe")
    assert result.status_code == 202, result.text
    return result.json()["job_id"]


def claim(client, device):
    return client.post(f"/api/v1/workers/{device}/claim", headers=worker_headers(device), json={"capabilities": ["worker_probe"]})


def test_binding_selection_and_missing_device_fail_closed(team):
    clients, users = team
    assert clients["alice"].post("/api/v1/execution-devices/probe").status_code == 409
    assert clients["bob"].post("/api/v1/execution-devices/select", json={"worker_id": "alpha"}).status_code == 409
    assert clients["bob"].patch("/api/v1/execution-devices/beta/owner", json={"user_id": users["alice"]["id"]}).status_code == 403
    assert [d["id"] for d in clients["bob"].get("/api/v1/execution-devices").json()["items"]] == ["beta"]


def test_each_person_only_claims_their_own_task_and_reports_back(team):
    clients, users = team
    select_device(clients["alice"], "alpha"); select_device(clients["bob"], "beta")
    alice_job = submit(clients["alice"]); bob_job = submit(clients["bob"])
    second = claim(clients["worker"], "beta").json()
    assert second["id"] == bob_job and second["created_by"] == users["bob"]["id"]
    assert claim(clients["worker"], "beta").json() is None
    first = claim(clients["worker"], "alpha").json()
    assert first["id"] == alice_job and first["target_worker_id"] == "alpha"
    assert clients["worker"].post(f"/api/v1/workers/beta/jobs/{alice_job}/complete", headers=worker_headers("beta"), json={"result": {"ok": True}}).status_code == 403
    result = clients["worker"].post(f"/api/v1/workers/alpha/jobs/{alice_job}/complete", headers=worker_headers("alpha"), json={"result": {"ok": True}})
    assert result.status_code == 200 and result.json()["status"] == "completed"


def test_offline_and_expired_tasks_never_move_to_another_computer(team):
    clients, _ = team
    select_device(clients["alice"], "alpha")
    job_id = submit(clients["alice"])
    with SessionLocal() as db:
        db.get(WorkerNode, "alpha").last_seen_at = utc_now() - timedelta(days=1)
        db.commit()
    assert claim(clients["worker"], "beta").json() is None
    with SessionLocal() as db:
        assert db.get(Job, job_id).status == "queued"
        assert acquire_job(db, "local-all") is None  # Direct local DB consumers cannot steal assigned jobs.
    assert claim(clients["worker"], "alpha").json()["id"] == job_id
    with SessionLocal() as db:
        db.get(Job, job_id).lease_expires_at = utc_now() - timedelta(seconds=1)
        db.commit()
    assert claim(clients["worker"], "beta").json() is None
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.status == "running" and job.target_worker_id == "alpha"
    assert claim(clients["worker"], "alpha").json() is None  # Original computer starts retry backoff.
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.status == "retrying"
        job.heartbeat_at = utc_now() - timedelta(seconds=10); db.commit()
    assert claim(clients["worker"], "alpha").json()["id"] == job_id


def test_concurrent_browser_sessions_keep_distinct_computers(team):
    clients, users = team
    select_device(clients["alice"], "alpha"); select_device(clients["second-login"], "alpha-two")
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(submit, [clients["alice"], clients["second-login"]]))
    with SessionLocal() as db:
        assert [db.get(Job, value).target_worker_id for value in ids] == ["alpha", "alpha-two"]
        assert all(db.get(Job, value).created_by == users["alice"]["id"] for value in ids)


def test_active_tasks_block_device_transfer_and_disabled_user_cannot_claim(team):
    clients, users = team
    select_device(clients["bob"], "beta")
    submit(clients["bob"])
    assert clients["alice"].patch("/api/v1/execution-devices/beta/owner", json={"user_id": users["alice"]["id"]}).status_code == 409
    assert clients["alice"].patch(f'/api/v1/auth/users/{users["bob"]["id"]}', json={"enabled": False}).status_code == 200
    assert claim(clients["worker"], "beta").status_code == 403


def test_unsupported_task_is_not_silently_run_on_server(team):
    clients, _ = team
    select_device(clients["alice"], "alpha")
    response = clients["alice"].post("/api/v1/jobs", json={"type": "asset_index", "idempotency_key": "unsupported"})
    assert response.status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(Job.id)) is None


def test_device_transfer_clears_old_browser_selection(team):
    clients, users = team
    select_device(clients["alice"], "alpha")
    assert clients["alice"].patch("/api/v1/execution-devices/alpha/owner", json={"user_id": users["bob"]["id"]}).status_code == 200
    assert clients["alice"].get("/api/v1/execution-devices").json()["current_worker_id"] == ""
    assert clients["alice"].post("/api/v1/execution-devices/probe").status_code == 409


def test_disabled_user_cannot_return_results(team):
    clients, users = team
    select_device(clients["bob"], "beta")
    job_id = submit(clients["bob"])
    assert claim(clients["worker"], "beta").json()["id"] == job_id
    assert clients["alice"].patch(f'/api/v1/auth/users/{users["bob"]["id"]}', json={"enabled": False}).status_code == 200
    response = clients["worker"].post(f"/api/v1/workers/beta/jobs/{job_id}/complete", headers=worker_headers("beta"), json={"result": {"ok": True}})
    assert response.status_code == 403


def test_real_client_probe_loop_uses_assigned_computer(team, monkeypatch):
    import httpx
    import logging
    from erp import remote_worker
    clients, _ = team
    select_device(clients["alice"], "alpha")
    job_id = submit(clients["alice"])
    settings = get_settings()
    monkeypatch.setattr(settings, "control_plane_url", "https://testserver")
    monkeypatch.setattr(settings, "worker_id", "alpha")
    monkeypatch.setattr(settings, "worker_token", "device-alpha-test-secret")
    original_client = httpx.Client
    def transport(request):
        response = clients["worker"].request(request.method, str(request.url), headers=dict(request.headers), content=request.content)
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)
    monkeypatch.setattr(remote_worker.httpx, "Client", lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(transport)))
    remote_worker._run_session(once=True, logger=logging.getLogger("test-device"))
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.status == "completed" and job.target_worker_id == "alpha"
        assert job.attempts == 1
