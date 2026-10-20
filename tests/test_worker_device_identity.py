import hashlib

import pytest

from erp.core.config import get_settings


@pytest.fixture
def device_credentials(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "worker_require_device_identity", True)
    monkeypatch.setattr(settings, "worker_token_hashes", {
        "office-a": hashlib.sha256(b"device-a-secret").hexdigest(),
        "office-b": hashlib.sha256(b"device-b-secret").hexdigest(),
    })
    return settings


def headers(device):
    return {"Authorization": f"Bearer device-{device}-secret", "X-Worker-Attempt": "1"}


def test_two_devices_share_queue_but_not_identity(client, device_credentials):
    for device in ("a", "b"):
        assert client.post("/api/v1/workers/register", headers=headers(device), json={"id": f"office-{device}"}).status_code == 200
    assert client.post("/api/v1/workers/register", headers=headers("a"), json={"id": "office-b"}).status_code == 403
    job = client.post("/api/v1/jobs", json={"type": "worker_probe", "idempotency_key": "device-isolation"}).json()
    claimed = client.post("/api/v1/workers/office-a/claim", headers=headers("a"), json={"capabilities": ["worker_probe"]})
    assert claimed.json()["id"] == job["id"]
    second = client.post("/api/v1/workers/office-b/claim", headers=headers("b"), json={"capabilities": ["worker_probe"]})
    assert second.json() is None
    path = f"/api/v1/workers/office-a/jobs/{job['id']}/complete"
    assert client.post(path, headers=headers("b"), json={"result": {}}).status_code == 403
    assert client.post(path, headers=headers("a"), json={"result": {"ok": True}}).status_code == 200
    assert client.get("/api/v1/stores", headers=headers("a")).status_code == 401


@pytest.mark.parametrize("suffix,body", [("heartbeat", {}), ("claim", {}), ("jobs/any/heartbeat", {}), ("jobs/any/complete", {"result": {}}), ("jobs/any/fail", {"error_message": "test"})])
def test_all_device_routes_reject_impersonation(client, device_credentials, suffix, body):
    assert client.post(f"/api/v1/workers/office-b/{suffix}", headers=headers("a"), json=body).status_code == 403


def test_revoked_last_device_does_not_reenable_shared_token(client, device_credentials, monkeypatch):
    monkeypatch.setattr(device_credentials, "worker_token_hashes", {})
    for token in ("device-a-secret", "test-worker-token"):
        response = client.post("/api/v1/workers/register", headers={"Authorization": f"Bearer {token}"}, json={"id": "office-a"})
        assert response.status_code == 401


def test_mapping_alone_disables_shared_credentials(client, device_credentials, monkeypatch):
    monkeypatch.setattr(device_credentials, "worker_require_device_identity", False)
    result = client.post("/api/v1/workers/register", headers={"Authorization": "Bearer test-worker-token"}, json={"id": "office-a"})
    assert result.status_code == 401


def test_duplicate_device_credentials_fail_closed(client, device_credentials, monkeypatch):
    digest = hashlib.sha256(b"device-a-secret").hexdigest()
    monkeypatch.setattr(device_credentials, "worker_token_hashes", {"office-a": digest, "office-b": digest})
    assert client.post("/api/v1/workers/register", headers=headers("a"), json={"id": "office-a"}).status_code == 401
