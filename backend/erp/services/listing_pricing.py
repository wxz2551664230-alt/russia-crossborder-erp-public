from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

from sqlalchemy import select

from erp.core.jsonutil import dumps, loads
from erp.models import Listing, SourcingCandidate, SystemSetting
from erp.modules.ozon_follow.legacy.services.pricing import calculate_actual_sale_for_target_cost_margin


def source_landed_cost(source: SourcingCandidate):
    if source.unit_price_cny is not None:
        return Decimal(str(source.unit_price_cny)) + Decimal(str(source.domestic_delivery_cny or 0))
    return source.landed_cost_cny or 0


def effective_pricing_package(listing: Listing) -> dict:
    package = loads(listing.package_json, {})
    return {
        "depth_mm": package.get("depth_mm") or 300,
        "width_mm": package.get("width_mm") or 200,
        "height_mm": package.get("height_mm") or 100,
        "weight_g": package.get("weight_g") or (listing.research_product.weight_grams if listing.research_product else None),
    }


def auto_price_listing(session, listing: Listing) -> bool:
    if not listing.research_product_id:
        return False
    source = session.scalar(select(SourcingCandidate).where(
        SourcingCandidate.research_product_id == listing.research_product_id,
        SourcingCandidate.selected.is_(True),
    ))
    if not source or source.unit_price_cny is None:
        return False
    setting = session.get(SystemSetting, "pricing")
    profile = loads(setting.value_json, {}) if setting else {}
    package = effective_pricing_package(listing)
    priced = False
    for child in listing.store_listings:
        if not child.store.enabled:
            continue
        store_profile = {**profile, **(loads(child.store.config_json, {}).get("ozon_follow_pricing") or {})}
        try:
            result = calculate_actual_sale_for_target_cost_margin(
                target_cost_profit_margin=store_profile.get("target_cost_profit_margin") or 30,
                purchase_price=source_landed_cost(source),
                length_mm=package["depth_mm"], width_mm=package["width_mm"], height_mm=package["height_mm"], weight_g=package["weight_g"],
                category_module=store_profile.get("pricing_category_module", ""), product_category=store_profile.get("pricing_product_category", ""),
                sticker_fee=store_profile.get("pricing_sticker_fee") or 2.5, advertising_cost=store_profile.get("pricing_operating_cost") or 3,
                advertising_rate=store_profile.get("pricing_advertising_rate") or 0, return_rate=store_profile.get("pricing_return_rate") or 10,
                logistics_service_rate=store_profile.get("pricing_logistics_service_rate") or 2,
                price_multiplier=store_profile.get("pricing_price_multiplier") or 3, old_price_multiplier=store_profile.get("pricing_old_price_multiplier") or 3.7,
            )
        except (ValueError, TypeError):
            continue
        details = {key: str(value) for key, value in asdict(result).items()}
        child.pricing_json = dumps({**loads(child.pricing_json, {}), "price": details["ozon_price"], "old_price": details["ozon_old_price"], "action_price": details["actual_sale"], "currency": "CNY", "calculation": details})
        priced = True
    enabled_children = [child for child in listing.store_listings if child.store.enabled]
    if priced and enabled_children:
        listing.pricing_json = enabled_children[0].pricing_json
    return priced
