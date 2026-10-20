from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp.core.database import get_db
from erp.core.errors import AppError, ConflictError, NotFoundError
from erp.core.jsonutil import dumps
from erp.core.security import vault
from erp.models import IntegrationConnection
from erp.schemas import ConnectionCreate, ConnectionUpdate
from erp.serializers import connection_dict


router = APIRouter(prefix="/connections", tags=["connections"])


@router.get("")
def list_connections(request: Request, session: Session = Depends(get_db)) -> dict:
    items = session.scalars(select(IntegrationConnection).order_by(IntegrationConnection.provider, IntegrationConnection.name)).all()
    if request.state.user["role"] != "admin":
        return {"items": [{"id": item.id, "provider": item.provider, "name": item.name, "enabled": item.enabled} for item in items]}
    return {"items": [connection_dict(item) for item in items]}


@router.post("", status_code=201)
def create_connection(request: ConnectionCreate, session: Session = Depends(get_db)) -> dict:
    item = IntegrationConnection(
        provider=request.provider,
        name=request.name.strip(),
        base_url=request.base_url.strip(),
        config_json=dumps(request.config),
        secrets_encrypted=vault.encrypt(request.secrets),
        enabled=request.enabled,
    )
    session.add(item)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise ConflictError("同类型下已存在同名接口连接") from error
    session.refresh(item)
    return connection_dict(item)


def _get(session: Session, connection_id: str) -> IntegrationConnection:
    item = session.get(IntegrationConnection, connection_id)
    if not item:
        raise NotFoundError("接口连接不存在")
    return item


@router.patch("/{connection_id}")
def update_connection(connection_id: str, request: ConnectionUpdate, session: Session = Depends(get_db)) -> dict:
    item = _get(session, connection_id)
    values = request.model_dump(exclude_unset=True)
    if "name" in values:
        item.name = str(values["name"]).strip()
    if "base_url" in values:
        item.base_url = str(values["base_url"] or "").strip()
    if "config" in values:
        item.config_json = dumps(values["config"] or {})
    if "enabled" in values:
        item.enabled = bool(values["enabled"])
    if "secrets" in values:
        existing = vault.decrypt(item.secrets_encrypted) if item.secrets_encrypted else {}
        item.secrets_encrypted = vault.encrypt(vault.merge(existing, values["secrets"]))
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise ConflictError("同类型下已存在同名接口连接") from error
    session.refresh(item)
    return connection_dict(item)


@router.delete("/{connection_id}")
def delete_connection(connection_id: str, session: Session = Depends(get_db)) -> dict:
    item = _get(session, connection_id)
    if item.stores:
        raise ConflictError("该接口仍被店铺使用，请先解除店铺关联")
    session.delete(item)
    session.commit()
    return {"deleted": True}


@router.post("/{connection_id}/test")
def test_connection(connection_id: str, session: Session = Depends(get_db)) -> dict:
    item = _get(session, connection_id)
    secrets = vault.decrypt(item.secrets_encrypted) if item.secrets_encrypted else {}
    required = {
        "daji": ("app_key", "app_secret"),
        "elim": ("api_key",),
        "alibaba": ("app_key", "app_secret"),
        "oss": ("access_key_id", "access_key_secret"),
        "ai_chat": ("api_key",),
        "ai_image": ("api_key",),
        "hicustom": ("app_key", "app_secret"),
        "s2b": ("app_key", "app_secret"),
        "sds": ("app_id", "app_secret"),
        "tms": ("app_key", "app_secret"),
    }.get(item.provider, ())
    missing = [key for key in required if not secrets.get(key)]
    item.last_tested_at = datetime.now(timezone.utc)
    if missing:
        item.status = "failed"
        item.last_error = "缺少密钥字段：" + "、".join(missing)
        session.commit()
        raise AppError(item.last_error, code="connection_incomplete")
    item.status = "configured"
    item.last_error = ""
    session.commit()
    return {"ok": True, "status": item.status, "note": "配置完整；真实读写调用由对应业务任务执行"}
