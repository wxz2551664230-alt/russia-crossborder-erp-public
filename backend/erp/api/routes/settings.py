from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from erp.core.database import get_db
from erp.core.jsonutil import dumps, loads
from erp.models import SystemSetting
from erp.schemas import SettingUpdate
from erp.services.config_resolution import DEFAULTS, system_defaults
from erp.services.legacy_migration import inspect_legacy_sources
from erp.modules.ozon_follow.legacy.services.pricing import OZON_RFBS_COMMISSION_TARIFFS


router = APIRouter(prefix="/settings", tags=["settings"])

@router.get("/ozon-follow-pricing-options")
def ozon_follow_pricing_options() -> dict:
    return {"modules": [{"name": module, "categories": [{"name": name, "rates": [str(rate * 100) for rate in rates]} for name, rates in categories.items()]} for module, categories in OZON_RFBS_COMMISSION_TARIFFS.items()]}


@router.get("")
def get_settings(session: Session = Depends(get_db)) -> dict:
    profiles = {item.key: loads(item.value_json, {}) for item in session.scalars(select(SystemSetting).order_by(SystemSetting.key)).all()}
    from erp.core.ai_concurrency import DEFAULT_LIMITS
    return {"defaults": system_defaults(session), "profiles": profiles, "ai_concurrency": profiles.get("ai_concurrency", DEFAULT_LIMITS), "legacy_sources": inspect_legacy_sources()}


@router.put("/{key}")
def update_setting(key: str, request: SettingUpdate, session: Session = Depends(get_db)) -> dict:
    if key == 'amazon_us_pricing':
        from erp.services.amazon_pricing import profile
        request.value = profile(request.value)
    if key == 'amazon_us_freight':
        from erp.domains.pod.us_freight import validate_rule
        request.value = validate_rule(request.value)
    if key == "ai_pricing":
        from erp.core.ai_usage import validate_pricing
        from erp.core.errors import AppError
        try:
            validate_pricing(request.value)
        except ValueError as error:
            raise AppError(str(error), code="ai_pricing_invalid", status_code=422) from error
    if key == "ai_concurrency":
        from erp.core.ai_concurrency import validate_limits
        from erp.core.errors import AppError
        try:
            validate_limits(request.value)
        except ValueError as error:
            raise AppError(str(error), code="ai_concurrency_invalid", status_code=422) from error
    item = session.get(SystemSetting, key)
    if not item:
        item = SystemSetting(key=key)
        session.add(item)
    item.value_json = dumps(request.value)
    session.commit()
    return {"key": key, "value": loads(item.value_json, None)}
