from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from erp.auth_models import AuthControl, AuthThrottle, ErpSession, ErpUser, UserAuditEvent
from erp.core.database import SessionLocal
from erp.core.errors import AppError, ConflictError, NotFoundError
from erp.models import utc_now

SESSION_HOURS = 12
MIN_PASSWORD_LENGTH = 10
PASSWORD_ROUNDS = 600_000
ROLES = {"admin", "operator", "viewer"}


def password_hash(password: str) -> str:
    if not MIN_PASSWORD_LENGTH <= len(password) <= 128:
        raise AppError(f"密码需为{MIN_PASSWORD_LENGTH}至128个字符", code="password_invalid", status_code=422)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ROUNDS)
    return f"pbkdf2_sha256${PASSWORD_ROUNDS}${salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or int(rounds) != PASSWORD_ROUNDS:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def normalized_username(value: str) -> str:
    name = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", name):
        raise AppError("账号需为3至64位字母、数字、点、下划线或短横线", code="username_invalid", status_code=422)
    return name


def user_dict(user: ErpUser) -> dict:
    return {"id": user.id, "username": user.username, "display_name": user.display_name,
            "role": user.role, "enabled": user.enabled}


def lock_accounts(db: Session) -> None:
    # Shared DB row serializes last-admin checks and login counters across API
    # processes. No per-computer file lock or in-memory-only rate limiter.
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    db.execute(insert(AuthControl).values(id="accounts", revision=0).on_conflict_do_nothing(index_elements=["id"]))
    db.execute(update(AuthControl).where(AuthControl.id == "accounts").values(revision=AuthControl.revision + 1))


def audit(db: Session, actor: dict | None, action: str, target: str = "", status_code: int = 200) -> UserAuditEvent:
    actor = actor or {}
    row = UserAuditEvent(actor_id=actor.get("id", ""), actor_name=actor.get("username", ""),
                         action=action, target=target[:500], status_code=status_code)
    db.add(row)
    return row


def create_user(db: Session, *, username: str, display_name: str, password: str, role: str, actor: dict | None = None, bootstrap: bool = False) -> ErpUser:
    name = normalized_username(username)
    if role not in ROLES:
        raise AppError("账号角色无效", code="role_invalid", status_code=422)
    hashed = password_hash(password)
    lock_accounts(db)
    if bootstrap and db.scalar(select(func.count()).select_from(ErpUser)):
        raise ConflictError("已存在用户，不能重复初始化管理员")
    if db.scalar(select(ErpUser).where(ErpUser.username == name)):
        raise ConflictError("该账号已存在")
    row = ErpUser(username=name, display_name=display_name.strip() or name, password_hash=hashed, role=role, enabled=True)
    db.add(row); db.flush()
    audit(db, actor or user_dict(row), "account_create", row.id)
    db.commit(); db.refresh(row)
    return row


def update_user(db: Session, user_id: str, changes: dict, actor: dict) -> ErpUser:
    hashed = password_hash(changes["password"]) if changes.get("password") is not None else None
    lock_accounts(db)
    user = db.get(ErpUser, user_id)
    if not user:
        raise NotFoundError("用户不存在")
    db.refresh(user)
    role, enabled = changes.get("role", user.role), changes.get("enabled", user.enabled)
    if role not in ROLES:
        raise AppError("账号角色无效", code="role_invalid", status_code=422)
    if user.enabled and user.role == "admin" and (not enabled or role != "admin"):
        admins = db.scalar(select(func.count()).select_from(ErpUser).where(ErpUser.enabled.is_(True), ErpUser.role == "admin"))
        if admins <= 1:
            raise ConflictError("至少保留一个启用的管理员账号")
    user.role, user.enabled = role, enabled
    if "display_name" in changes:
        user.display_name = changes["display_name"].strip() or user.username
    if hashed:
        user.password_hash = hashed
    # Any administrative account edit revokes all existing sessions immediately.
    db.execute(delete(ErpSession).where(ErpSession.user_id == user_id))
    audit(db, actor, "account_update", user_id)
    db.commit(); db.refresh(user)
    return user


def login_user(username: str, password: str, client_ip: str, *, remember: bool = False) -> tuple[dict, str]:
    name = username.strip().lower()
    now = utc_now(); cutoff = now - timedelta(minutes=15)
    keys = [(hashlib.sha256(("user:" + name).encode()).hexdigest(), 5),
            (hashlib.sha256(("ip:" + client_ip).encode()).hexdigest(), 30)]
    with SessionLocal() as db:
        lock_accounts(db)
        db.execute(delete(AuthThrottle).where(AuthThrottle.window_start < cutoff))
        buckets = []
        for key, limit in keys:
            row = db.get(AuthThrottle, key)
            if row and row.failures >= limit:
                audit(db, None, "login_limited", status_code=429)
                db.commit()
                raise AppError("登录尝试过多，请15分钟后再试", code="login_limited", status_code=429)
            if not row:
                row = AuthThrottle(key=key, failures=0, window_start=now); db.add(row)
            buckets.append(row)
        user = db.scalar(select(ErpUser).where(ErpUser.username == name))
        dummy = f"pbkdf2_sha256${PASSWORD_ROUNDS}${'00' * 16}${'00' * 32}"
        valid = password_matches(password, user.password_hash if user else dummy)
        if not user or not user.enabled or not valid:
            for row in buckets: row.failures += 1
            audit(db, None, "login_failed", status_code=401)
            db.commit()
            raise AppError("账号或密码不正确，或账号已停用", code="invalid_credentials", status_code=401)
        buckets[0].failures = 0
        token = "u1." + secrets.token_urlsafe(32)
        db.add(ErpSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id,
                          created_at=now, expires_at=now + (timedelta(days=30) if remember else timedelta(hours=SESSION_HOURS))))
        db.execute(delete(ErpSession).where(ErpSession.expires_at < now))
        principal = user_dict(user)
        audit(db, principal, "login_success")
        db.commit()
        return principal, token


def session_user(token: str) -> dict | None:
    if not token.startswith("u1.") or len(token) > 100:
        return None
    with SessionLocal() as db:
        row = db.execute(select(ErpUser, ErpSession.worker_id).join(ErpSession, ErpSession.user_id == ErpUser.id).where(
            ErpSession.token_hash == hashlib.sha256(token.encode()).hexdigest(),
            ErpSession.expires_at > utc_now(), ErpUser.enabled.is_(True))).first()
        return {**user_dict(row[0]), "worker_id": row[1]} if row else None


def logout_user(token: str, actor: dict) -> None:
    with SessionLocal() as db:
        db.execute(delete(ErpSession).where(ErpSession.token_hash == hashlib.sha256(token.encode()).hexdigest()))
        audit(db, actor, "logout")
        db.commit()


def change_password(actor: dict, old_password: str, new_password: str) -> None:
    hashed = password_hash(new_password)
    with SessionLocal() as db:
        lock_accounts(db)
        row = db.get(ErpUser, actor["id"])
        if not row or not row.enabled or not password_matches(old_password, row.password_hash):
            raise AppError("当前密码不正确", code="invalid_credentials", status_code=401)
        row.password_hash = hashed
        db.execute(delete(ErpSession).where(ErpSession.user_id == row.id))
        audit(db, actor, "password_change", row.id)
        db.commit()


def begin_operation(actor: dict, method: str, path: str, status: int = 0) -> str:
    with SessionLocal() as db:
        row = audit(db, actor, method, path, status)
        db.commit()
        return row.id


def finish_operation(event_id: str, status: int) -> None:
    with SessionLocal() as db:
        db.execute(update(UserAuditEvent).where(UserAuditEvent.id == event_id).values(status_code=status))
        db.commit()
