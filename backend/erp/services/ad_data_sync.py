from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from erp.core.errors import AppError
from erp.core.jsonutil import dumps, loads
from erp.integrations.ozon import OzonSellerAdapter
from erp.integrations.ozon_performance import OzonPerformanceClient
from erp.models import AdCampaign, AdCampaignSku, AdSku, Store
from erp.services.ad_optimization import aggregate_store, percentile
from erp.services.config_resolution import build_job_snapshot, resolve_job_snapshot


def _number(value: Any, *, micro: bool = False) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return None
    return parsed / 1_000_000 if micro else parsed


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _category_paths(tree: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}

    def visit(items: list[dict[str, Any]], parents: list[str]) -> None:
        for item in items:
            name = str(item.get("category_name") or "").strip()
            path = [*parents, name] if name else parents
            category_id = str(item.get("description_category_id") or "")
            if category_id:
                result[category_id] = " / ".join(path)
            children = [child for child in (item.get("children") or []) if isinstance(child, dict)]
            visit(children, path)

    visit(tree, [])
    return result


def _analytics_by_sku(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in rows:
        dimensions = item.get("dimensions") or []
        sku_id = str(dimensions[0].get("id") or "") if dimensions and isinstance(dimensions[0], dict) else ""
        names = item.get("metric_names") or []
        values = item.get("metrics") or []
        if sku_id:
            result[sku_id] = {name: values[index] if index < len(values) else None for index, name in enumerate(names)}
    return result


def sync_seller_catalog(session: Session, store: Store, *, days: int = 30) -> dict[str, int]:
    seller = OzonSellerAdapter(resolve_job_snapshot(build_job_snapshot(session, store_id=store.id)))
    products = seller.products()
    skus = [str(item.get("sku")) for item in products if item.get("sku")]
    details = seller.product_info(skus)
    ratings = seller.product_ratings(skus)
    date_to = date.today() - timedelta(days=1)
    quarter_from = date(date_to.year, ((date_to.month - 1) // 3) * 3 + 1, 1)
    analytics_windows: dict[str, dict[str, Any]] = {}
    for period, date_from in (
        ("7d", date_to - timedelta(days=6)),
        ("30d", date_to - timedelta(days=29)),
        ("quarter", quarter_from),
    ):
        period_rows = seller.analytics_by_sku(date_from.isoformat(), date_to.isoformat())
        analytics_windows[period] = {
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "by_sku": _analytics_by_sku(period_rows),
        }
    category_paths = _category_paths(seller.category_tree("ZH_HANS"))
    details_by_sku = {str(item.get("sku")): item for item in details if item.get("sku")}
    ratings_by_sku = {str(item.get("sku")): item for item in ratings if item.get("sku")}

    updated = 0
    created = 0
    for product in products:
        sku_id = str(product.get("sku") or "")
        if not sku_id:
            continue
        sku = session.scalar(select(AdSku).where(AdSku.store_id == store.id, AdSku.sku_id == sku_id))
        if not sku:
            sku = AdSku(store_id=store.id, sku_id=sku_id)
            session.add(sku)
            created += 1
        else:
            updated += 1
        info = details_by_sku.get(sku_id, {})
        sku.product_id = str(info.get("id") or product.get("product_id") or sku.product_id)
        sku.name = str(info.get("name") or sku.name)
        sku.category_id = str(info.get("description_category_id") or sku.category_id)
        sku.category_path = category_paths.get(sku.category_id, sku.category_path)
        sku.price = _number(info.get("price")) or sku.price
        sku.live_date = _datetime(info.get("created_at")) or sku.live_date
        stock_group = info.get("stocks") if isinstance(info.get("stocks"), dict) else {}
        stock_rows = [row for row in (stock_group.get("stocks") or []) if isinstance(row, dict)]
        sku.stock = sum(max(int(row.get("present") or 0) - int(row.get("reserved") or 0), 0) for row in stock_rows)
        sources = [row for row in (info.get("sources") or []) if isinstance(row, dict)]
        sku.fulfillment_type = ",".join(sorted({str(row.get("shipment_type") or row.get("source") or "") for row in sources if row.get("shipment_type") or row.get("source")}))
        metadata = loads(sku.metadata_json, {})
        images = [str(value) for value in (info.get("images") or []) if value]
        raw_primary = info.get("primary_image")
        if isinstance(raw_primary, list):
            primary_image = next((str(value) for value in raw_primary if value), "")
        else:
            primary_image = str(raw_primary or "")
        primary_image = primary_image or (images[0] if images else "") or str(metadata.get("image_url") or "")
        metadata.update({
            "seller_status": info.get("statuses") or {},
            "is_archived": bool(info.get("is_archived") or product.get("archived")),
            "commissions": info.get("commissions") or [],
            "content_rating": ratings_by_sku.get(sku_id, {}).get("rating"),
            "content_rating_groups": ratings_by_sku.get(sku_id, {}).get("groups") or [],
            "image_url": primary_image,
            "image_urls": images,
            "seller_synced_at": datetime.now(timezone.utc).isoformat(),
        })
        seller_analytics_windows = {
            period: {
                "date_from": window["date_from"],
                "date_to": window["date_to"],
                "metrics": window["by_sku"].get(sku_id, {}),
            }
            for period, window in analytics_windows.items()
        }
        metadata["seller_analytics_windows"] = seller_analytics_windows
        metadata["seller_analytics_30d"] = seller_analytics_windows["30d"]["metrics"]
        sku.metadata_json = dumps(metadata)
    session.flush()
    analytics_count = sum(len(window["by_sku"]) for window in analytics_windows.values())
    return {
        "products": len(products),
        "details": len(details),
        "ratings": len(ratings),
        "analytics": analytics_count,
        "analytics_windows": len(analytics_windows),
        "created": created,
        "updated": updated,
    }


def sync_campaign_configuration(session: Session, store: Store, client: OzonPerformanceClient, campaigns: list[dict[str, Any]], *, product_snapshots: dict[str, list[dict[str, Any]] | AppError] | None = None) -> dict[str, int]:
    sku_targets: dict[str, list[dict[str, Any]]] = {}
    product_count = 0
    product_errors = 0
    for raw in campaigns:
        campaign_id = str(raw.get("id") or "")
        if not campaign_id:
            continue
        campaign = session.scalar(select(AdCampaign).where(AdCampaign.store_id == store.id, AdCampaign.campaign_id == campaign_id))
        if not campaign:
            campaign = AdCampaign(store_id=store.id, campaign_id=campaign_id)
            session.add(campaign)
            session.flush()
        campaign.name = str(raw.get("title") or "")
        campaign.state = str(raw.get("state") or "")
        campaign.payment_type = str(raw.get("paymentType") or raw.get("PaymentType") or "")
        campaign.expense_strategy = str(raw.get("expenseStrategy") or "")
        campaign.budget_type = str(raw.get("budgetType") or "")
        new_budget = _number(raw.get("budget"), micro=True)
        if campaign.budget is not None and new_budget is not None and campaign.budget != new_budget:
            campaign.previous_budget = campaign.budget
            campaign.last_change_time = datetime.now(timezone.utc)
        campaign.budget = new_budget
        campaign.daily_budget = _number(raw.get("dailyBudget"), micro=True)
        campaign.weekly_budget = _number(raw.get("weeklyBudget"), micro=True)
        campaign.remote_created_at = _datetime(raw.get("createdAt"))
        campaign.remote_updated_at = _datetime(raw.get("updatedAt"))
        campaign.metadata_json = dumps(raw)
        try:
            snapshot = product_snapshots[campaign_id] if product_snapshots is not None else None
            if isinstance(snapshot, AppError):raise snapshot
            products = snapshot if snapshot is not None else client.campaign_products(campaign_id)
        except AppError as error:
            product_errors += 1
            metadata = loads(campaign.metadata_json, {})
            metadata["campaign_products_sync_warning"] = str(error)
            campaign.metadata_json = dumps(metadata)
            products = []
        product_count += len(products)
        for item in products:
            sku_id = str(item.get("sku") or "")
            if not sku_id:
                continue
            config = session.scalar(select(AdCampaignSku).where(AdCampaignSku.ad_campaign_id == campaign.id, AdCampaignSku.sku_id == sku_id))
            if not config:
                config = AdCampaignSku(ad_campaign_id=campaign.id, sku_id=sku_id)
                session.add(config)
            target = _number(item.get("targetCir"))
            target_drr = target / 100 if target and target > 0 else None
            config.title = str(item.get("title") or "")
            config.bid = _number(item.get("bid"), micro=True)
            if config.target_drr is not None and target_drr is not None and config.target_drr != target_drr:
                config.previous_target_drr = config.target_drr
                config.last_change_time = datetime.now(timezone.utc)
            config.target_drr = target_drr
            config.status = str(item.get("status") or raw.get("state") or "")
            config.metadata_json = dumps(item)
            if target_drr is not None:
                sku_targets.setdefault(sku_id, []).append({
                    "campaign_id": campaign_id,
                    "campaign_name": campaign.name,
                    "current_target_drr": target_drr,
                    "previous_target_drr": config.previous_target_drr,
                    "last_change_time": config.last_change_time.isoformat() if config.last_change_time else None,
                })
    for sku_id, targets in sku_targets.items():
        sku = session.scalar(select(AdSku).where(AdSku.store_id == store.id, AdSku.sku_id == sku_id))
        if sku:
            sku.target_drr = sum(float(target["current_target_drr"]) for target in targets) / len(targets)
            metadata = loads(sku.metadata_json, {})
            metadata["campaign_sku_targets"] = targets
            metadata["target_drr_is_analysis_average"] = len(targets) > 1
            metadata["multi_campaign_conflict"] = len(targets) > 1
            sku.metadata_json = dumps(metadata)
    session.flush()
    return {
        "campaigns": len(campaigns),
        "campaign_products": product_count,
        "skus_with_target_drr": len(sku_targets),
        "multi_campaign_conflicts": sum(1 for targets in sku_targets.values() if len(targets) > 1),
        "campaign_product_errors": product_errors,
    }


def assign_weak_labels_and_maturity(session: Session, store_id: str) -> dict[str, Any]:
    items = aggregate_store(session, store_id)
    ordered = [item for item in items if item.historical_sold_qty > 0]
    click_p25 = percentile((item.clicks for item in ordered), 0.25)
    click_p75 = percentile((item.clicks for item in ordered), 0.75)
    order_p75 = percentile((item.historical_sold_qty for item in ordered), 0.75)
    zero_order = [item for item in items if item.sold_qty == 0]
    zero_click_p90 = percentile((item.clicks for item in zero_order), 0.90)
    zero_spend_p90 = percentile((item.ad_spend for item in zero_order), 0.90)
    success_first_click_p95 = percentile((item.clicks_to_first_order for item in ordered), 0.95)
    success_first_spend_p95 = percentile((item.spend_to_first_order for item in ordered), 0.95)
    high_click_boundary = max(value for value in (zero_click_p90, success_first_click_p95) if value is not None) if zero_order and ordered else zero_click_p90
    high_spend_boundary = max(value for value in (zero_spend_p90, success_first_spend_p95) if value is not None) if zero_order and ordered else zero_spend_p90
    counts: dict[str, int] = {}
    maturity: dict[str, int] = {}
    for item in items:
        metadata = loads(item.sku.metadata_json, {})
        status = metadata.get("seller_status") if isinstance(metadata.get("seller_status"), dict) else {}
        externally_stopped = bool(metadata.get("is_archived") or status.get("status") in {"ARCHIVED", "BLOCKED", "ERROR"})
        if externally_stopped:
            label = "STOPPED_EXTERNALLY"
        elif item.historical_sold_qty >= 2:
            label = "MULTI_ORDER"
        elif item.historical_sold_qty == 1:
            label = "ORDERED"
        elif high_spend_boundary is not None and item.ad_spend >= high_spend_boundary and item.ad_spend > 0:
            label = "ZERO_ORDER_HIGH_SPEND"
        elif high_click_boundary is not None and item.clicks >= high_click_boundary and item.clicks > 0:
            label = "ZERO_ORDER_HIGH_CLICK"
        elif item.add_to_cart > 0:
            label = "HAS_CART_NO_ORDER"
        elif item.clicks > 0:
            label = "NO_CART_NO_ORDER"
        else:
            label = "NO_TRAFFIC"
        if item.impressions <= 0:
            level = "LEVEL_0"
        elif item.historical_sold_qty > 0 and (order_p75 is None or item.historical_sold_qty <= order_p75):
            level = "LEVEL_4"
        elif item.historical_sold_qty > 0:
            level = "LEVEL_5"
        elif item.clicks <= 0:
            level = "LEVEL_1"
        elif click_p25 is None or item.clicks < click_p25:
            level = "LEVEL_2"
        else:
            level = "LEVEL_3"
        item.sku.weak_label = label
        item.sku.data_maturity = level
        counts[label] = counts.get(label, 0) + 1
        maturity[level] = maturity.get(level, 0) + 1
    session.flush()
    return {"weak_labels": counts, "maturity": maturity, "learned_boundaries": {"ordered_click_p25": click_p25, "ordered_click_p75": click_p75, "ordered_order_p75": order_p75, "zero_order_click_p90": zero_click_p90, "zero_order_spend_p90": zero_spend_p90, "success_first_click_p95": success_first_click_p95, "success_first_spend_p95": success_first_spend_p95, "high_click_boundary": high_click_boundary, "high_spend_boundary": high_spend_boundary}}
