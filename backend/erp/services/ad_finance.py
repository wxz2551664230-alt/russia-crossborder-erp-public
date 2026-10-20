from __future__ import annotations

from typing import Any


DEFAULT_FINANCIAL_POLICY: dict[str, Any] = {
    "planned_ad_drr": 0.10,
    "minimum_profit_margin_after_planned_ads": 0.15,
    "estimated_pre_ad_contribution_margin": 0.25,
    "estimated_break_even_drr": 0.25,
}


def _category_defaults(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, float] = {}
    for key, raw in value.items():
        candidate = raw.get("break_even_drr") if isinstance(raw, dict) else raw
        try:
            parsed = float(candidate)
        except (TypeError, ValueError):
            continue
        if 0 < parsed <= 1:
            result[str(key).strip()] = parsed
    return result


def flatten_financial_policy(value: dict[str, Any] | None) -> dict[str, Any]:
    source = value or {}
    pricing = source.get("pricing_model") if isinstance(source.get("pricing_model"), dict) else {}
    derived = source.get("derived") if isinstance(source.get("derived"), dict) else {}
    category_source = (
        source.get("category_break_even_drr")
        or source.get("category_defaults")
        or derived.get("category_break_even_drr")
        or {}
    )
    return {
        "planned_ad_drr": float(pricing.get("planned_ad_drr", DEFAULT_FINANCIAL_POLICY["planned_ad_drr"])),
        "minimum_profit_margin_after_planned_ads": float(pricing.get("minimum_profit_margin_after_planned_ads", DEFAULT_FINANCIAL_POLICY["minimum_profit_margin_after_planned_ads"])),
        "estimated_pre_ad_contribution_margin": float(derived.get("estimated_pre_ad_contribution_margin", DEFAULT_FINANCIAL_POLICY["estimated_pre_ad_contribution_margin"])),
        "estimated_break_even_drr": float(derived.get("estimated_break_even_drr", DEFAULT_FINANCIAL_POLICY["estimated_break_even_drr"])),
        "category_break_even_drr": _category_defaults(category_source),
    }


def category_break_even_drr(
    policy: dict[str, Any] | None,
    *,
    category_id: str = "",
    category_path: str = "",
) -> float | None:
    defaults = (policy or {}).get("category_break_even_drr")
    if not isinstance(defaults, dict):
        return None
    category_id = category_id.strip()
    category_path = category_path.strip()
    for key in (category_id, category_path):
        if key and key in defaults:
            return float(defaults[key])
    path_matches = [
        (key, value)
        for key, value in defaults.items()
        if category_path and (category_path == key or category_path.startswith(f"{key}/") or category_path.startswith(f"{key} / "))
    ]
    if not path_matches:
        return None
    _, value = max(path_matches, key=lambda item: len(item[0]))
    return float(value)


def assess_financials(
    actual_drr: float | None,
    *,
    exact_break_even_drr: float | None = None,
    category_break_even_drr: float | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    values = {**DEFAULT_FINANCIAL_POLICY, **(policy or {})}
    planned = float(values["planned_ad_drr"])
    minimum_profit = float(values["minimum_profit_margin_after_planned_ads"])
    estimated_break_even = float(values["estimated_break_even_drr"])
    if exact_break_even_drr is not None:
        break_even = float(exact_break_even_drr)
        source = "SKU_EXACT"
        confidence = "HIGH"
    elif category_break_even_drr is not None:
        break_even = float(category_break_even_drr)
        source = "CATEGORY_DEFAULT"
        confidence = "MEDIUM"
    else:
        break_even = estimated_break_even
        source = "STORE_DEFAULT"
        confidence = "MEDIUM"

    if actual_drr is None:
        status = "UNKNOWN"
        remaining = None
    else:
        remaining = break_even - actual_drr if source != "STORE_DEFAULT" else minimum_profit - max(0.0, actual_drr - planned)
        if actual_drr > break_even:
            status = "FINANCIAL_DANGER"
        elif actual_drr <= planned:
            status = "PROFIT_HEALTHY"
        elif actual_drr <= 0.15:
            status = "PROFIT_ACCEPTABLE"
        elif actual_drr <= 0.20:
            status = "GROWTH_COST"
        else:
            status = "STRATEGIC_TEST"
    return {
        "actual_drr": actual_drr,
        "planned_ad_drr": planned,
        "minimum_profit_margin_after_planned_ads": minimum_profit,
        "estimated_pre_ad_contribution_margin": float(values["estimated_pre_ad_contribution_margin"]),
        "estimated_break_even_drr": estimated_break_even,
        "break_even_drr": break_even,
        "effective_break_even_drr": break_even,
        "estimated_remaining_margin": remaining,
        "estimated_min_remaining_margin": remaining,
        "financial_status": status,
        "financial_confidence": confidence,
        "confidence": confidence,
        "financial_model_source": source,
        "source": source,
        "passed": None if actual_drr is None else actual_drr <= break_even,
    }
