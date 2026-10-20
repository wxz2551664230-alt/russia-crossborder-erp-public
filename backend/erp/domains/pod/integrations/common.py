from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from erp.core.errors import AppError
from erp.core.jsonutil import loads
from erp.core.security import vault
from erp.models import IntegrationConnection


@dataclass(frozen=True)
class SupplierConnectionConfig:
    provider: str
    connection_id: str
    base_url: str
    config: dict[str, Any]
    secrets: dict[str, Any]


def load_supplier_connection(session: Session, provider: str, connection_id: str = "") -> SupplierConnectionConfig:
    if connection_id:
        item = session.get(IntegrationConnection, connection_id)
        if item and (item.provider != provider or not item.enabled):
            item = None
    else:
        item = session.scalar(
            select(IntegrationConnection).where(
                IntegrationConnection.provider == provider,
                IntegrationConnection.enabled.is_(True),
            ).order_by(IntegrationConnection.created_at)
        )
    if not item:
        raise AppError(
            f"{provider.upper()} 接口未配置或已停用",
            code="pod_supplier_not_configured",
            status_code=503,
        )
    secrets = vault.decrypt(item.secrets_encrypted) if item.secrets_encrypted else {}
    return SupplierConnectionConfig(
        provider=provider,
        connection_id=item.id,
        base_url=item.base_url.rstrip("/"),
        config=loads(item.config_json, {}),
        secrets=secrets,
    )
