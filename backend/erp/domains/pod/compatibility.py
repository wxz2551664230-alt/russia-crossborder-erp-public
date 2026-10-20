from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from erp.core.config import get_settings
from erp.core.jsonutil import dumps, loads
from erp.core.security import vault
from erp.domains.pod.composition import list_compositions
from erp.models import IntegrationConnection
from erp.serializers import connection_dict


def _connection(session: Session, provider: str, *, pod_oss: bool = False) -> IntegrationConnection | None:
    rows = session.scalars(select(IntegrationConnection).where(
        IntegrationConnection.provider == provider, IntegrationConnection.enabled.is_(True),
    ).order_by(IntegrationConnection.created_at)).all()
    if provider == "oss" and pod_oss:
        return next((row for row in rows if str(loads(row.config_json, {}).get("prefix") or "").strip("/") == "hicustom-products"), None)
    return rows[0] if rows else None


def _secret(status: dict[str, Any], key: str) -> dict[str, Any]:
    return dict(status.get(key) or {"configured": False, "masked": ""})


def workspace_configuration(session: Session) -> dict[str, Any]:
    hicustom = _connection(session, "hicustom")
    s2b = _connection(session, "s2b")
    sds = _connection(session, "sds")
    oss = _connection(session, "oss", pod_oss=True)
    chat = _connection(session, "ai_chat")
    h = connection_dict(hicustom) if hicustom else {"config": {}, "secret_status": {}}
    s = connection_dict(s2b) if s2b else {"config": {}, "secret_status": {}}
    d = connection_dict(sds) if sds else {"config": {}, "secret_status": {}}
    o = connection_dict(oss) if oss else {"config": {}, "secret_status": {}}
    a = connection_dict(chat) if chat else {"config": {}, "secret_status": {}}
    hc, sc, dc, oc, ac = h["config"], s["config"], d["config"], o["config"], a["config"]
    return {
        "configured": bool(hicustom), "base_url": hicustom.base_url if hicustom else "https://api.hicustom.com",
        "external_customer_id": str(hc.get("external_customer_id") or ""), "hicustom_store_id": str(hc.get("store_id") or ""),
        "composition": {"concurrency": int(hc.get("composition_concurrency") or 1)},
        "app_key": _secret(h["secret_status"], "app_key"), "app_secret": _secret(h["secret_status"], "app_secret"),
        "s2b": {"enabled": bool(s2b), "configured": bool(s2b), "environment": str(sc.get("environment") or "production"), "base_url": s2b.base_url if s2b else "https://openapi.s2bdiy.com", "store_id": str(sc.get("store_id") or ""), "using_public_test_credentials": False, "app_key": _secret(s["secret_status"], "app_key"), "app_secret": _secret(s["secret_status"], "app_secret")},
        "sds": {"enabled": bool(sds), "configured": bool(sds), "base_url": sds.base_url if sds else "https://merchantapi.sdspod.com", "app_id": _secret(d["secret_status"], "app_id"), "app_secret": _secret(d["secret_status"], "app_secret")},
        "oss": {"configured": bool(oss), "region": str(oc.get("region") or ""), "endpoint": str(oc.get("endpoint") or oss.base_url if oss else ""), "bucket": str(oc.get("bucket") or ""), "prefix": str(oc.get("prefix") or "hicustom-products"), "access_key_id": _secret(o["secret_status"], "access_key_id"), "access_key_secret": _secret(o["secret_status"], "access_key_secret")},
        "copywriting": {"configured": bool(chat), "api_host": chat.base_url if chat else "https://www.micuapi.ai", "api_path": str(ac.get("api_path") or "/v1/chat/completions"), "model": str(ac.get("default_model") or "gpt-5.6-terra"), "api_token": _secret(a["secret_status"], "api_key"), "concurrency": int(ac.get("concurrency") or 3), "title_min": int(ac.get("title_min") or 80), "title_max": int(ac.get("title_max") or 100), "description_min": int(ac.get("description_min") or 400), "description_max": int(ac.get("description_max") or 470), "category_prompt": str(ac.get("category_prompt") or ""), "page_keywords": str(ac.get("page_keywords") or "")},
        "ozon": {"configured": False}, "wildberries": {"configured": False}, "order_stores": [],
        "order_logistics_defaults": {"hicustom": {"company_name": "圆通速递", "logistics_id": 0}, "s2b": {"company_name": "圆通", "logistics_id": 109}},
    }


def workspace_status(session: Session) -> dict[str, Any]:
    return {"configuration": workspace_configuration(session), "output_root": str((get_settings().data_dir / "pod").resolve()), "counts": list_compositions(session, limit=2000)["counts"]}


def _upsert_connection(session: Session, provider: str, name: str, base_url: str, config_updates: dict[str, Any], secret_updates: dict[str, str], *, enabled: bool = True) -> IntegrationConnection:
    item = _connection(session, provider, pod_oss=provider == "oss")
    if not item:
        candidates = session.scalars(select(IntegrationConnection).where(IntegrationConnection.provider == provider).order_by(IntegrationConnection.created_at)).all()
        item = next((row for row in candidates if provider != "oss" or str(loads(row.config_json, {}).get("prefix") or "").strip("/") == "hicustom-products"), None)
    if not item:
        item = IntegrationConnection(provider=provider, name=name, base_url=base_url, config_json="{}", secrets_encrypted="", enabled=enabled)
        session.add(item); session.flush()
    item.base_url = str(base_url or item.base_url).strip(); item.enabled = enabled
    config = loads(item.config_json, {}); config.update({key: value for key, value in config_updates.items() if value is not None}); item.config_json = dumps(config)
    existing = vault.decrypt(item.secrets_encrypted) if item.secrets_encrypted else {}
    existing.update({key: value for key, value in secret_updates.items() if str(value or "").strip()})
    item.secrets_encrypted = vault.encrypt(existing); return item


def update_workspace_settings(session: Session, values: dict[str, Any]) -> dict[str, Any]:
    _upsert_connection(session, "hicustom", "HICUSTOM", str(values.get("hicustom_base_url") or "https://api.hicustom.com"), {"external_customer_id": values.get("hicustom_external_customer_id"), "store_id": values.get("hicustom_store_id"), "composition_concurrency": values.get("composition_concurrency")}, {"app_key": str(values.get("hicustom_app_key") or ""), "app_secret": str(values.get("hicustom_app_secret") or "")})
    if values.get("s2b_enabled", True):
        _upsert_connection(session, "s2b", "S2B", str(values.get("s2b_base_url") or "https://openapi.s2bdiy.com"), {"store_id": values.get("s2b_store_id"), "environment": "test" if "opentest" in str(values.get("s2b_base_url") or "") else "production"}, {"app_key": str(values.get("s2b_app_key") or ""), "app_secret": str(values.get("s2b_app_secret") or "")})
    else:
        for item in session.scalars(select(IntegrationConnection).where(IntegrationConnection.provider == "s2b")).all(): item.enabled = False
    if any(values.get(key) for key in ("oss_region", "oss_bucket", "oss_access_key_id", "oss_access_key_secret")):
        _upsert_connection(session, "oss", "POD 产品资产 OSS", str(values.get("oss_endpoint") or ""), {"region": values.get("oss_region"), "endpoint": values.get("oss_endpoint"), "bucket": values.get("oss_bucket"), "prefix": values.get("oss_prefix") or "hicustom-products"}, {"access_key_id": str(values.get("oss_access_key_id") or ""), "access_key_secret": str(values.get("oss_access_key_secret") or "")})
    model = str(values.get("copy_model") or "gpt-5.6-terra")
    if model not in {"gpt-5.6-terra", "gpt-5.6-sol"}: model = "gpt-5.6-terra"
    if any(values.get(key) for key in ("copy_api_host", "copy_api_token", "copy_model")):
        _upsert_connection(session, "ai_chat", "MICUAPI 文案模型", str(values.get("copy_api_host") or "https://www.micuapi.ai"), {"api_path": values.get("copy_api_path") or "/v1/chat/completions", "default_model": model, "concurrency": values.get("copy_concurrency"), "title_min": values.get("copy_title_min"), "title_max": values.get("copy_title_max"), "description_min": values.get("copy_description_min"), "description_max": values.get("copy_description_max"), "category_prompt": values.get("copy_category_prompt"), "page_keywords": values.get("copy_page_keywords")}, {"api_key": str(values.get("copy_api_token") or "")})
    session.commit(); return workspace_configuration(session)


def open_workspace_folder() -> dict[str, Any]:
    folder = (get_settings().data_dir / "pod").resolve(); folder.mkdir(parents=True, exist_ok=True)
    if __import__("sys").platform == "win32": __import__("os").startfile(str(folder))  # type: ignore[attr-defined]
    return {"opened": True, "path": str(folder)}
