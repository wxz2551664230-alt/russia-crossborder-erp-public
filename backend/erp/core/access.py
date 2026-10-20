"""Single-operator authentication. Worker credentials never grant operator access."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import urlsplit

from fastapi import Request

from erp.core.config import get_settings
from erp.core.errors import AppError

COOKIE_NAME = "erp_session"
SESSION_SECONDS = 12 * 60 * 60


def access_key() -> str:
    settings = get_settings()
    if settings.access_token:
        return settings.access_token
    if settings.runtime_mode != "local":
        raise AppError("请配置 ERP_ACCESS_TOKEN 后启动控制中心", code="access_not_configured", status_code=503)
    path = settings.data_dir / "operator-access.key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        token = secrets.token_urlsafe(32)
        stream.write(token)
    return token


def valid_key(value: str) -> bool:
    return bool(value) and hmac.compare_digest(value.encode("utf-8"), access_key().encode("utf-8"))


def session_cookie() -> str:
    body = f"{int(time.time()) + SESSION_SECONDS}.{secrets.token_hex(16)}"
    signature = hmac.new(access_key().encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def authenticated(request: Request) -> bool:
    if get_settings().multiuser_enabled:
        return principal(request) is not None
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer ") and valid_key(authorization[7:]):
        return True
    value = request.cookies.get(COOKIE_NAME, "")
    try:
        body, signature = value.rsplit(".", 1)
        expires, _nonce = body.split(".", 1)
        expected = hmac.new(access_key().encode(), body.encode(), hashlib.sha256).hexdigest()
        return int(expires) > time.time() and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


def principal(request: Request) -> dict | None:
    if get_settings().multiuser_enabled:
        from sqlalchemy.exc import SQLAlchemyError
        from erp.services.accounts import session_user
        try:
            return session_user(request.cookies.get(COOKIE_NAME, ""))
        except SQLAlchemyError as error:
            raise AppError("账号服务未就绪，请检查数据库迁移", code="accounts_not_ready", status_code=503) from error
    if authenticated(request):
        return {"id": "owner", "username": "owner", "display_name": "本地管理员", "role": "admin", "enabled": True}
    return None


def permitted(user: dict, method: str, path: str) -> bool:
    """Server-enforced baseline roles; all users share the business dataset."""
    if user["role"] == "admin":
        return True
    parts = path.strip("/").split("/")
    root = parts[0]
    if root == "auth":
        return path in {"/auth/session", "/auth/logout", "/auth/password", "/auth/client-capabilities"}
    if root == "execution-devices":
        if path.startswith('/execution-devices/local/'):
            return method in {'GET','POST'} and user['role'] == 'operator'
        return (path == "/execution-devices" and method == "GET") or (path == "/execution-devices/select" and method == "POST") or (path == "/execution-devices/probe" and method == "POST" and user["role"] == "operator")
    if root == 'jobs' and method == 'POST' and len(parts) == 3 and parts[2] in {'pause','resume','cancel','retry'}:
        return user['role'] == 'operator'
    if path in {'/pod/legacy-health','/pod/legacy-api/status','/pod/legacy-api/jobs'} and method=='GET':
        return True
    if path in {'/pod/legacy-api/jobs','/pod/legacy-api/jobs/multi'} and method=='POST':
        return user['role']=='operator'
    if path=='/pod/legacy-api/jobs/finished' and method=='DELETE':
        return user['role']=='operator'
    if path.startswith('/pod/legacy-api/jobs/') and method=='POST' and parts[-1] in {'retry','retry-oss','cancel'}:
        return user['role']=='operator'
    if path == "/connections" and method == "GET":
        return True  # The route returns only a selector list to non-admins.
    if root == 'stores' and method == 'POST' and len(parts) == 4 and parts[2] == 'ozon-options' and parts[3] in {'warehouses','promotions','category_attributes','category_tree'}:
        return user['role'] == 'operator'
    if root in {"connections", "settings", "users"} or any("migration" in p or p.startswith("legacy") for p in parts):
        return False
    if method in {"GET", "HEAD", "OPTIONS"}:
        return True
    if user["role"] != "operator":
        return False
    return root not in {"stores", "workers", "jobs"}


def trusted_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True  # Non-browser clients must still authenticate.
    try:
        parsed = urlsplit(origin)
        expected = urlsplit(f"{request.url.scheme}://{request.headers.get('host', '')}")
        return (
            parsed.scheme in {"http", "https"}
            and not parsed.username and not parsed.password
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
            and (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
            == (expected.scheme, expected.hostname, expected.port or (443 if expected.scheme == "https" else 80))
        )
    except ValueError:
        return False
