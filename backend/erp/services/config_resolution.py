from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from erp.core.errors import NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.core.security import vault
from erp.models import IntegrationConnection, Store, SystemSetting


DEFAULTS: dict[str, Any] = {
    "currency": "CNY",
    "request_timeout": 60,
    "request_retries": 3,
    "pricing": {
        "target_cost_profit_margin": 30,
        "price_multiplier": 3,
        "old_price_multiplier": 3.7,
        "return_rate": 10,
        "logistics_service_rate": 2,
    },
    "financial_policy": {
        "pricing_model": {
            "planned_ad_drr": 0.10,
            "minimum_profit_margin_after_planned_ads": 0.15,
        },
        "derived": {
            "estimated_pre_ad_contribution_margin": 0.25,
            "estimated_break_even_drr": 0.25,
        },
        "confidence": {
            "level": "MEDIUM",
            "reason": "根据店铺定价规则推导；尚未包含逐 SKU 精确采购、物流、佣金和退货成本。",
        },
    },
}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def system_defaults(session: Session) -> dict[str, Any]:
    item = session.get(SystemSetting, "defaults")
    return _deep_merge(DEFAULTS, loads(item.value_json, {}) if item else {})


def build_job_snapshot(
    session: Session,
    *,
    store_id: str | None = None,
    connection_id: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> str:
    public = system_defaults(session)
    secrets: dict[str, Any] = {}
    connection: IntegrationConnection | None = None
    store: Store | None = None
    if store_id:
        store = session.get(Store, store_id)
        if not store:
            raise NotFoundError("店铺不存在")
        if store.connection_id:
            connection = session.get(IntegrationConnection, store.connection_id)
    elif connection_id:
        connection = session.get(IntegrationConnection, connection_id)
        if not connection:
            raise NotFoundError("接口连接不存在")
    if connection:
        public = _deep_merge(public, {
            "connection_id": connection.id,
            "provider": connection.provider,
            "base_url": connection.base_url,
            **loads(connection.config_json, {}),
        })
        secrets.update(vault.decrypt(connection.secrets_encrypted) if connection.secrets_encrypted else {})
    if store:
        public = _deep_merge(public, {
            "store_id": store.id,
            "platform": store.platform,
            "store_name": store.name,
            "external_account_id": store.external_account_id or "",
            "offer_prefix": store.offer_prefix,
            "currency": store.currency,
            **loads(store.config_json, {}),
        })
        secrets.update(vault.decrypt(store.secrets_encrypted) if store.secrets_encrypted else {})
    public = _deep_merge(public, overrides or {})
    return dumps({"public": public, "secrets_encrypted": vault.encrypt(secrets)})


def resolve_job_snapshot(value: str) -> dict[str, Any]:
    snapshot = loads(value, {})
    public = dict(snapshot.get("public") or {})
    encrypted = str(snapshot.get("secrets_encrypted") or "")
    public["secrets"] = vault.decrypt(encrypted) if encrypted else {}
    return public
