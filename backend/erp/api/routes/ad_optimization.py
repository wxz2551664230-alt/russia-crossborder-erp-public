from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp.core.database import get_db
from erp.core.config import get_settings
from erp.core.errors import AppError, ConflictError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.core.security import vault
from erp.integrations.ozon_performance import OzonPerformanceClient
from erp.models import (
    AdAction,
    AdBacktestRun,
    AdCampaign,
    AdCampaignDailyMetric,
    AdCampaignSku,
    AdDailyMetric,
    AdSku,
    AdSkuState,
    AdSkuStateHistory,
    AdThresholdVersion,
    Job,
    Store,
)
from erp.schemas import AdActionCreate, AdBatchExecutionRequest, AdOutcomeUpdate
from erp.services.ad_analysis import build_ad_analysis, write_ad_reports
from erp.services.ad_data_sync import assign_weak_labels_and_maturity, sync_campaign_configuration, sync_seller_catalog
from erp.services.ad_finance import assess_financials, category_break_even_drr, flatten_financial_policy
from erp.services.ad_optimization import analyze_store, proposed_scale_target, run_backtest, safe_div
from erp.services.config_resolution import system_defaults
from erp.services.jobs import ACTIVE_STATUSES, enqueue_job


router = APIRouter(prefix="/ad-optimization", tags=["ad-optimization"])

STATE_LABELS = {
    "NEW": "新品",
    "COLD_START": "冷启动",
    "OBSERVATION": "观察中",
    "TRAFFIC_STARVED": "流量不足",
    "LOW_IMPRESSIONS": "展示不足",
    "INSUFFICIENT_CLICK_SAMPLE": "点击样本不足",
    "STALE_DATA": "数据陈旧",
    "LOW_CTR": "点击率偏低",
    "LOW_ATC": "加购偏低",
    "HIGH_CART_LOW_ORDER": "高加购低成交",
    "LOW_CVR": "成交率偏低",
    "ZERO_ORDER_HIGH_CLICK": "高点击零成交",
    "ZERO_ORDER_HIGH_SPEND": "高消耗零成交",
    "BAD_ECONOMICS": "经济性不成立",
    "SCALE_CANDIDATE": "可放量",
    "HEALTHY": "健康",
    "GROWTH": "增长中",
    "RETEST": "复测中",
    "STOP_ADS": "停止广告",
    "NO_RECENT_DATA": "近期无数据",
    "NO_METRIC_DATA": "无广告数据",
    "ACTIVE_NO_EXPOSURE": "运行中无曝光",
    "RECENT_ZERO_ORDER_DECAY": "历史出单后衰退",
}

RECOMMENDATION_LABELS = {
    "OBSERVE": "继续观察",
    "INCREASE_TARGET_DRR": "建议提高目标费用并实验",
    "OPTIMIZE_CREATIVE": "建议优化主图和标题",
    "OPTIMIZE_LISTING": "建议优化商品卡并复测",
    "CHECK_OFFER": "建议检查价格、配送与评价",
    "REDUCE_TARGET_DRR": "建议降低目标费用并复测",
    "STOP_ADS": "建议停止主动广告",
    "STOP_ADS_CANDIDATE": "停止广告候选",
    "INCREASE_TARGET_DRR_CANDIDATE": "提高目标费用候选",
    "INCREASE_BUDGET_CANDIDATE": "提高预算候选",
    "DIAGNOSE_NO_EXPOSURE": "自动排查无曝光",
    "REVIEW_REQUIRED": "需要人工复核",
    "SCALE": "建议放量",
    "HOLD": "保持当前策略",
}

ALIASES = {
    "sku_id": ("sku_id", "sku", "offer_id", "offerid", "商品sku"),
    "product_id": ("product_id", "ozon_product_id", "商品id"),
    "name": ("name", "title", "商品名称"),
    "category_id": ("category_id", "类目id"),
    "category_path": ("category_path", "category", "类目", "类目路径"),
    "price": ("price", "售价", "price_rub"),
    "live_date": ("live_date", "create_date", "上架日期"),
    "ad_start_date": ("ad_start_date", "广告开始日期"),
    "metric_date": ("metric_date", "update_date", "date", "数据日期"),
    "campaign_id": ("campaign_id", "广告活动id"),
    "impressions": ("impressions", "展示", "展示次数"),
    "clicks": ("clicks", "点击", "点击次数"),
    "add_to_cart": ("add_to_cart", "cart", "加购", "加购次数"),
    "sold_qty": ("sold_qty", "orders", "订单", "广告订单"),
    "ad_spend": ("ad_spend", "spend", "广告费用"),
    "ad_sales": ("ad_sales", "sales", "广告销售额"),
    "target_drr": ("target_drr", "目标drr", "目标费用率"),
    "avg_cpc": ("avg_cpc", "cpc", "平均cpc"),
    "review_count": ("review_count", "reviews", "评论数"),
    "rating": ("rating", "评分"),
    "stock": ("stock", "库存"),
    "fulfillment_type": ("fulfillment_type", "履约模式"),
    "delivery_days": ("delivery_days", "配送天数"),
    "break_even_drr": ("break_even_drr", "盈亏平衡drr", "盈亏平衡费用率"),
    "image_version": ("image_version", "主图版本"),
    "price_version": ("price_version", "价格版本"),
    "outcome_label": ("outcome_label", "结果标签"),
}


def _field(row: dict[str, str], key: str) -> str:
    normalized = {str(name).strip().lower(): (value or "").strip() for name, value in row.items() if name is not None}
    return next((normalized[name] for name in ALIASES[key] if normalized.get(name) not in (None, "")), "")


def _number(value: str, *, integer: bool = False) -> int | float | None:
    if not value:
        return None
    clean = value.replace(" ", "").replace(",", ".").replace("₽", "")
    parsed = float(clean)
    return int(parsed) if integer else parsed


def _ratio(value: str) -> float | None:
    if not value:
        return None
    percent = "%" in value
    parsed = float(value.replace("%", "").replace(",", ".").strip())
    return parsed / 100 if percent or parsed > 1 else parsed


def _date(value: str) -> date | None:
    if not value:
        return None
    clean = value.strip().replace("/", "-").split("T", 1)[0].split(" ", 1)[0]
    return date.fromisoformat(clean)


def _datetime(value: str) -> datetime | None:
    parsed = _date(value)
    return datetime.combine(parsed, datetime.min.time(), tzinfo=timezone.utc) if parsed else None


def _performance_datetime(value: str) -> datetime | None:
    if not value:
        return None
    parsed = datetime.strptime(value, "%d.%m.%Y").date()
    return datetime.combine(parsed, datetime.min.time(), tzinfo=timezone.utc)


def _decode_csv(content: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ConflictError("无法识别 CSV 编码，请另存为 UTF-8 CSV 后重试")


def _store_or_404(session: Session, store_id: str) -> Store:
    store = session.get(Store, store_id)
    if not store or store.platform != "ozon":
        raise NotFoundError("OZON 店铺不存在")
    return store


def _state_dict(state: AdSkuState, sku: AdSku) -> dict:
    metrics = loads(state.metrics_json, {})
    metadata = loads(sku.metadata_json, {})
    return {
        "id": state.id,
        "sku_record_id": sku.id,
        "sku_id": sku.sku_id,
        "product_id": sku.product_id,
        "image_url": str(metadata.get("image_url") or ""),
        "name": sku.name,
        "category_path": sku.category_path,
        "price": sku.price,
        "target_drr": sku.target_drr,
        "break_even_drr": sku.break_even_drr,
        "outcome_label": sku.outcome_label,
        "weak_label": sku.weak_label,
        "data_maturity": sku.data_maturity,
        "state": state.state,
        "state_label": STATE_LABELS.get(state.state, state.state),
        "confidence": state.confidence,
        "evidence_strength": metrics.get("evidence_strength", "LOW"),
        "primary_state": metrics.get("primary_state", state.state),
        "secondary_flags": metrics.get("secondary_flags", []),
        "candidate_action": metrics.get("candidate_action", state.recommendation),
        "action_scope": metrics.get("action_scope", "SKU_DIAGNOSIS"),
        "campaign_sku_actions": metrics.get("campaign_sku_actions", []),
        "multi_campaign_conflict": metrics.get("multi_campaign_conflict", False),
        "action_risk": metrics.get("action_risk", "LOW"),
        "auto_eligible": False,
        "review_required": True,
        "decision_tier": metrics.get("decision_tier", "STANDARD"),
        "review_owner": metrics.get("review_owner", "POLICY_ENGINE"),
        "ready_for_shadow_action": metrics.get("ready_for_shadow_action", False),
        "ready_for_execution": metrics.get("ready_for_execution", False),
        "stop_evidence_count": metrics.get("stop_evidence_count", 0),
        "stop_evidence_flags": metrics.get("stop_evidence_flags", []),
        "budget_limited": metrics.get("budget_limited", False),
        "risk_flags": metrics.get("risk_flags", []),
        "safety_gate": metrics.get("safety_gate", {}),
        "recommendation": state.recommendation,
        "recommendation_label": RECOMMENDATION_LABELS.get(state.recommendation, state.recommendation),
        "reason": state.reason,
        "cohort_key": state.cohort_key,
        "cohort_level": state.cohort_level,
        "cohort_sample_size": state.cohort_sample_size,
        "metrics": metrics,
        "evidence": loads(state.evidence_json, []),
        "financial_check": loads(state.financial_check_json, {}),
        "updated_at": state.updated_at.isoformat(),
    }


def _backtest_dict(item: AdBacktestRun | None) -> dict | None:
    if not item:
        return None
    return {
        "id": item.id,
        "threshold_version": item.threshold_version,
        "sample_size": item.sample_size,
        "false_kill_count": item.false_kill_count,
        "false_keep_count": item.false_keep_count,
        "potential_savings": item.potential_savings,
        "summary": loads(item.summary_json, {}),
        "created_at": item.created_at.isoformat(),
    }


def _sku_financial_context(sku: AdSku, financial_policy: dict) -> dict:
    return assess_financials(
        None,
        exact_break_even_drr=sku.break_even_drr,
        category_break_even_drr=category_break_even_drr(
            financial_policy,
            category_id=sku.category_id,
            category_path=sku.category_path,
        ),
        policy=financial_policy,
    )


def _seller_order_window(sku: AdSku, period: str, date_from: date, date_to: date) -> tuple[dict, bool, str]:
    metadata = loads(sku.metadata_json, {})
    windows = metadata.get("seller_analytics_windows") if isinstance(metadata, dict) else {}
    window = windows.get(period) if isinstance(windows, dict) else None
    if not isinstance(window, dict):
        return {}, False, ""
    matches_range = window.get("date_from") == date_from.isoformat() and window.get("date_to") == date_to.isoformat()
    metrics = window.get("metrics")
    return (metrics if isinstance(metrics, dict) else {}), matches_range, str(metadata.get("seller_synced_at") or "")


def _metric_value(metrics: dict, key: str) -> float:
    try:
        return float(metrics.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _order_profile(total_orders: float, ad_orders: float, organic_orders: float, attribution_gap: float) -> str:
    if attribution_gap > 0:
        return "ATTRIBUTION_MISMATCH"
    if total_orders <= 0 and ad_orders <= 0:
        return "NO_ORDERS"
    if ad_orders <= 0 and organic_orders > 0:
        return "ORGANIC_ONLY"
    if organic_orders <= 0 and ad_orders > 0:
        return "AD_ONLY"
    ad_share = safe_div(ad_orders, total_orders) or 0
    if ad_share >= 0.7:
        return "AD_DEPENDENT"
    if ad_share <= 0.3:
        return "ORGANIC_DOMINANT"
    return "MIXED"


@router.get("")
def overview(
    store_id: str = "",
    search: str = "",
    state: str = "",
    candidate_action: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=200),
    session: Session = Depends(get_db),
) -> dict:
    stores = session.scalars(select(Store).where(Store.platform == "ozon", Store.enabled.is_(True)).order_by(Store.name)).all()
    chosen = store_id or (stores[0].id if stores else "")
    store_options = [{"id": item.id, "name": item.name} for item in stores]
    if not chosen:
        return {"mode": "live_reviewed", "stores": store_options, "store_id": "", "summary": {}, "items": [], "pagination": {"page": 1, "page_size": page_size, "total": 0}, "thresholds": [], "latest_backtest": None}
    _store_or_404(session, chosen)
    conditions = [AdSku.store_id == chosen]
    if search:
        pattern = f"%{search.strip()}%"
        conditions.append((AdSku.sku_id.ilike(pattern)) | (AdSku.name.ilike(pattern)) | (AdSku.category_path.ilike(pattern)))
    base_query = select(AdSkuState, AdSku).join(AdSku, AdSku.id == AdSkuState.ad_sku_id).where(*conditions)
    filtered_items = [_state_dict(row[0], row[1]) for row in session.execute(base_query).all()]
    executed_stop_sku_ids = set(session.scalars(
        select(AdAction.ad_sku_id).where(AdAction.action == "STOP_ADS_CANDIDATE", AdAction.status == "executed")
    ).all())
    for item in filtered_items:
        item["execution_status"] = (
            "STOPPED_EXECUTED"
            if item["sku_record_id"] in executed_stop_sku_ids and not item["campaign_sku_actions"]
            else ""
        )
    if state == "STOPPED_EXECUTED":
        filtered_items = [item for item in filtered_items if item["execution_status"] == "STOPPED_EXECUTED"]
    else:
        filtered_items = [item for item in filtered_items if item["execution_status"] != "STOPPED_EXECUTED"]
    if state and state != "STOPPED_EXECUTED":
        filtered_items = [
            item for item in filtered_items
            if state in {
                item["state"], item["primary_state"], item["candidate_action"],
                item["decision_tier"], item["review_owner"],
                *item["secondary_flags"], *item["stop_evidence_flags"],
            }
        ]
    if candidate_action:
        filtered_items = [item for item in filtered_items if item["candidate_action"] == candidate_action]
    action_priority = {
        "STOP_ADS_CANDIDATE": 0,
        "REDUCE_TARGET_DRR": 1,
        "INCREASE_BUDGET_CANDIDATE": 2,
        "INCREASE_TARGET_DRR_CANDIDATE": 2,
        "DIAGNOSE_NO_EXPOSURE": 3,
        "OPTIMIZE_LISTING": 4,
        "OPTIMIZE_CREATIVE": 4,
        "CHECK_OFFER": 4,
        "OBSERVE": 5,
        "HOLD": 6,
    }
    filtered_items.sort(key=lambda item: (
        action_priority.get(item["candidate_action"], 7),
        0 if item["ready_for_shadow_action"] else 1,
        -float(item["metrics"].get("ad_spend") or 0),
        item["sku_id"],
    ))
    filtered_total = len(filtered_items)
    items = filtered_items[(page - 1) * page_size:page * page_size]
    all_states = session.execute(
        select(AdSkuState, AdSku).join(AdSku, AdSku.id == AdSkuState.ad_sku_id).where(AdSku.store_id == chosen)
    ).all()
    all_items = [_state_dict(row[0], row[1]) for row in all_states]
    stopped_history_count = sum(
        1 for item in all_items
        if item["sku_record_id"] in executed_stop_sku_ids and not item["campaign_sku_actions"]
    )
    all_items = [
        item for item in all_items
        if not (item["sku_record_id"] in executed_stop_sku_ids and not item["campaign_sku_actions"])
    ]
    thresholds = session.scalars(
        select(AdThresholdVersion).where(AdThresholdVersion.store_id == chosen, AdThresholdVersion.active.is_(True)).order_by(AdThresholdVersion.success_sample_size.desc()).limit(20)
    ).all()
    latest_backtest = session.scalar(select(AdBacktestRun).where(AdBacktestRun.store_id == chosen).order_by(AdBacktestRun.created_at.desc()))
    total_spend = sum(float(item["metrics"].get("ad_spend") or 0) for item in all_items)
    backtest_summary = loads(latest_backtest.summary_json, {}) if latest_backtest else {}
    replay_strategies = backtest_summary.get("strategies") if isinstance(backtest_summary.get("strategies"), list) else []
    replay_savings = max((float(item.get("future_spend_saved") or 0) for item in replay_strategies), default=0.0)
    return {
        "mode": "live_reviewed",
        "stores": store_options,
        "store_id": chosen,
        "summary": {
            "sku_count": len(all_items),
            "stopped_history_count": stopped_history_count,
            "observation_count": sum(1 for item in all_items if item["state"] in {"NEW", "COLD_START", "OBSERVATION"}),
            "optimization_count": sum(1 for item in all_items if item["candidate_action"] not in {"OBSERVE", "HOLD"}),
            "stop_count": sum(1 for item in all_items if item["candidate_action"] == "STOP_ADS_CANDIDATE"),
            "system_stop_count": sum(1 for item in all_items if item["candidate_action"] == "STOP_ADS_CANDIDATE" and item["decision_tier"] == "SYSTEM_HIGH_CONFIDENCE" and item["ready_for_shadow_action"]),
            "executable_stop_count": sum(1 for item in all_items if item["candidate_action"] == "STOP_ADS_CANDIDATE" and item["ready_for_execution"] and not item["multi_campaign_conflict"] and len(item["campaign_sku_actions"]) == 1),
            "codex_review_count": sum(1 for item in all_items if item["review_owner"] == "CODEX" and item["ready_for_shadow_action"]),
            "scale_count": sum(1 for item in all_items if item["candidate_action"] in {"INCREASE_TARGET_DRR_CANDIDATE", "INCREASE_BUDGET_CANDIDATE"}),
            "executable_scale_count": sum(1 for item in all_items if item["candidate_action"] == "INCREASE_TARGET_DRR_CANDIDATE" and item["ready_for_execution"] and not item["multi_campaign_conflict"] and len(item["campaign_sku_actions"]) == 1),
            "no_exposure_count": sum(1 for item in all_items if item["candidate_action"] == "DIAGNOSE_NO_EXPOSURE"),
            "potential_savings": round(replay_savings, 2),
            "potential_savings_source": "LATEST_CHRONOLOGICAL_REPLAY_MAX" if latest_backtest else "NO_BACKTEST",
            "window_spend": round(total_spend, 2),
        },
        "items": items,
        "pagination": {"page": page, "page_size": page_size, "total": filtered_total},
        "thresholds": [{
            "id": item.id,
            "version": item.version,
            "cohort_key": item.cohort_key,
            "cohort_level": item.cohort_level,
            "sample_size": item.sample_size,
            "success_sample_size": item.success_sample_size,
            "window_days": item.window_days,
            "values": loads(item.thresholds_json, {}),
        } for item in thresholds],
        "latest_backtest": _backtest_dict(latest_backtest),
    }


@router.get("/dashboard")
def dashboard(
    store_id: str = "",
    period: str = Query("7d", pattern="^(7d|30d|quarter)$"),
    session: Session = Depends(get_db),
) -> dict:
    stores = session.scalars(select(Store).where(Store.platform == "ozon", Store.enabled.is_(True)).order_by(Store.name)).all()
    chosen = store_id or (stores[0].id if stores else "")
    options = [{"id": item.id, "name": item.name} for item in stores]
    if not chosen:
        return {"stores": options, "store_id": "", "period": period, "totals": {}, "trend": [], "funnel": {}, "campaigns": [], "order_overview": {}, "sku_orders": [], "profitability": {}}
    _store_or_404(session, chosen)
    date_to = date.today() - timedelta(days=1)
    if period == "quarter":
        date_from = date(date_to.year, ((date_to.month - 1) // 3) * 3 + 1, 1)
    else:
        date_from = date_to - timedelta(days=(7 if period == "7d" else 30) - 1)
    rows = session.scalars(select(AdCampaignDailyMetric).where(
        AdCampaignDailyMetric.store_id == chosen,
        AdCampaignDailyMetric.metric_date >= date_from,
        AdCampaignDailyMetric.metric_date <= date_to,
    )).all()

    totals = {field: sum(getattr(row, field) for row in rows) for field in ("impressions", "clicks", "add_to_cart", "sold_qty", "ad_spend", "ad_sales")}
    totals.update({
        "ctr": safe_div(totals["clicks"], totals["impressions"]),
        "cpc": safe_div(totals["ad_spend"], totals["clicks"]),
        "roas": safe_div(totals["ad_sales"], totals["ad_spend"]),
        "acos": safe_div(totals["ad_spend"], totals["ad_sales"]),
        "cvr": safe_div(totals["sold_qty"], totals["clicks"]),
        "cpa": safe_div(totals["ad_spend"], totals["sold_qty"]),
    })
    by_date: dict[date, dict] = defaultdict(lambda: {"ad_spend": 0.0, "ad_sales": 0.0, "orders": 0})
    campaign_configs = {
        item.campaign_id: item
        for item in session.scalars(select(AdCampaign).where(AdCampaign.store_id == chosen)).all()
    }
    by_campaign: dict[str, dict] = defaultdict(lambda: {"name": "", "impressions": 0, "clicks": 0, "orders": 0, "ad_spend": 0.0, "ad_sales": 0.0})
    by_sku: dict[str, dict] = defaultdict(lambda: {"impressions": 0, "clicks": 0, "ad_orders": 0, "ad_spend": 0.0, "ad_sales": 0.0})
    for row in rows:
        day = by_date[row.metric_date]
        day["ad_spend"] += row.ad_spend
        day["ad_sales"] += row.ad_sales
        day["orders"] += row.sold_qty
        campaign = by_campaign[row.campaign_id]
        campaign["name"] = row.campaign_name or campaign["name"]
        for field in ("impressions", "clicks"):
            campaign[field] += getattr(row, field)
        campaign["orders"] += row.sold_qty
        campaign["ad_spend"] += row.ad_spend
        campaign["ad_sales"] += row.ad_sales
        sku_ad = by_sku[row.sku_id]
        sku_ad["impressions"] += row.impressions
        sku_ad["clicks"] += row.clicks
        sku_ad["ad_orders"] += row.sold_qty
        sku_ad["ad_spend"] += row.ad_spend
        sku_ad["ad_sales"] += row.ad_sales
    trend = [{"date": current.isoformat(), **by_date.get(current, {"ad_spend": 0, "ad_sales": 0, "orders": 0})} for current in (date_from + timedelta(days=index) for index in range((date_to - date_from).days + 1))]
    campaign_items = []
    for campaign_id, item in by_campaign.items():
        config = campaign_configs.get(campaign_id)
        campaign_items.append({
            "campaign_id": campaign_id,
            **item,
            "name": item["name"] or (config.name if config else ""),
            "state": config.state if config else "",
            "budget": config.budget if config else None,
            "daily_budget": config.daily_budget if config else None,
            "weekly_budget": config.weekly_budget if config else None,
            "ctr": safe_div(item["clicks"], item["impressions"]),
            "roas": safe_div(item["ad_sales"], item["ad_spend"]),
            "acos": safe_div(item["ad_spend"], item["ad_sales"]),
        })
    campaign_items.sort(key=lambda item: item["ad_spend"], reverse=True)
    skus = session.scalars(select(AdSku).where(AdSku.store_id == chosen)).all()
    financial_policy = flatten_financial_policy(system_defaults(session).get("financial_policy"))
    financial_contexts = [_sku_financial_context(sku, financial_policy) for sku in skus]
    exact_count = sum(1 for item in financial_contexts if item["source"] == "SKU_EXACT")
    category_count = sum(1 for item in financial_contexts if item["source"] == "CATEGORY_DEFAULT")
    store_default_count = sum(1 for item in financial_contexts if item["source"] == "STORE_DEFAULT")
    financial_sources = sorted({str(item["source"]) for item in financial_contexts})
    effective_break_even = [float(item["effective_break_even_drr"]) for item in financial_contexts]
    max_cpas = [
        sku.price * float(context["effective_break_even_drr"])
        for sku, context in zip(skus, financial_contexts)
        if sku.price
    ]
    sku_orders = []
    seller_coverage = 0
    seller_synced_at = ""
    seller_totals = {field: 0.0 for field in ("ordered_units", "revenue", "cancellations", "returns", "delivered_units")}
    covered_ad_totals = {"orders": 0.0, "sales": 0.0, "spend": 0.0}
    for sku in skus:
        seller_metrics, seller_available, synced_at = _seller_order_window(sku, period, date_from, date_to)
        ad = by_sku.get(sku.sku_id, {})
        if seller_available:
            seller_coverage += 1
            seller_synced_at = max(seller_synced_at, synced_at)
            for field in seller_totals:
                seller_totals[field] += _metric_value(seller_metrics, field)
            covered_ad_totals["orders"] += float(ad.get("ad_orders") or 0)
            covered_ad_totals["sales"] += float(ad.get("ad_sales") or 0)
            covered_ad_totals["spend"] += float(ad.get("ad_spend") or 0)
        ad_orders = float(ad.get("ad_orders") or 0)
        ad_sales = float(ad.get("ad_sales") or 0)
        total_orders = _metric_value(seller_metrics, "ordered_units") if seller_available else None
        total_sales = _metric_value(seller_metrics, "revenue") if seller_available else None
        organic_orders = max(total_orders - ad_orders, 0.0) if total_orders is not None else None
        organic_sales = max(total_sales - ad_sales, 0.0) if total_sales is not None else None
        attribution_gap = max(ad_orders - total_orders, 0.0) if total_orders is not None else 0.0
        profile = _order_profile(total_orders or 0.0, ad_orders, organic_orders or 0.0, attribution_gap) if seller_available else "SELLER_DATA_MISSING"
        sku_orders.append({
            "sku_id": sku.sku_id,
            "name": sku.name,
            "seller_data_available": seller_available,
            "total_orders": total_orders,
            "ad_orders": ad_orders,
            "estimated_organic_orders": organic_orders,
            "total_sales": total_sales,
            "ad_sales": ad_sales,
            "estimated_organic_sales": organic_sales,
            "cancellations": _metric_value(seller_metrics, "cancellations") if seller_available else None,
            "returns": _metric_value(seller_metrics, "returns") if seller_available else None,
            "delivered_units": _metric_value(seller_metrics, "delivered_units") if seller_available else None,
            "ad_order_share": safe_div(ad_orders, total_orders or 0) if seller_available else None,
            "ad_spend": float(ad.get("ad_spend") or 0),
            "ad_drr": safe_div(float(ad.get("ad_spend") or 0), ad_sales),
            "ad_spend_to_total_sales": safe_div(float(ad.get("ad_spend") or 0), total_sales or 0) if seller_available else None,
            "impressions": int(ad.get("impressions") or 0),
            "clicks": int(ad.get("clicks") or 0),
            "attribution_gap": attribution_gap,
            "order_profile": profile,
        })
    sku_orders.sort(key=lambda item: (item["total_orders"] or 0, item["ad_spend"], item["ad_orders"]), reverse=True)
    seller_available = seller_coverage > 0
    seller_complete = bool(skus) and seller_coverage == len(skus)
    total_orders = seller_totals["ordered_units"] if seller_coverage else None
    total_sales = seller_totals["revenue"] if seller_coverage else None
    estimated_organic_orders = max((total_orders or 0) - covered_ad_totals["orders"], 0.0) if total_orders is not None else None
    estimated_organic_sales = max((total_sales or 0) - covered_ad_totals["sales"], 0.0) if total_sales is not None else None
    return {
        "stores": options,
        "store_id": chosen,
        "period": period,
        "date_from": date_from,
        "date_to": date_to,
        "totals": totals,
        "trend": trend,
        "funnel": {"impressions": totals["impressions"], "clicks": totals["clicks"], "orders": totals["sold_qty"], "click_rate": totals["ctr"], "order_rate": totals["cvr"]},
        "campaigns": campaign_items[:50],
        "campaign_count": len(campaign_items),
        "order_overview": {
            "available": seller_available,
            "complete": seller_complete,
            "coverage_count": seller_coverage,
            "sku_count": len(skus),
            "seller_synced_at": seller_synced_at,
            "total_orders": total_orders,
            "ad_orders": covered_ad_totals["orders"],
            "estimated_organic_orders": estimated_organic_orders,
            "total_sales": total_sales,
            "ad_sales": covered_ad_totals["sales"],
            "estimated_organic_sales": estimated_organic_sales,
            "cancellations": seller_totals["cancellations"] if seller_coverage else None,
            "returns": seller_totals["returns"] if seller_coverage else None,
            "delivered_units": seller_totals["delivered_units"] if seller_coverage else None,
            "ad_order_share": safe_div(covered_ad_totals["orders"], total_orders or 0) if total_orders is not None else None,
            "ad_spend_to_total_sales": safe_div(covered_ad_totals["spend"], total_sales or 0) if total_sales is not None else None,
            "attribution_mismatch_count": sum(1 for item in sku_orders if item["attribution_gap"] > 0),
        },
        "sku_orders": sku_orders,
        "profitability": {
            "sku_count": len(skus),
            "configured_count": exact_count + category_count,
            "exact_count": exact_count,
            "category_default_count": category_count,
            "store_default_count": store_default_count,
            "coverage": 1.0 if skus else None,
            "planned_ad_drr": financial_policy["planned_ad_drr"],
            "minimum_profit_margin_after_planned_ads": financial_policy["minimum_profit_margin_after_planned_ads"],
            "estimated_pre_ad_contribution_margin": financial_policy["estimated_pre_ad_contribution_margin"],
            "estimated_break_even_drr": financial_policy["estimated_break_even_drr"],
            "financial_confidence": "HIGH" if financial_contexts and exact_count == len(financial_contexts) else "MEDIUM",
            "financial_model_source": financial_sources[0] if len(financial_sources) == 1 else "MIXED",
            "average_break_even_acos": safe_div(sum(effective_break_even), len(effective_break_even)),
            "average_break_even_roas": safe_div(len(effective_break_even), sum(effective_break_even)),
            "average_max_cpa": safe_div(sum(max_cpas), len(max_cpas)),
        },
    }


@router.post("/imports/csv", status_code=201)
async def import_csv(
    store_id: str = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(get_db),
) -> dict:
    _store_or_404(session, store_id)
    content = await file.read()
    if len(content) > 15 * 1024 * 1024:
        raise ConflictError("CSV 文件不能超过 15 MB")
    reader = csv.DictReader(io.StringIO(_decode_csv(content)))
    if not reader.fieldnames:
        raise ConflictError("CSV 缺少表头")
    financial_policy = flatten_financial_policy(system_defaults(session).get("financial_policy"))
    imported = 0
    skipped = 0
    errors: list[str] = []
    for line_number, row in enumerate(reader, start=2):
        try:
            sku_id = _field(row, "sku_id")
            metric_date = _date(_field(row, "metric_date"))
            if not sku_id or not metric_date:
                raise ValueError("sku_id 和 metric_date 不能为空")
            sku = session.scalar(select(AdSku).where(AdSku.store_id == store_id, AdSku.sku_id == sku_id))
            if not sku:
                sku = AdSku(store_id=store_id, sku_id=sku_id)
                session.add(sku)
                session.flush()
            for key in ("product_id", "name", "category_id", "category_path", "fulfillment_type", "image_version", "price_version"):
                value = _field(row, key)
                if value:
                    setattr(sku, key, value)
            for key, integer in (("price", False), ("review_count", True), ("rating", False), ("stock", True), ("delivery_days", False)):
                value = _number(_field(row, key), integer=integer)
                if value is not None:
                    setattr(sku, key, value)
            for key in ("live_date", "ad_start_date"):
                value = _datetime(_field(row, key))
                if value:
                    setattr(sku, key, value)
            for key in ("target_drr", "break_even_drr"):
                value = _ratio(_field(row, key))
                if value is not None:
                    setattr(sku, key, value)
            label = _field(row, "outcome_label").upper()
            if label:
                if label not in {"SUCCESS", "WEAK", "FAILED", "NO_TRAFFIC", "STOPPED_EXTERNALLY"}:
                    raise ValueError("outcome_label 不在允许范围内")
                sku.outcome_label = label

            metric = session.scalar(select(AdDailyMetric).where(AdDailyMetric.ad_sku_id == sku.id, AdDailyMetric.metric_date == metric_date))
            if not metric:
                metric = AdDailyMetric(ad_sku_id=sku.id, metric_date=metric_date)
                session.add(metric)
            metric.campaign_id = _field(row, "campaign_id")
            metric.impressions = int(_number(_field(row, "impressions"), integer=True) or 0)
            metric.clicks = int(_number(_field(row, "clicks"), integer=True) or 0)
            metric.add_to_cart = int(_number(_field(row, "add_to_cart"), integer=True) or 0)
            metric.sold_qty = int(_number(_field(row, "sold_qty"), integer=True) or 0)
            metric.ad_spend = float(_number(_field(row, "ad_spend")) or 0)
            metric.ad_sales = float(_number(_field(row, "ad_sales")) or 0)
            metric.target_drr = _ratio(_field(row, "target_drr")) if _field(row, "target_drr") else sku.target_drr
            metric.avg_cpc = _number(_field(row, "avg_cpc")) or safe_div(metric.ad_spend, metric.clicks)
            metric.ctr = safe_div(metric.clicks, metric.impressions)
            metric.atc_rate = safe_div(metric.add_to_cart, metric.clicks)
            metric.cvr = safe_div(metric.sold_qty, metric.clicks)
            metric.cart_cvr = safe_div(metric.sold_qty, metric.add_to_cart)
            metric.cpa = safe_div(metric.ad_spend, metric.sold_qty)
            metric.actual_drr = safe_div(metric.ad_spend, metric.ad_sales)
            break_even_drr = float(_sku_financial_context(sku, financial_policy)["effective_break_even_drr"])
            metric.break_even_cvr = safe_div(metric.avg_cpc, (sku.price or 0) * break_even_drr) if metric.avg_cpc is not None else None
            metric.raw_json = dumps(row)
            imported += 1
        except (TypeError, ValueError) as error:
            skipped += 1
            if len(errors) < 20:
                errors.append(f"第 {line_number} 行：{error}")
    if imported == 0:
        session.rollback()
        raise ConflictError("没有可导入的数据", details={"errors": errors})
    session.flush()
    labels = assign_weak_labels_and_maturity(session, store_id)
    analysis = analyze_store(session, store_id)
    return {"imported": imported, "skipped": skipped, "errors": errors, "labels": labels, "analysis": analysis}


@router.post("/imports/csv/background", status_code=202)
async def queue_csv_import(
    store_id: str = Form(...), file: UploadFile = File(...), session: Session = Depends(get_db),
) -> dict:
    _store_or_404(session, store_id)
    content = await file.read(15 * 1024 * 1024 + 1)
    if len(content) > 15 * 1024 * 1024:
        raise ConflictError("CSV 文件不能超过 15 MB")
    if not next(csv.reader(io.StringIO(_decode_csv(content))), None):
        raise ConflictError("CSV 缺少表头")
    upload_id = uuid4().hex
    directory = get_settings().data_dir / "ad_imports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{upload_id}.csv"
    path.write_bytes(content)
    try:
        job = enqueue_job(
            session, job_type="ad_optimization_import",
            payload={"store_id": store_id, "upload_id": upload_id},
            config_snapshot=vault.encrypt({}),
            idempotency_key=f"ad_optimization_import:{store_id}:{upload_id}", max_attempts=1,
            steps=[("import", "导入历史广告数据并分析")],
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return {"job_id": job.id, "status": job.status}


@router.post("/analyze")
def analyze(store_id: str = Query(...), session: Session = Depends(get_db)) -> dict:
    _store_or_404(session, store_id)
    try:
        labels = assign_weak_labels_and_maturity(session, store_id)
        return {**analyze_store(session, store_id), "labels": labels}
    except ValueError as error:
        raise ConflictError(str(error)) from error


@router.post("/sync", status_code=201)
def sync_from_ozon(
    store_id: str = Query(...),
    days: int = Query(62, ge=1, le=62),
    checkpoint_job_id: str = "",
    session: Session = Depends(get_db),
) -> dict:
    if checkpoint_job_id and (len(checkpoint_job_id) != 32 or any(character not in "0123456789abcdef" for character in checkpoint_job_id)):
        raise AppError("广告同步任务编号无效", code="ad_sync_checkpoint_invalid", status_code=422)
    store = _store_or_404(session, store_id)
    secrets = vault.decrypt(store.secrets_encrypted)
    client = OzonPerformanceClient(
        str(secrets.get("performance_client_id") or ""),
        str(secrets.get("performance_client_secret") or ""),
    )
    campaigns = client.campaigns()
    campaign_ids = [str(item.get("id")) for item in campaigns if item.get("id")]
    campaign_names = {
        str(item.get("id")): str(item.get("title") or item.get("name") or item.get("campaignName") or "")
        for item in campaigns if item.get("id")
    }
    if not campaign_ids:
        raise ConflictError("该店铺没有可同步的 Ozon 广告活动")
    date_to = date.today() - timedelta(days=1)
    date_from = date_to - timedelta(days=days - 1)
    campaign_rows: list[dict] = []
    files = 0
    report_files = client.report_files(
        campaign_ids, date_from, date_to,
        **({"checkpoint_dir": get_settings().data_dir / "ad_report_checkpoints" / checkpoint_job_id} if checkpoint_job_id else {}),
    )
    for _, content in report_files:
        files += 1
        for row in client.parse_report(content):
            campaign_rows.append(row)
    product_snapshots: dict[str, list[dict] | AppError] = {}
    for campaign_id in campaign_ids:
        try:
            product_snapshots[campaign_id] = client.campaign_products(campaign_id)
        except AppError as error:
            product_snapshots[campaign_id] = error
    seller_sync = sync_seller_catalog(session, store, days=days)
    campaign_sync = sync_campaign_configuration(session, store, client, campaigns, product_snapshots=product_snapshots)
    financial_policy = flatten_financial_policy(system_defaults(session).get("financial_policy"))
    # Replace only campaigns still returned by Ozon. Metrics from stopped or archived
    # campaigns must remain available for historical analysis and backtests.
    session.execute(delete(AdCampaignDailyMetric).where(
        AdCampaignDailyMetric.store_id == store_id,
        AdCampaignDailyMetric.campaign_id.in_(campaign_ids),
        AdCampaignDailyMetric.metric_date >= date_from,
        AdCampaignDailyMetric.metric_date <= date_to,
    ))

    for row in campaign_rows:
        campaign_metric = AdCampaignDailyMetric(
            store_id=store_id, campaign_id=row["campaign_id"], sku_id=row["sku_id"], metric_date=row["metric_date"]
        )
        session.add(campaign_metric)
        campaign_metric.campaign_name = campaign_names.get(row["campaign_id"], "")
        for field in ("impressions", "clicks", "add_to_cart", "sold_qty"):
            setattr(campaign_metric, field, int(row.get(field) or 0))
        for field in ("ad_spend", "ad_sales"):
            setattr(campaign_metric, field, float(row.get(field) or 0))

    session.flush()
    aggregated: dict[tuple[str, date], dict] = {}
    stored_campaign_rows = session.scalars(select(AdCampaignDailyMetric).where(
        AdCampaignDailyMetric.store_id == store_id,
        AdCampaignDailyMetric.metric_date >= date_from,
        AdCampaignDailyMetric.metric_date <= date_to,
    )).all()
    for stored in stored_campaign_rows:
        key = (stored.sku_id, stored.metric_date)
        current = aggregated.setdefault(key, {
            "sku_id": stored.sku_id,
            "metric_date": stored.metric_date,
            "campaign_ids": [],
            "impressions": 0,
            "clicks": 0,
            "add_to_cart": 0,
            "sold_qty": 0,
            "ad_spend": 0.0,
            "ad_sales": 0.0,
        })
        current["campaign_ids"].append(stored.campaign_id)
        for field in ("impressions", "clicks", "add_to_cart", "sold_qty"):
            current[field] += int(getattr(stored, field) or 0)
        for field in ("ad_spend", "ad_sales"):
            current[field] += float(getattr(stored, field) or 0)

    imported = 0
    source_rows = {(item["sku_id"], item["metric_date"]): item for item in campaign_rows}
    for (sku_id, metric_date), row in aggregated.items():
        sku = session.scalar(select(AdSku).where(AdSku.store_id == store_id, AdSku.sku_id == sku_id))
        if not sku:
            sku = AdSku(store_id=store_id, sku_id=sku_id)
            session.add(sku)
            session.flush()
        source_row = source_rows.get((sku_id, metric_date), {})
        sku.name = source_row.get("name") or sku.name
        sku.price = source_row.get("price") or sku.price
        observed_ad_start = _performance_datetime(source_row.get("ad_start_date", ""))
        if observed_ad_start and (sku.ad_start_date is None or observed_ad_start.date() < sku.ad_start_date.date()):
            sku.ad_start_date = observed_ad_start
        metric = session.scalar(select(AdDailyMetric).where(AdDailyMetric.ad_sku_id == sku.id, AdDailyMetric.metric_date == metric_date))
        if not metric:
            metric = AdDailyMetric(ad_sku_id=sku.id, metric_date=metric_date)
            session.add(metric)
        metric.campaign_id = ",".join(dict.fromkeys(row.get("campaign_ids") or []))
        metric.impressions = int(row.get("impressions") or 0)
        metric.clicks = int(row.get("clicks") or 0)
        metric.add_to_cart = int(row.get("add_to_cart") or 0)
        metric.sold_qty = int(row.get("sold_qty") or 0)
        metric.ad_spend = float(row.get("ad_spend") or 0)
        metric.ad_sales = float(row.get("ad_sales") or 0)
        metric.avg_cpc = safe_div(metric.ad_spend, metric.clicks)
        metric.ctr = safe_div(metric.clicks, metric.impressions)
        metric.atc_rate = safe_div(metric.add_to_cart, metric.clicks)
        metric.cvr = safe_div(metric.sold_qty, metric.clicks)
        metric.cart_cvr = safe_div(metric.sold_qty, metric.add_to_cart)
        metric.cpa = safe_div(metric.ad_spend, metric.sold_qty)
        metric.actual_drr = safe_div(metric.ad_spend, metric.ad_sales)
        break_even_drr = float(_sku_financial_context(sku, financial_policy)["effective_break_even_drr"])
        metric.break_even_cvr = safe_div(metric.avg_cpc, (sku.price or 0) * break_even_drr) if metric.avg_cpc is not None else None
        metric.raw_json = dumps(row)
        imported += 1
    session.flush()
    labels = assign_weak_labels_and_maturity(session, store_id)
    analysis = analyze_store(session, store_id)
    return {
        "campaigns": len(campaign_ids),
        "report_files": files,
        "imported": imported,
        "date_from": date_from,
        "date_to": date_to,
        "seller_sync": seller_sync,
        "campaign_sync": campaign_sync,
        "labels": labels,
        "analysis": analysis,
        "mode": "live_reviewed",
        "executed": False,
    }


@router.post("/jobs/{operation}", status_code=202)
def queue_ad_operation(
    operation: Literal["sync", "analyze", "backtest", "reports"],
    store_id: str = Query(...),
    days: int = Query(62, ge=1, le=62),
    session: Session = Depends(get_db),
) -> dict:
    _store_or_404(session, store_id)
    from erp.core.task_identity import task_identity
    from erp.core.config import get_settings
    actor_id = (task_identity.get() or {}).get('id', '') if get_settings().multiuser_enabled else ''
    job_type = f"ad_optimization_{operation}"
    for existing in session.scalars(select(Job).where(Job.type == job_type, Job.status.in_(ACTIVE_STATUSES))).all():
        if loads(existing.payload_json, {}).get("store_id") == store_id and (not actor_id or existing.created_by == actor_id):
            return {"job_id": existing.id, "status": existing.status, "already_running": True}
    labels = {"sync": "同步 Ozon 广告与卖家数据", "analyze": "分析广告 SKU", "backtest": "逐日回测广告策略", "reports": "生成广告分析报告"}
    job = enqueue_job(
        session, job_type=job_type, payload={"store_id": store_id, "days": days},
        config_snapshot=vault.encrypt({}),
        idempotency_key=f"{job_type}:{store_id}:{uuid4().hex}", max_attempts=3 if operation in {"analyze", "backtest", "reports"} else 2,
        steps=[(operation, labels[operation])],
    )
    return {"job_id": job.id, "status": job.status, "already_running": False}


@router.get("/reports/{job_id}/download")
def download_personal_report(job_id: str, session: Session = Depends(get_db)) -> FileResponse:
    from erp.services.ad_report_result import archive_path, completion_summary
    import hashlib

    job = session.get(Job, job_id)
    if not job or job.type != 'ad_optimization_reports':
        raise NotFoundError('广告报告任务不存在')
    if job.status != 'completed':
        raise ConflictError('广告报告尚未完成')
    receipt = completion_summary(job)
    path = archive_path(job_id)
    if (not path.is_file() or path.stat().st_size != receipt['report_bytes']
            or hashlib.sha256(path.read_bytes()).hexdigest() != receipt['report_sha256']):
        raise ConflictError('广告报告文件校验失败')
    return FileResponse(path, media_type='application/zip', filename=f'Ozon-ad-report-{job_id}.zip')


@router.post("/backtests", status_code=201)
def backtest(store_id: str = Query(...), session: Session = Depends(get_db)) -> dict:
    _store_or_404(session, store_id)
    if not session.scalar(select(func.count()).select_from(AdSkuState).join(AdSku).where(AdSku.store_id == store_id)):
        raise ConflictError("请先导入数据并运行分析")
    return _backtest_dict(run_backtest(session, store_id)) or {}


@router.get("/analysis")
def analysis(store_id: str = Query(...), session: Session = Depends(get_db)) -> dict:
    _store_or_404(session, store_id)
    return build_ad_analysis(session, store_id)


@router.post("/reports", status_code=201)
def generate_reports(store_id: str = Query(...), session: Session = Depends(get_db)) -> dict:
    _store_or_404(session, store_id)
    result = write_ad_reports(session, store_id)
    analysis_result = result["analysis"]
    return {
        "report_dir": result["report_dir"],
        "date_from": analysis_result["audit"]["date_from"],
        "date_to": analysis_result["audit"]["date_to"],
        "advertised_sku_count": analysis_result["audit"]["advertised_sku_count"],
        "candidate_counts": analysis_result["candidate_counts"],
        "mode": "live_reviewed",
        "executed": False,
    }


@router.put("/skus/{sku_record_id}/outcome")
def update_outcome(sku_record_id: str, request: AdOutcomeUpdate, session: Session = Depends(get_db)) -> dict:
    sku = session.get(AdSku, sku_record_id)
    if not sku:
        raise NotFoundError("广告 SKU 不存在")
    sku.outcome_label = request.outcome_label
    session.commit()
    return {"id": sku.id, "outcome_label": sku.outcome_label}


@router.post("/actions", status_code=201)
def create_action(request: AdActionCreate, session: Session = Depends(get_db)) -> dict:
    state = session.get(AdSkuState, request.state_id)
    if not state:
        raise NotFoundError("SKU 分析状态不存在")
    latest_history = session.scalar(
        select(AdSkuStateHistory).where(AdSkuStateHistory.ad_sku_id == state.ad_sku_id).order_by(AdSkuStateHistory.created_at.desc())
    )
    sku = session.get(AdSku, state.ad_sku_id)
    metrics = loads(state.metrics_json, {})
    campaign_actions = metrics.get("campaign_sku_actions") if isinstance(metrics.get("campaign_sku_actions"), list) else []
    candidate_action = str(metrics.get("candidate_action") or state.recommendation)
    single_campaign_target = campaign_actions[0].get("current_target_drr") if len(campaign_actions) == 1 else None
    proposed_target = request.proposed_target_drr if len(campaign_actions) == 1 else None
    proposal = {
        "scope": "CAMPAIGN_SKU_ACTION" if campaign_actions else "SKU_DIAGNOSIS",
        "sku_id": sku.sku_id if sku else "",
        "candidate_action": candidate_action,
        "campaign_sku_actions": campaign_actions,
        "multi_campaign_conflict": bool(metrics.get("multi_campaign_conflict")),
        "decision_tier": metrics.get("decision_tier", "STANDARD"),
        "review_owner": metrics.get("review_owner", "POLICY_ENGINE"),
        "ready_for_shadow_action": bool(metrics.get("ready_for_shadow_action")),
        "requested_target_drr": request.proposed_target_drr,
        "stored_target_drr": proposed_target,
        "auto_eligible": False,
        "review_required": True,
        "executed": False,
    }
    action = AdAction(
        ad_sku_id=state.ad_sku_id,
        state_history_id=latest_history.id if latest_history else None,
        action=candidate_action,
        status="proposed",
        previous_target_drr=single_campaign_target,
        proposed_target_drr=proposed_target,
        reason=state.reason,
        before_metrics_json=dumps({"diagnosis": metrics, "proposal": proposal}),
    )
    session.add(action)
    session.commit()
    session.refresh(action)
    return {
        "id": action.id,
        "status": action.status,
        "action": action.action,
        "scope": proposal["scope"],
        "campaign_sku_actions": campaign_actions,
        "review_required": True,
        "mode": "live_reviewed",
        "executed": False,
    }


def _selected_execution_rows(session: Session, store_id: str, state_ids: list[str]) -> list[tuple[AdSkuState, AdSku, dict]]:
    return [
        (state, sku, loads(state.metrics_json, {}))
        for state, sku in session.execute(
            select(AdSkuState, AdSku)
            .join(AdSku, AdSku.id == AdSkuState.ad_sku_id)
            .where(AdSku.store_id == store_id, AdSkuState.id.in_(state_ids))
        ).all()
    ]


def _execution_candidates(session: Session, store_id: str, requested_action: str, state_ids: list[str]) -> list[tuple[AdSkuState, AdSku, dict, dict]]:
    expected = (
        {"STOP_ADS_CANDIDATE"}
        if requested_action == "STOP_ADS_CANDIDATE"
        else {"INCREASE_TARGET_DRR_CANDIDATE"}
    )
    candidates = []
    for state, sku, metrics in _selected_execution_rows(session, store_id, state_ids):
        candidate_action = str(metrics.get("candidate_action") or state.recommendation)
        actions = metrics.get("campaign_sku_actions") if isinstance(metrics.get("campaign_sku_actions"), list) else []
        if (
            candidate_action in expected
            and bool(metrics.get("ready_for_execution"))
            and not bool(metrics.get("multi_campaign_conflict"))
            and len(actions) == 1
        ):
            candidates.append((state, sku, metrics, actions[0]))
    return candidates


@router.post("/actions/preview")
def preview_batch(request: AdBatchExecutionRequest, http_request: Request, store_id: str = Query(...), session: Session = Depends(get_db)) -> dict:
    if get_settings().multiuser_enabled:
        from erp.models import AdExecutionBatch
        if session.scalar(select(AdExecutionBatch.id).where(AdExecutionBatch.active_store == store_id)):
            raise ConflictError('该店铺有正在执行或结果待核对的广告批次')
        if not _store_or_404(session, store_id).enabled:
            raise ConflictError('店铺已停用，不能生成广告执行预览')
        job = enqueue_job(
            session, job_type='ad_optimization_preview',
            payload={'store_id': store_id, 'action': request.action,
                     'state_ids': sorted(set(request.state_ids))},
            config_snapshot=vault.encrypt({}),
            idempotency_key=f'ad-preview:{store_id}:{uuid4().hex}',
            max_attempts=3, steps=[('preview', '在本人电脑分析并生成广告审批预览')])
        return {'job_id': job.id, 'status': job.status}
    from erp.services.ad_execution import preview
    return preview(session, store_id, request, http_request.state.operator)


@router.get('/actions/previews/{job_id}')
def personal_preview(job_id: str, session: Session = Depends(get_db)) -> dict:
    from erp.core.task_identity import task_identity
    from erp.models import AdExecutionBatch
    job = session.get(Job, job_id)
    actor_id = (task_identity.get() or {}).get('id', '')
    if not job or job.type != 'ad_optimization_preview' or (get_settings().multiuser_enabled and job.created_by != actor_id):
        raise NotFoundError('广告预览任务不存在')
    if job.status != 'completed':
        raise ConflictError('广告预览尚未完成')
    receipt = loads(job.external_ref, {})
    batch = session.get(AdExecutionBatch, receipt.get('approval_id'))
    if not batch or batch.store_id != loads(job.payload_json, {}).get('store_id') or batch.actor != job.created_by:
        raise ConflictError('广告审批批次与预览任务不一致')
    plan = loads(batch.preview_json, {})
    store = session.get(Store, batch.store_id)
    return {'approval_id': batch.id, 'store_name': store.name,
            'action': batch.action, 'expires_at': batch.expires_at,
            'items': plan['items'], 'blocked': plan['blocked'], 'mode': 'live_reviewed'}


@router.post("/actions/execute-batch")
def execute_batch(request: AdBatchExecutionRequest, http_request: Request, store_id: str = Query(...), session: Session = Depends(get_db)) -> dict:
    if get_settings().multiuser_enabled:
        from erp.core.task_identity import task_identity
        from erp.models import AdExecutionBatch
        from erp.services.ad_desktop_snapshot import analysis_source_fingerprint
        if not request.confirmed or not request.approval_id:
            raise ConflictError('请先预览本次变更并确认具体批次')
        actor_id = (task_identity.get() or {}).get('id', '')
        batch = session.get(AdExecutionBatch, request.approval_id)
        if not batch or batch.store_id != store_id:
            raise NotFoundError('广告审批批次不存在')
        plan = loads(batch.preview_json, {})
        if (batch.actor != actor_id or batch.action != request.action
                or plan.get('state_ids') != sorted(set(request.state_ids))):
            raise ConflictError('审批与本次请求不一致，请重新预览')
        previous = next((row for row in session.scalars(
            select(Job).where(Job.type == 'ad_optimization_execute')).all()
            if loads(row.payload_json, {}).get('approval_id') == batch.id), None)
        if previous:
            if previous.created_by != actor_id:
                raise ConflictError('广告执行任务归属不一致')
            return {'job_id': previous.id, 'status': previous.status,
                    'approval_id': batch.id}
        if batch.status != 'preview':
            raise ConflictError('该审批批次已经进入执行或核对，禁止重复提交')
        if batch.expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
            raise ConflictError('审批预览已过期，请重新预览')
        if not plan.get('source_fingerprint') or analysis_source_fingerprint(session, store_id) != plan['source_fingerprint']:
            raise ConflictError('广告数据或分析依据已变化，请重新预览')
        store = _store_or_404(session, store_id)
        if not store.enabled:
            raise ConflictError('店铺已停用')
        secrets = vault.decrypt(store.secrets_encrypted)
        if not secrets.get('performance_client_id') or not secrets.get('performance_client_secret'):
            raise ConflictError('Ozon 广告连接凭据不完整')
        try:
            batch.status = 'executing'
            batch.active_store = store_id
            job = enqueue_job(
                session, job_type='ad_optimization_execute',
                payload={'store_id': store_id, 'approval_id': batch.id,
                         'action': request.action, 'state_ids': plan['state_ids']},
                config_snapshot=vault.encrypt({}),
                idempotency_key=f'ad-execute:{batch.id}', max_attempts=3,
                steps=[('execute', '本人电脑执行已审批广告批次并核对结果')], commit=False)
            session.commit()
        except IntegrityError:
            session.rollback()
            raise ConflictError('该店铺已有广告批次正在执行或待核对') from None
        return {'job_id': job.id, 'status': job.status, 'approval_id': batch.id}
    from erp.services.ad_execution import execute
    return execute(session, store_id, request, http_request.state.operator)


@router.get("/actions/batches")
def execution_batches(store_id: str = Query(...), session: Session = Depends(get_db)) -> dict:
    from erp.models import AdExecutionBatch
    from erp.services.ad_execution import summary
    rows = session.scalars(select(AdExecutionBatch).where(AdExecutionBatch.store_id == store_id, AdExecutionBatch.status != "preview").order_by(AdExecutionBatch.created_at.desc()).limit(30)).all()
    return {"items": [summary(row) for row in rows]}


@router.get('/actions/batches/{batch_id}')
def execution_batch_details(batch_id: str, store_id: str = Query(...), session: Session = Depends(get_db)) -> dict:
    from erp.models import AdExecutionBatch
    from erp.services.ad_execution import summary
    batch = session.get(AdExecutionBatch, batch_id)
    if not batch or batch.store_id != store_id or batch.status == 'preview':
        raise NotFoundError('广告执行批次不存在')
    return summary(batch)


@router.post("/actions/batches/{batch_id}/reconcile")
def reconcile_batch(batch_id: str, store_id: str = Query(...), session: Session = Depends(get_db)) -> dict:
    if get_settings().multiuser_enabled:
        from erp.core.task_identity import task_identity
        from erp.models import AdExecutionBatch
        from erp.services.ad_desktop_snapshot import reconcile_source_fingerprint
        actor = task_identity.get() or {}
        batch = session.get(AdExecutionBatch, batch_id)
        if not batch or batch.store_id != store_id:
            raise NotFoundError('广告执行批次不存在')
        if batch.actor != actor.get('id'):
            raise ConflictError('只能核对本人发起的广告批次')
        if batch.status not in {'unknown', 'executing'} or batch.active_store != store_id:
            raise ConflictError('此广告批次无需平台核对')
        if (batch.status == 'executing' and
                (datetime.now(timezone.utc) - batch.updated_at.replace(tzinfo=timezone.utc)).total_seconds() < 300):
            raise ConflictError('批次仍可能正在执行，请稍后核对')
        original = next((row for row in session.scalars(
            select(Job).where(Job.type == 'ad_optimization_execute', Job.created_by == batch.actor)).all()
            if loads(row.payload_json, {}).get('approval_id') == batch.id), None)
        if not original or original.target_worker_id != actor.get('worker_id'):
            raise ConflictError('请选用原任务的执行电脑核对，离线后等待原电脑恢复')
        source = reconcile_source_fingerprint(session, batch)
        active = next((row for row in session.scalars(
            select(Job).where(Job.type == 'ad_optimization_reconcile',
                              Job.created_by == batch.actor,
                              Job.status.in_(['queued', 'running', 'paused']))).all()
            if loads(row.payload_json, {}).get('approval_id') == batch.id), None)
        if active:
            session.commit()
            return {'job_id': active.id, 'status': active.status, 'approval_id': batch.id}
        job = enqueue_job(
            session, job_type='ad_optimization_reconcile',
            payload={'store_id': store_id, 'approval_id': batch.id},
            config_snapshot=vault.encrypt({}),
            idempotency_key=f'ad-reconcile:{batch.id}:{source}:{uuid4().hex}',
            max_attempts=3, steps=[('reconcile', '本人电脑只读核对平台广告结果')])
        session.commit()
        return {'job_id': job.id, 'status': job.status, 'approval_id': batch.id}
    from erp.services.ad_execution import reconcile
    return reconcile(session, batch_id, store_id)


@router.get("/import-template")
def import_template() -> StreamingResponse:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "sku_id", "product_id", "name", "category_path", "price", "live_date", "ad_start_date", "metric_date",
        "impressions", "clicks", "add_to_cart", "sold_qty", "ad_spend", "ad_sales", "target_drr", "avg_cpc",
        "review_count", "rating", "stock", "fulfillment_type", "delivery_days", "break_even_drr", "image_version",
        "price_version", "outcome_label",
    ])
    writer.writerow(["SKU001", "123456", "示例商品", "家居/收纳", "499", "2026-08-01", "2026-08-02", "2026-09-01", "1500", "42", "8", "3", "180", "1497", "7%", "4.29", "12", "4.7", "60", "FBO", "9", "", "img-v1", "price-v1", "SUCCESS"])
    content = ("\ufeff" + output.getvalue()).encode("utf-8")
    filename = quote("Ozon广告历史数据导入模板.csv")
    return StreamingResponse(io.BytesIO(content), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})
