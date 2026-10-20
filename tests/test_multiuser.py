from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from erp.auth_models import ErpSession, ErpUser, UserAuditEvent
from erp.core.access import COOKIE_NAME, permitted
from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.core.errors import ConflictError
from erp.main import app
from erp.models import utc_now
from erp.services import accounts

PASSWORD = "test-only-strong-password"


@pytest.fixture
def multiuser(monkeypatch):
    monkeypatch.setattr(get_settings(), "multiuser_enabled", True)
    monkeypatch.setattr(get_settings(), "session_cookie_secure", False)
    with SessionLocal() as db:
        return accounts.user_dict(accounts.create_user(db, username="admin", display_name="管理员", password=PASSWORD, role="admin", bootstrap=True))


def add_user(name="operator", role="operator"):
    with SessionLocal() as db:
        return accounts.user_dict(accounts.create_user(db, username=name, display_name=name, password=PASSWORD, role=role))


def login(client, username="admin", password=PASSWORD):
    return client.post("/api/v1/auth/login", json={"username": username, "password": password})


def test_remembered_login_cookie_and_server_expiry_agree_and_logout_revokes(multiuser):
    with TestClient(app) as browser:
        result = browser.post('/api/v1/auth/login', json={
            'username': 'admin', 'password': PASSWORD, 'remember': True})
        assert result.status_code == 200
        assert 'Max-Age=2592000' in result.headers['set-cookie']
        token = browser.cookies.get(COOKIE_NAME)
        with SessionLocal() as db:
            session = db.get(ErpSession, hashlib.sha256(token.encode()).hexdigest())
            assert session.expires_at - session.created_at == timedelta(days=30)
        assert browser.post('/api/v1/auth/logout').status_code == 200
        assert accounts.session_user(token) is None


def test_shared_data_separate_sessions_and_audit(multiuser):
    add_user()
    with TestClient(app) as admin, TestClient(app) as operator:
        assert login(admin).status_code == 200
        assert login(operator, "OPERATOR").status_code == 200
        response = admin.post("/api/v1/stores", json={"platform": "ozon", "name": "Shared store", "external_account_id": "test-client"})
        assert response.status_code == 201, response.text
        store = response.json()
        assert operator.get("/api/v1/stores").json()["items"][0]["id"] == store["id"]
        assert admin.cookies.get(COOKIE_NAME) != operator.cookies.get(COOKIE_NAME)
        denied = operator.patch(f'/api/v1/stores/{store["id"]}', json={"name": "not allowed"})
        assert denied.status_code == 403
        rows = admin.get("/api/v1/auth/audit").json()
        assert any(row["actor_name"] == "admin" and row["action"] == "POST" and row["status_code"] == 201 for row in rows)
        assert any(row["actor_name"] == "operator" and row["status_code"] == 403 for row in rows)
        assert PASSWORD not in str(rows)


def test_login_stores_hashes_and_rejects_shared_keys(multiuser):
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/config").json() == {"multiuser": True, "ui_profile": "full"}
        assert client.get("/api/v1/stores", headers={"Authorization": "Bearer test-operator-token"}).status_code == 401
        assert client.post("/api/v1/auth/login", json={"token": "test-operator-token"}).status_code == 401
        response = login(client)
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=strict" in response.headers["set-cookie"]
        token = client.cookies.get(COOKIE_NAME)
        with SessionLocal() as db:
            row = db.get(ErpUser, multiuser["id"])
            assert row.password_hash != PASSWORD and accounts.password_matches(PASSWORD, row.password_hash)
            assert db.get(ErpSession, hashlib.sha256(token.encode()).hexdigest()) is not None
            assert db.get(ErpSession, token) is None
        assert "password" not in response.text


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_role_boundaries(multiuser, role):
    add_user("member", role)
    with TestClient(app) as client:
        assert login(client, "member").status_code == 200
        for path in ["/auth/users", "/auth/audit", "/settings"]:
            assert client.get("/api/v1" + path).status_code == 403
        assert client.post("/api/v1/auth/users", json={}).status_code == 403
        assert client.post("/api/v1/jobs", json={}).status_code == 403
        assert client.post("/api/v1/pod/migrations", json={}).status_code == 403
        assert client.post("/api/v1/catalog/products", json={}).status_code == (403 if role == "viewer" else 422)
        assert client.get("/api/v1/stores").status_code == 200


def test_logout_replay_and_session_expiry(multiuser):
    with TestClient(app) as first, TestClient(app) as second:
        login(first); login(second)
        old = first.cookies.get(COOKIE_NAME)
        assert first.post("/api/v1/auth/logout").status_code == 200
        first.cookies.set(COOKIE_NAME, old)
        assert first.get("/api/v1/auth/session").status_code == 401
        assert second.get("/api/v1/auth/session").status_code == 200
        with SessionLocal() as db:
            db.execute(update(ErpSession).values(expires_at=utc_now() - timedelta(seconds=1)))
            db.commit()
        assert second.get("/api/v1/auth/session").status_code == 401


@pytest.mark.parametrize("changes", [{"enabled": False}, {"role": "viewer"}, {"password": "new-strong-password"}])
def test_account_edit_revokes_every_computer(multiuser, changes):
    member = add_user()
    with TestClient(app) as admin, TestClient(app) as first, TestClient(app) as second:
        login(admin); login(first, "operator"); login(second, "operator")
        assert admin.patch(f'/api/v1/auth/users/{member["id"]}', json=changes).status_code == 200
        assert first.get("/api/v1/auth/session").status_code == 401
        assert second.get("/api/v1/auth/session").status_code == 401
        if changes.get("enabled") is False:
            assert login(first, "operator").status_code == 401


def test_password_change_invalidates_all_sessions(multiuser):
    with TestClient(app) as first, TestClient(app) as second:
        login(first); login(second)
        assert first.post("/api/v1/auth/password", json={"old_password": PASSWORD, "new_password": "another-long-password"}).status_code == 200
        assert second.get("/api/v1/auth/session").status_code == 401
        assert login(second).status_code == 401
        assert login(second, password="another-long-password").status_code == 200


def test_admin_creation_validation_last_admin_and_secret_redaction(multiuser):
    with TestClient(app) as client:
        login(client)
        assert client.patch(f'/api/v1/auth/users/{multiuser["id"]}', json={"enabled": False}).status_code == 409
        assert client.patch(f'/api/v1/auth/users/{multiuser["id"]}', json={"role": "viewer"}).status_code == 409
        response = client.post("/api/v1/auth/users", json={"username": "bad", "password": "TooShort9"})
        assert response.status_code == 422 and "TooShort9" not in response.text
        response = client.post("/api/v1/auth/users", json={"username": "new-user", "password": PASSWORD})
        assert response.status_code == 201 and response.json()["role"] == "viewer"
        assert "password" not in response.text
        assert client.post("/api/v1/auth/users", json={"username": "NEW-USER", "password": PASSWORD}).status_code == 409
    with SessionLocal() as db:
        with pytest.raises(ConflictError):
            accounts.create_user(db, username="another", display_name="", password=PASSWORD, role="admin", bootstrap=True)


def test_last_admin_protected_across_concurrent_edits(multiuser):
    other = add_user("admin2", "admin")
    def demote(user):
        with SessionLocal() as db:
            try:
                accounts.update_user(db, user["id"], {"role": "viewer"}, multiuser)
                return "ok"
            except ConflictError:
                return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(demote, [multiuser, other]))
    assert sorted(results) == ["conflict", "ok"]


def test_persistent_login_throttle_and_no_forwarded_ip_trust(multiuser):
    for _ in range(5):
        with TestClient(app) as client:
            assert login(client, password="wrong").status_code == 401
    with TestClient(app) as client:
        response = client.post("/api/v1/auth/login", headers={"X-Forwarded-For": "203.0.113.42"}, json={"username": "admin", "password": PASSWORD})
        assert response.status_code == 429
        with SessionLocal() as db:
            assert db.scalar(select(UserAuditEvent).where(UserAuditEvent.action == "login_limited")) is not None


def test_secure_cookie_and_origin(multiuser, monkeypatch):
    monkeypatch.setattr(get_settings(), "session_cookie_secure", True)
    with TestClient(app, base_url="https://testserver") as client:
        assert "Secure" in login(client).headers["set-cookie"]
        assert client.post("/api/v1/auth/logout", headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.get("/api/v1/auth/session").status_code == 200


def test_ad_preview_and_execute_use_logged_in_person(multiuser, monkeypatch):
    from erp.models import Job
    member = add_user()
    with TestClient(app) as admin:
        login(admin)
        store=admin.post('/api/v1/stores',json={'platform':'ozon','name':'ad-owner-test',
                         'external_account_id':'ad-owner-test-client'}).json()
    with TestClient(app) as client:
        login(client, "operator")
        device='pc-'+'a'*32
        assert client.post('/api/v1/execution-devices/local/register',json={'id':device,'name':'PC',
                            'ad_preview_version':1}).status_code==200
        response=client.post(f"/api/v1/ad-optimization/actions/preview?store_id={store['id']}",
                             json={"action":"STOP_ADS_CANDIDATE","state_ids":["test"]})
        assert response.status_code==200,response.text
        with SessionLocal() as db:
            job=db.get(Job,response.json()['job_id'])
            assert job.created_by==member['id'] and job.target_worker_id==device
        denied=client.post(f"/api/v1/ad-optimization/actions/execute-batch?store_id={store['id']}",
                           json={"action":"STOP_ADS_CANDIDATE","state_ids":["test"]})
        assert denied.status_code==409


def test_connection_selectors_do_not_expose_configuration(multiuser):
    add_user("viewer", "viewer")
    with TestClient(app) as admin, TestClient(app) as viewer:
        login(admin); login(viewer, "viewer")
        response = admin.post("/api/v1/connections", json={"provider": "ai_chat", "name": "Team AI", "base_url": "https://example.invalid", "config": {"sensitive": "hidden"}, "secrets": {"api_key": "never-show"}})
        assert response.status_code == 201, response.text
        items = viewer.get("/api/v1/connections").json()["items"]
        assert set(items[0]) == {"id", "provider", "name", "enabled"}
        assert "never-show" not in str(items) and "hidden" not in str(items)


def test_two_operators_write_to_one_shared_catalog(multiuser):
    add_user("first"); add_user("second")
    with TestClient(app) as first, TestClient(app) as second:
        login(first, "first"); login(second, "second")
        response = first.post("/api/v1/catalog/products", json={"source_type": "self_selected", "sku": "SHARED-TEST", "name": "Shared catalog item"})
        assert response.status_code == 201, response.text
        product_id = response.json()["id"]
        response = second.get(f"/api/v1/catalog/products/{product_id}")
        assert response.status_code == 200 and response.json()["name"] == "Shared catalog item"


def test_ten_character_password_create_reset_and_change(multiuser):
    with TestClient(app) as admin:
        assert login(admin).status_code==200
        created=admin.post('/api/v1/auth/users',json={'username':'ten-char','password':'Abcd123456','role':'admin'})
        assert created.status_code==201
        identity=created.json()['id']
        assert admin.patch('/api/v1/auth/users/'+identity,json={'password':'NewPass123'}).status_code==200
        assert admin.patch('/api/v1/auth/users/'+identity,json={'password':'TooShort9'}).status_code==422
    with TestClient(app) as user:
        assert login(user,'ten-char','NewPass123').status_code==200
        assert user.post('/api/v1/auth/password',json={'old_password':'NewPass123','new_password':'Change1234'}).status_code==200
        assert login(user,'ten-char','Change1234').status_code==200
