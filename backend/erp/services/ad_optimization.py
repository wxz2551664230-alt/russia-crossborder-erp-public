from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import ceil, floor, sqrt
from random import Random
from typing import Any, Iterable

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from erp.core.jsonutil import dumps, loads
from erp.services.ad_finance import assess_financials, category_break_even_drr, flatten_financial_policy
from erp.services.config_resolution import system_defaults
from erp.models import (
    AdBacktestRun,
    AdCampaign,
    AdCampaignDailyMetric,
    AdCampaignSku,
    AdDailyMetric,
    AdSku,
    AdSkuState,
    AdSkuStateHistory,
    AdThresholdVersion,
    Store,
)


ANALYSIS_WINDOW_DAYS = 30
TAIL_SUPPORT_OBSERVATIONS = 3
STOP_QUANTILE = 0.80
SYSTEM_STOP_MIN_EVIDENCE = 3
CODEX_STOP_MIN_EVIDENCE = 2


def proposed_scale_target(current_target_drr: float | None, financial: dict[str, Any]) -> float | None:
    if current_target_drr is None or current_target_drr <= 0:
        return None
    break_even = float(financial.get("effective_break_even_drr") or financial.get("break_even_drr") or 0.25)
    safe_cap = min(0.15, max(0.05, break_even * 0.60))
    proposed = min(current_target_drr + 0.01, safe_cap)
    return round(proposed, 4) if proposed > current_target_drr else None


def minimum_samples_for_quantile(quantile: float) -> int:
    tail_probability = round(min(quantile, 1 - quantile), 10)
    return ceil(TAIL_SUPPORT_OBSERVATIONS / tail_probability)


QUANTILE_MIN_SAMPLES = {
    "p25": minimum_samples_for_quantile(0.25),
    "p50": minimum_samples_for_quantile(0.50),
    "p80": minimum_samples_for_quantile(0.80),
    "p90": minimum_samples_for_quantile(0.90),
    "p95": minimum_samples_for_quantile(0.95),
}


@dataclass
class SkuAggregate:
    sku: AdSku
    analysis_as_of: date
    first_metric_date: date | None
    last_metric_date: date | None
    days_since_last_metric: int | None
    data_freshness: str
    has_metric_history: bool
    observed_history_span_days: int
    first_order_history_quality: str
    first_order_data_complete: bool
    first_order_date: date | None
    historical_order_days: int
    recent_order_days: int
    success_eligible: bool
    success_eligibility_reason: str
    campaign_targets: list[dict[str, Any]]
    multi_campaign_conflict: bool
    latest_date: date
    impressions: int
    clicks: int
    add_to_cart: int
    sold_qty: int
    ad_spend: float
    ad_sales: float
    avg_cpc: float | None
    ctr: float | None
    atc_rate: float | None
    cvr: float | None
    cart_cvr: float | None
    cpa: float | None
    actual_drr: float | None
    break_even_cvr: float | None
    effective_break_even_drr: float
    financial_model_source: str
    financial_confidence: str
    age_days: int | None
    days_to_first_order: int | None
    clicks_to_first_order: int | None
    spend_to_first_order: float | None
    historical_clicks: int
    historical_spend: float
    observed_history_days: int
    historical_sold_qty: int
    cohort_key: str
    category_key: str

    def metrics(self) -> dict:
        return {
            "window_days": ANALYSIS_WINDOW_DAYS,
            "analysis_as_of": self.analysis_as_of.isoformat(),
            "first_metric_date": self.first_metric_date.isoformat() if self.first_metric_date else None,
            "last_metric_date": self.last_metric_date.isoformat() if self.last_metric_date else None,
            "days_since_last_metric": self.days_since_last_metric,
            "data_freshness": self.data_freshness,
            "has_metric_history": self.has_metric_history,
            "observed_history_span_days": self.observed_history_span_days,
            "first_order_history_quality": self.first_order_history_quality,
            "first_order_data_complete": self.first_order_data_complete,
            "first_order_date": self.first_order_date.isoformat() if self.first_order_date else None,
            "historical_order_days": self.historical_order_days,
            "recent_order_days": self.recent_order_days,
            "success_eligible": self.success_eligible,
            "success_eligibility_reason": self.success_eligibility_reason,
            "campaign_target_count": len(self.campaign_targets),
            "multi_campaign_conflict": self.multi_campaign_conflict,
            "latest_date": self.latest_date.isoformat(),
            "impressions": self.impressions,
            "clicks": self.clicks,
            "add_to_cart": self.add_to_cart,
            "sold_qty": self.sold_qty,
            "ad_spend": round(self.ad_spend, 2),
            "ad_sales": round(self.ad_sales, 2),
            "avg_cpc": _rounded(self.avg_cpc),
            "ctr": _rounded(self.ctr),
            "atc_rate": _rounded(self.atc_rate),
            "cvr": _rounded(self.cvr),
            "cart_cvr": _rounded(self.cart_cvr),
            "cpa": _rounded(self.cpa),
            "actual_drr": _rounded(self.actual_drr),
            "break_even_cvr": _rounded(self.break_even_cvr),
            "effective_break_even_drr": _rounded(self.effective_break_even_drr),
            "financial_model_source": self.financial_model_source,
            "financial_confidence": self.financial_confidence,
            "age_days": self.age_days,
            "clicks_to_first_order": self.clicks_to_first_order,
            "spend_to_first_order": _rounded(self.spend_to_first_order),
            "historical_clicks": self.historical_clicks,
            "historical_spend": _rounded(self.historical_spend, 2),
            "observed_history_days": self.observed_history_days,
            "historical_sold_qty": self.historical_sold_qty,
        }


def _rounded(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(value, digits)


def safe_div(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0 else None


def percentile(values: Iterable[float | int | None], quantile: float) -> float | None:
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _threshold_stat(values: Iterable[float | int | None], quantile: float) -> tuple[float | None, dict]:
    clean = [float(value) for value in values if value is not None]
    required = minimum_samples_for_quantile(quantile)
    label = f"P{int(round(quantile * 100))}"
    quality = {
        "quantile": label,
        "sample_size": len(clean),
        "minimum_sample_size": required,
        "tail_support_required": TAIL_SUPPORT_OBSERVATIONS,
        "status": "AVAILABLE" if len(clean) >= required else ("INSUFFICIENT_TAIL_SAMPLE" if quantile >= 0.80 else "INSUFFICIENT_SAMPLE"),
        "stability": "UNAVAILABLE",
        "bootstrap_ci_low": None,
        "bootstrap_ci_high": None,
        "bootstrap_relative_width": None,
    }
    if len(clean) < required:
        return None, quality
    value = percentile(clean, quantile)
    rng = Random(f"ad-threshold:{label}:{len(clean)}:{sum(clean):.12f}")
    estimates = [percentile((clean[rng.randrange(len(clean))] for _ in clean), quantile) for _ in range(250)]
    low = percentile(estimates, 0.025)
    high = percentile(estimates, 0.975)
    spread = max(clean) - min(clean)
    relative_width = safe_div((high or 0) - (low or 0), spread) if spread > 0 else 0.0
    tail_support = len(clean) * min(quantile, 1 - quantile)
    if relative_width is not None and relative_width <= 0.25 and tail_support >= 5:
        stability = "HIGH"
    elif relative_width is not None and relative_width <= 0.75 and tail_support >= TAIL_SUPPORT_OBSERVATIONS:
        stability = "MEDIUM"
    else:
        stability = "LOW"
    quality.update({
        "stability": stability,
        "bootstrap_ci_low": _rounded(low),
        "bootstrap_ci_high": _rounded(high),
        "bootstrap_relative_width": _rounded(relative_width),
    })
    return value, quality


def _band(value: float | int | None, steps: tuple[float, ...], labels: tuple[str, ...]) -> str:
    if value is None:
        return "未知"
    for boundary, label in zip(steps, labels):
        if value <= boundary:
            return label
    return labels[-1]


def _cohort_keys(sku: AdSku, age_days: int | None) -> tuple[str, str]:
    category = sku.category_path.strip() or sku.category_id.strip() or "未分类"
    price_band = _band(sku.price, (300, 500, 1000, 2000, float("inf")), ("≤300", "301-500", "501-1000", "1001-2000", ">2000"))
    age_band = _band(age_days, (7, 14, 30, 90, float("inf")), ("0-7天", "8-14天", "15-30天", "31-90天", ">90天"))
    review_band = _band(sku.review_count, (0, 10, 50, 200, float("inf")), ("0评", "1-10评", "11-50评", "51-200评", ">200评"))
    rating_band = _band(sku.rating, (0, 3.9, 4.4, 4.7, 5), ("无评分", "<4.0", "4.0-4.4", "4.5-4.7", "4.8-5.0"))
    drr = "未知" if sku.target_drr is None else f"{sku.target_drr:.3f}"
    delivery_band = _band(sku.delivery_days, (7, 14, 21, 30, float("inf")), ("≤7天", "8-14天", "15-21天", "22-30天", ">30天"))
    full = " × ".join((category, f"{price_band} RUB", age_band, review_band, rating_band, f"目标DRR {drr}", f"配送{delivery_band}"))
    return full, f"类目：{category}"


def _freshness_policy(grouped: dict[str, list[AdDailyMetric]]) -> dict[str, int | float]:
    gaps: list[int] = []
    for rows in grouped.values():
        dates = sorted({row.metric_date for row in rows})
        gaps.extend((current - previous).days for previous, current in zip(dates, dates[1:]) if current > previous)
    p75 = percentile(gaps, 0.75)
    p95 = percentile(gaps, 0.95)
    fresh_days = max(1, ceil(p75 or 1))
    no_recent_days = max(fresh_days + 1, ceil(p95 or fresh_days * 3))
    return {
        "observed_gap_count": len(gaps),
        "gap_p75_days": _rounded(p75, 2),
        "gap_p95_days": _rounded(p95, 2),
        "fresh_max_days": fresh_days,
        "stale_max_days": no_recent_days,
    }


def _data_freshness(days_since_last_metric: int, policy: dict[str, int | float]) -> str:
    if days_since_last_metric <= int(policy["fresh_max_days"]):
        return "FRESH"
    if days_since_last_metric <= int(policy["stale_max_days"]):
        return "STALE"
    return "NO_RECENT_DATA"


def _first_order_history_quality(
    sku: AdSku,
    history: list[AdDailyMetric],
    analysis_as_of: date,
    first_order_date: date | None,
) -> str:
    if not sku.ad_start_date:
        return "UNKNOWN"
    origin = sku.ad_start_date.date()
    first_metric = history[0].metric_date
    if first_metric > origin:
        return "LEFT_TRUNCATED"
    endpoint = first_order_date or analysis_as_of
    if endpoint < origin:
        return "UNKNOWN"
    observed = {row.metric_date for row in history if origin <= row.metric_date <= endpoint}
    expected_days = (endpoint - origin).days + 1
    return "COMPLETE" if len(observed) == expected_days else "UNKNOWN"


def _aggregate(
    sku: AdSku,
    rows: list[AdDailyMetric],
    financial_policy: dict[str, Any],
    analysis_as_of: date,
    freshness_policy: dict[str, int | float],
) -> SkuAggregate | None:
    age_origin = sku.ad_start_date or sku.live_date
    age_days = max((analysis_as_of - age_origin.date()).days + 1, 0) if age_origin else None
    category_break_even = category_break_even_drr(
        financial_policy,
        category_id=sku.category_id,
        category_path=sku.category_path,
    )
    financial = assess_financials(
        None,
        exact_break_even_drr=sku.break_even_drr,
        category_break_even_drr=category_break_even,
        policy=financial_policy,
    )
    effective_break_even = float(financial["effective_break_even_drr"])
    cohort_key, category_key = _cohort_keys(sku, age_days)
    if not rows:
        return SkuAggregate(
            sku=sku,
            analysis_as_of=analysis_as_of,
            first_metric_date=None,
            last_metric_date=None,
            days_since_last_metric=None,
            data_freshness="NO_METRIC_DATA",
            has_metric_history=False,
            observed_history_span_days=0,
            first_order_history_quality="UNKNOWN",
            first_order_data_complete=False,
            first_order_date=None,
            historical_order_days=0,
            recent_order_days=0,
            success_eligible=False,
            success_eligibility_reason="没有广告指标",
            campaign_targets=[],
            multi_campaign_conflict=False,
            latest_date=analysis_as_of,
            impressions=0,
            clicks=0,
            add_to_cart=0,
            sold_qty=0,
            ad_spend=0.0,
            ad_sales=0.0,
            avg_cpc=None,
            ctr=None,
            atc_rate=None,
            cvr=None,
            cart_cvr=None,
            cpa=None,
            actual_drr=None,
            break_even_cvr=None,
            effective_break_even_drr=effective_break_even,
            financial_model_source=str(financial["financial_model_source"]),
            financial_confidence=str(financial["financial_confidence"]),
            age_days=age_days,
            days_to_first_order=None,
            clicks_to_first_order=None,
            spend_to_first_order=None,
            historical_clicks=0,
            historical_spend=0.0,
            observed_history_days=0,
            historical_sold_qty=0,
            cohort_key=cohort_key,
            category_key=category_key,
        )
    first_metric = min(row.metric_date for row in rows)
    last_metric = max(row.metric_date for row in rows)
    days_since_last_metric = max((analysis_as_of - last_metric).days, 0)
    window_start = analysis_as_of - timedelta(days=ANALYSIS_WINDOW_DAYS - 1)
    current = [row for row in rows if window_start <= row.metric_date <= analysis_as_of]
    impressions = sum(row.impressions for row in current)
    clicks = sum(row.clicks for row in current)
    carts = sum(row.add_to_cart for row in current)
    sold = sum(row.sold_qty for row in current)
    spend = sum(row.ad_spend for row in current)
    sales = sum(row.ad_sales for row in current)
    avg_cpc = safe_div(spend, clicks)
    history = sorted(rows, key=lambda item: item.metric_date)
    first_order_index = next((index for index, row in enumerate(history) if row.sold_qty > 0), None)
    first_order = history[first_order_index].metric_date if first_order_index is not None else None
    through_first = history[: first_order_index + 1] if first_order_index is not None else []
    first_order_history_quality = _first_order_history_quality(sku, history, analysis_as_of, first_order)
    first_order_data_complete = first_order_history_quality == "COMPLETE"
    order_origin = age_origin.date() if age_origin else first_metric
    days_to_first_order = max((first_order - order_origin).days + 1, 0) if first_order else None
    return SkuAggregate(
        sku=sku,
        analysis_as_of=analysis_as_of,
        first_metric_date=first_metric,
        last_metric_date=last_metric,
        days_since_last_metric=days_since_last_metric,
        data_freshness=_data_freshness(days_since_last_metric, freshness_policy),
        has_metric_history=True,
        observed_history_span_days=max((last_metric - first_metric).days + 1, 1),
        first_order_history_quality=first_order_history_quality,
        first_order_data_complete=first_order_data_complete,
        first_order_date=first_order,
        historical_order_days=sum(1 for row in history if row.sold_qty > 0),
        recent_order_days=sum(1 for row in current if row.sold_qty > 0),
        success_eligible=False,
        success_eligibility_reason="尚未评估",
        campaign_targets=[],
        multi_campaign_conflict=False,
        latest_date=last_metric,
        impressions=impressions,
        clicks=clicks,
        add_to_cart=carts,
        sold_qty=sold,
        ad_spend=spend,
        ad_sales=sales,
        avg_cpc=avg_cpc,
        ctr=safe_div(clicks, impressions),
        atc_rate=safe_div(carts, clicks),
        cvr=safe_div(sold, clicks),
        cart_cvr=safe_div(sold, carts),
        cpa=safe_div(spend, sold),
        actual_drr=safe_div(spend, sales),
        break_even_cvr=(safe_div(avg_cpc, (sku.price or 0) * effective_break_even) if avg_cpc is not None else None),
        effective_break_even_drr=effective_break_even,
        financial_model_source=str(financial["financial_model_source"]),
        financial_confidence=str(financial["financial_confidence"]),
        age_days=age_days,
        days_to_first_order=days_to_first_order if first_order_data_complete else None,
        clicks_to_first_order=sum(row.clicks for row in through_first) if through_first and first_order_data_complete else None,
        spend_to_first_order=sum(row.ad_spend for row in through_first) if through_first and first_order_data_complete else None,
        historical_clicks=sum(row.clicks for row in history),
        historical_spend=sum(row.ad_spend for row in history),
        observed_history_days=max((analysis_as_of - order_origin).days + 1, 0),
        historical_sold_qty=sum(row.sold_qty for row in history),
        cohort_key=cohort_key,
        category_key=category_key,
    )


def aggregate_store(session: Session, store_id: str) -> list[SkuAggregate]:
    skus = session.scalars(select(AdSku).where(AdSku.store_id == store_id)).all()
    if not skus:
        return []
    rows = session.scalars(select(AdDailyMetric).where(AdDailyMetric.ad_sku_id.in_([sku.id for sku in skus]))).all()
    grouped: dict[str, list[AdDailyMetric]] = defaultdict(list)
    for row in rows:
        grouped[row.ad_sku_id].append(row)
    analysis_as_of = max((row.metric_date for row in rows), default=date.today() - timedelta(days=1))
    freshness_policy = _freshness_policy(grouped)
    policy = flatten_financial_policy(system_defaults(session).get("financial_policy"))
    aggregates = [
        value for sku in skus
        if (value := _aggregate(sku, grouped.get(sku.id, []), policy, analysis_as_of, freshness_policy)) is not None
    ]
    _assign_success_eligibility(aggregates, policy)
    _attach_campaign_context(session, store_id, aggregates)
    return aggregates


def _running_status(value: str) -> bool:
    normalized = str(value or "").strip().upper()
    return normalized in {"ACTIVE", "RUNNING", "CAMPAIGN_STATE_RUNNING"} or normalized.endswith("_RUNNING")


def _attach_campaign_context(session: Session, store_id: str, items: list[SkuAggregate]) -> None:
    by_sku: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pairs = session.execute(
        select(AdCampaignSku, AdCampaign)
        .join(AdCampaign, AdCampaign.id == AdCampaignSku.ad_campaign_id)
        .where(AdCampaign.store_id == store_id)
    ).all()
    analysis_as_of = max((item.analysis_as_of for item in items), default=date.today())
    week_start = analysis_as_of - timedelta(days=6)
    recent_campaign_rows = session.scalars(
        select(AdCampaignDailyMetric).where(
            AdCampaignDailyMetric.store_id == store_id,
            AdCampaignDailyMetric.metric_date >= week_start,
            AdCampaignDailyMetric.metric_date <= analysis_as_of,
        )
    ).all()
    weekly_spend: dict[str, float] = defaultdict(float)
    for row in recent_campaign_rows:
        weekly_spend[row.campaign_id] += float(row.ad_spend or 0)
    for config, campaign in pairs:
        if not (_running_status(campaign.state) and _running_status(config.status or campaign.state)):
            continue
        budget_limit = campaign.weekly_budget or campaign.budget
        spend_7d = weekly_spend.get(campaign.campaign_id, 0.0)
        by_sku[config.sku_id].append({
            "campaign_id": campaign.campaign_id,
            "campaign_name": campaign.name,
            "sku_id": config.sku_id,
            "current_target_drr": config.target_drr,
            "previous_target_drr": config.previous_target_drr,
            "last_change_time": config.last_change_time.isoformat() if config.last_change_time else None,
            "status": config.status,
            "campaign_state": campaign.state,
            "budget": campaign.budget,
            "daily_budget": campaign.daily_budget,
            "weekly_budget": campaign.weekly_budget,
            "campaign_spend_7d": round(spend_7d, 2),
            "budget_utilization_7d": safe_div(spend_7d, float(budget_limit)) if budget_limit else None,
        })
    for item in items:
        item.campaign_targets = sorted(by_sku.get(item.sku.sku_id, []), key=lambda row: row["campaign_id"])
        item.multi_campaign_conflict = len(item.campaign_targets) > 1


def _assign_success_eligibility(items: list[SkuAggregate], financial_policy: dict[str, Any]) -> None:
    positive = [item for item in items if item.historical_sold_qty > 0 and item.data_freshness == "FRESH"]
    order_days_p50 = percentile((item.historical_order_days for item in positive), 0.50)
    sold_qty_p75 = percentile((item.historical_sold_qty for item in positive), 0.75)
    for item in items:
        manual = item.sku.outcome_label.strip().upper()
        if manual == "SUCCESS":
            item.success_eligible = bool(
                item.has_metric_history
                and item.historical_sold_qty > 0
                and item.data_freshness == "FRESH"
            )
            item.success_eligibility_reason = (
                "人工 SUCCESS 标签且当前可观测数据通过"
                if item.success_eligible
                else "人工 SUCCESS 标签缺少当时可观测的成交或新鲜数据"
            )
            continue
        if manual:
            item.success_eligibility_reason = f"人工标签 {manual} 不属于 SUCCESS"
            continue
        if item.historical_sold_qty <= 1:
            item.success_eligibility_reason = "仅有一件归因成交，只作为 FIRST_ORDER_POSITIVE 证据"
            continue
        economics_passed = item.actual_drr is not None and item.actual_drr <= item.effective_break_even_drr
        sustained = (
            item.historical_order_days >= max(2.0, float(order_days_p50 or 2.0))
            or item.historical_sold_qty >= max(2.0, float(sold_qty_p75 or 2.0))
        )
        item.success_eligible = bool(
            item.data_freshness == "FRESH"
            and item.sold_qty > 0
            and economics_passed
            and sustained
        )
        item.success_eligibility_reason = (
            "重复成交、近期成交和广告经济性均通过本店正向样本门槛"
            if item.success_eligible
            else "重复成交、近期成交、数据新鲜度或广告经济性未同时满足"
        )


def _matching(items: list[SkuAggregate], key: str, level: str) -> list[SkuAggregate]:
    if level == "full":
        return [item for item in items if item.cohort_key == key]
    if level == "category":
        return [item for item in items if item.category_key == key]
    return items


def _kaplan_meier(rows: Iterable[tuple[float, bool]]) -> list[dict[str, Any]]:
    grouped: dict[float, list[bool]] = defaultdict(list)
    for duration, event in rows:
        grouped[max(float(duration), 0.0)].append(bool(event))
    at_risk = sum(len(flags) for flags in grouped.values())
    survival = 1.0
    curve: list[dict[str, Any]] = []
    for duration in sorted(grouped):
        flags = grouped[duration]
        events = sum(flags)
        censored = len(flags) - events
        if at_risk:
            survival *= 1 - events / at_risk
        curve.append({
            "duration": _rounded(duration, 2),
            "at_risk": at_risk,
            "events": events,
            "censored": censored,
            "survival_probability": _rounded(survival),
        })
        at_risk -= len(flags)
    return curve


def _click_survival_model(items: list[SkuAggregate]) -> dict[str, Any]:
    eligible = [item for item in items if item.first_order_data_complete]
    observations = [
        (
            float(item.clicks_to_first_order if item.first_order_date else item.historical_clicks),
            bool(item.first_order_date),
        )
        for item in eligible
    ]
    event_count = sum(1 for _, event in observations if event)
    minimum_sample_size = QUANTILE_MIN_SAMPLES["p50"]
    status = "AVAILABLE" if len(observations) >= minimum_sample_size and event_count >= TAIL_SUPPORT_OBSERVATIONS else "INSUFFICIENT_SAMPLE"
    return {
        "status": status,
        "sample_size": len(observations),
        "minimum_sample_size": minimum_sample_size,
        "event_count": event_count,
        "minimum_event_count": TAIL_SUPPORT_OBSERVATIONS,
        "right_censored_count": len(observations) - event_count,
        "curve": _kaplan_meier(observations),
        "method": "KAPLAN_MEIER_RIGHT_CENSORED",
    }


def _survival_at(curve: list[dict[str, Any]], exposure: float) -> float:
    survival = 1.0
    for point in curve:
        if float(point["duration"]) > exposure:
            break
        survival = float(point["survival_probability"])
    return survival


def _survival_estimate(item: SkuAggregate, thresholds: dict) -> dict[str, Any]:
    model = thresholds.get("click_survival") if isinstance(thresholds.get("click_survival"), dict) else {}
    complete_events = [
        float(value)
        for value in model.get("event_durations", [])
        if value is not None
    ]
    exposure = float(item.clicks_to_first_order if item.clicks_to_first_order is not None else item.historical_clicks)
    percentile_rank = safe_div(sum(1 for value in complete_events if value <= exposure), len(complete_events))
    curve = model.get("curve") if isinstance(model.get("curve"), list) else []
    future_probability = None
    if model.get("status") == "AVAILABLE" and curve:
        current_survival = _survival_at(curve, exposure)
        horizon_survival = float(curve[-1]["survival_probability"])
        future_probability = (
            max(0.0, min(1.0, 1 - horizon_survival / current_survival))
            if current_survival > 0
            else 0.0
        )
    return {
        "first_order_history_quality": item.first_order_history_quality,
        "clicks_to_first_order_percentile": _rounded(percentile_rank),
        "estimated_future_order_probability": _rounded(future_probability),
        "current_click_exposure": int(exposure),
        "model_status": model.get("status", "UNAVAILABLE"),
        "model_source_level": model.get("source_level", "unavailable"),
        "eligible_sample_size": int(model.get("sample_size") or 0),
        "event_count": int(model.get("event_count") or 0),
        "right_censored_count": int(model.get("right_censored_count") or 0),
        "estimation_horizon_clicks": curve[-1]["duration"] if curve else None,
        "method": model.get("method", "KAPLAN_MEIER_RIGHT_CENSORED"),
    }


def _learn_thresholds(successes: list[SkuAggregate], all_items: list[SkuAggregate]) -> dict:
    first_orders = [item for item in all_items if item.first_order_data_complete and item.first_order_date]
    scale_examples = [
        item for item in all_items
        if item.data_freshness == "FRESH"
        and item.sold_qty > 0
        and item.actual_drr is not None
        and item.actual_drr <= item.effective_break_even_drr
    ]
    specs = {
        "low_ctr_threshold": ((item.ctr for item in successes), 0.25, 6),
        "low_atc_threshold": ((item.atc_rate for item in successes), 0.25, 6),
        "low_cvr_threshold": ((item.cvr for item in successes), 0.25, 6),
        "low_cart_cvr_threshold": ((item.cart_cvr for item in successes), 0.25, 6),
        "traffic_starved_threshold": ((item.impressions for item in successes), 0.25, 2),
        "typical_impressions": ((item.impressions for item in scale_examples), 0.50, 2),
        "min_click_sample": ((item.clicks for item in scale_examples), 0.25, 2),
        "min_order_sample": ((item.sold_qty for item in scale_examples), 0.25, 2),
        "stop_click_threshold": ((item.clicks_to_first_order for item in first_orders), STOP_QUANTILE, 2),
        "stop_spend_threshold": ((item.spend_to_first_order for item in first_orders), STOP_QUANTILE, 2),
        "healthy_cvr": ((item.cvr for item in scale_examples), 0.50, 6),
        "cold_start_duration": ((item.days_to_first_order for item in first_orders), 0.80, 2),
    }
    result: dict = {}
    quality: dict[str, dict] = {}
    for name, (values, quantile, digits) in specs.items():
        value, details = _threshold_stat(values, quantile)
        result[name] = _rounded(value, digits)
        quality[name] = details
    click_survival = _click_survival_model(all_items)
    click_survival["event_durations"] = [
        item.clicks_to_first_order
        for item in first_orders
        if item.clicks_to_first_order is not None
    ]
    result.update({
        "threshold_quality": quality,
        "quantile_min_samples": QUANTILE_MIN_SAMPLES,
        "sample_profile": {
            "total_samples": len(all_items),
            "success_samples": len(successes),
            "scale_positive_samples": len(scale_examples),
            "first_order_events": len(first_orders),
            "right_censored_samples": click_survival["right_censored_count"],
        },
        "click_survival": click_survival,
        "historical_failure_probability": None,
        "historical_failure_probability_status": "EXPERIMENTAL_UNAVAILABLE",
        "method": "按分位数尾部支持量控制最低样本，并用确定性 bootstrap 评估阈值稳定性",
    })
    return result


def _with_first_order_fallback(item: SkuAggregate, thresholds: dict, all_items: list[SkuAggregate]) -> dict:
    result = dict(thresholds)
    quality = dict(result.get("threshold_quality") or {})
    candidates = [
        (item.cohort_key, "full", _learn_thresholds([], _matching(all_items, item.cohort_key, "full"))),
        (item.category_key, "category", _learn_thresholds([], _matching(all_items, item.category_key, "category"))),
        ("全店", "store", _learn_thresholds([], all_items)),
    ]
    for field in ("stop_click_threshold", "stop_spend_threshold", "cold_start_duration"):
        for key, level, candidate in candidates:
            if candidate.get(field) is not None:
                result[field] = candidate[field]
                quality[field] = {**candidate["threshold_quality"][field], "source_level": level, "source_key": key}
                break
        else:
            result[field] = None
            quality[field] = {**quality[field], "source_level": "unavailable", "reason": "INSUFFICIENT_TAIL_SAMPLE"}
    for key, level, candidate in candidates:
        model = candidate.get("click_survival") or {}
        if model.get("status") == "AVAILABLE":
            result["click_survival"] = {**model, "source_level": level, "source_key": key}
            break
    else:
        fallback_model = candidates[-1][2].get("click_survival") or {}
        result["click_survival"] = {**fallback_model, "source_level": "unavailable", "source_key": "全店"}
    result["threshold_quality"] = quality
    return result


def effective_outcome(item: SkuAggregate) -> str | None:
    """Resolve training labels without treating censored zero-order SKUs as failures."""
    manual = item.sku.outcome_label.strip().upper()
    if manual == "SUCCESS":
        return "SUCCESS" if item.success_eligible else None
    if manual == "FAILED":
        return "FAILED"
    if manual:
        return None
    if item.success_eligible:
        return "SUCCESS"
    return None


def _choose_cohort(item: SkuAggregate, successes: list[SkuAggregate]) -> tuple[str, str, list[SkuAggregate]] | None:
    for key, level in ((item.cohort_key, "full"), (item.category_key, "category"), ("全店", "store")):
        matched = _matching(successes, key, level)
        if len(matched) >= QUANTILE_MIN_SAMPLES["p25"]:
            return key, level, matched
    return None


def _fmt_percent(value: float | None) -> str:
    return "暂无" if value is None else f"{value * 100:.2f}%"


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if trials <= 0:
        return None, None
    bounded_successes = min(max(successes, 0), trials)
    rate = bounded_successes / trials
    denominator = 1 + z * z / trials
    center = (rate + z * z / (2 * trials)) / denominator
    margin = z * sqrt((rate * (1 - rate) + z * z / (4 * trials)) / trials) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def _quality_status(thresholds: dict | None, field: str) -> str:
    if not thresholds:
        return "UNAVAILABLE"
    return str((thresholds.get("threshold_quality") or {}).get(field, {}).get("status") or "UNAVAILABLE")


def _evidence_strength(
    item: SkuAggregate,
    thresholds: dict,
    cohort_size: int,
    financial: dict[str, Any],
    data_quality_ok: bool,
) -> tuple[str, dict[str, str]]:
    min_clicks = thresholds.get("min_click_sample")
    if min_clicks is None or item.clicks < float(min_clicks):
        sample_strength = "LOW"
    elif item.clicks >= max(float(min_clicks) * 3, float(min_clicks) + 20):
        sample_strength = "HIGH"
    else:
        sample_strength = "MEDIUM"
    cohort_strength = (
        "HIGH" if cohort_size >= QUANTILE_MIN_SAMPLES["p95"]
        else "MEDIUM" if cohort_size >= QUANTILE_MIN_SAMPLES["p25"]
        else "LOW"
    )
    available_quality = [
        details
        for details in (thresholds.get("threshold_quality") or {}).values()
        if details.get("status") == "AVAILABLE"
    ]
    stabilities = {str(details.get("stability") or "LOW") for details in available_quality}
    threshold_stability = (
        "HIGH" if available_quality and stabilities == {"HIGH"}
        else "MEDIUM" if available_quality and "LOW" not in stabilities
        else "LOW"
    )
    source_levels = {
        str(details.get("source_level"))
        for details in (thresholds.get("threshold_quality") or {}).values()
        if details.get("source_level")
    }
    survival_source = str((thresholds.get("click_survival") or {}).get("source_level") or "")
    if survival_source:
        source_levels.add(survival_source)
    if "unavailable" in source_levels:
        fallback_penalty = "UNAVAILABLE"
    elif "store" in source_levels:
        fallback_penalty = "STORE"
    elif "category" in source_levels:
        fallback_penalty = "CATEGORY"
    else:
        fallback_penalty = "NONE"
    survival = thresholds.get("click_survival") or {}
    censored_count = int(survival.get("right_censored_count") or 0)
    negative_strength = "HIGH" if censored_count >= 12 else "MEDIUM" if censored_count >= TAIL_SUPPORT_OBSERVATIONS else "LOW"
    components = {
        "sample_strength": sample_strength,
        "cohort_strength": cohort_strength,
        "threshold_stability": threshold_stability,
        "data_freshness": item.data_freshness,
        "cohort_fallback_penalty": fallback_penalty,
        "positive_sample_strength": cohort_strength,
        "negative_sample_strength": negative_strength,
        "financial_confidence": str(financial["financial_confidence"]),
    }
    if item.data_freshness != "FRESH" or not data_quality_ok or cohort_strength == "LOW" or threshold_stability == "LOW":
        return "LOW", components
    if (
        sample_strength == "HIGH"
        and cohort_strength == "HIGH"
        and threshold_stability == "HIGH"
        and negative_strength == "HIGH"
        and financial["financial_confidence"] == "HIGH"
        and fallback_penalty == "NONE"
    ):
        return "HIGH", components
    return "MEDIUM", components


def _decision(item: SkuAggregate, thresholds: dict | None, cohort_size: int, financial_policy: dict[str, Any]) -> dict[str, Any]:
    metrics = item.metrics()
    financial = assess_financials(
        item.actual_drr,
        exact_break_even_drr=item.sku.break_even_drr,
        category_break_even_drr=category_break_even_drr(
            financial_policy,
            category_id=item.sku.category_id,
            category_path=item.sku.category_path,
        ),
        policy=financial_policy,
    )
    financial["break_even_cvr"] = metrics["break_even_cvr"]
    thresholds = thresholds or {}
    survival = _survival_estimate(item, thresholds)
    min_impressions = thresholds.get("traffic_starved_threshold")
    min_clicks = thresholds.get("min_click_sample")
    min_orders = thresholds.get("min_order_sample")
    cold_days = thresholds.get("cold_start_duration")
    low_ctr = thresholds.get("low_ctr_threshold")
    low_atc = thresholds.get("low_atc_threshold")
    low_cvr = thresholds.get("low_cvr_threshold")
    low_cart_cvr = thresholds.get("low_cart_cvr_threshold")
    healthy_cvr = thresholds.get("healthy_cvr")
    stop_clicks = thresholds.get("stop_click_threshold")
    stop_spend = thresholds.get("stop_spend_threshold")
    typical_impressions = thresholds.get("typical_impressions")
    has_operational_thresholds = any(
        thresholds.get(field) is not None
        for field in (
            "low_ctr_threshold", "low_atc_threshold", "low_cvr_threshold",
            "low_cart_cvr_threshold", "traffic_starved_threshold", "min_click_sample",
        )
    )
    cvr_lower, cvr_upper = wilson_interval(item.sold_qty, item.clicks)
    flags: list[str] = []
    risk_flags: list[str] = []
    active_campaign = bool(item.campaign_targets)
    active_no_exposure = active_campaign and item.impressions <= 0
    evidence = [
        f"30 天窗口：展示 {item.impressions}，点击 {item.clicks}，加购 {item.add_to_cart}，广告订单 {item.sold_qty}",
        f"CTR {_fmt_percent(item.ctr)}，CVR {_fmt_percent(item.cvr)}，实际 DRR {_fmt_percent(item.actual_drr)}",
        f"全店分析截止日 {item.analysis_as_of.isoformat()}；数据新鲜度 {item.data_freshness}",
    ]
    if active_no_exposure:
        flags.append("ACTIVE_NO_EXPOSURE")
        evidence.append("商品仍在运行中的广告计划内，但分析窗口没有展示")
    elif not item.has_metric_history:
        flags.append("NO_METRIC_DATA")
    if item.data_freshness != "FRESH":
        if item.has_metric_history:
            flags.append("STALE_DATA" if item.data_freshness == "STALE" else "NO_RECENT_DATA")
        risk_flags.append("DATA_FRESHNESS")
    if cold_days is not None and item.age_days is not None and item.age_days <= cold_days and item.historical_sold_qty == 0:
        flags.append("COLD_START")
        evidence.append(f"商品广告年龄 {item.age_days} 天，成功 SKU 的 P80 首单时间为 {cold_days:.1f} 天")
    if min_impressions is not None and item.impressions < float(min_impressions):
        flags.extend(("LOW_IMPRESSIONS", "TRAFFIC_STARVED"))
    if min_clicks is not None and item.clicks < float(min_clicks):
        flags.append("INSUFFICIENT_CLICK_SAMPLE")
    impressions_sufficient = min_impressions is not None and item.impressions >= float(min_impressions)
    clicks_sufficient = min_clicks is not None and item.clicks >= float(min_clicks)
    if low_ctr is not None and item.ctr is not None and impressions_sufficient and item.ctr < float(low_ctr):
        flags.append("LOW_CTR")
        evidence.append(f"同类成功 SKU CTR P25 为 {_fmt_percent(low_ctr)}")
    if low_atc is not None and item.atc_rate is not None and clicks_sufficient and item.atc_rate < float(low_atc):
        flags.append("LOW_ATC")
        evidence.append(f"同类成功 SKU 加购率 P25 为 {_fmt_percent(low_atc)}")
    if low_cart_cvr is not None and item.atc_rate is not None and low_atc is not None and item.atc_rate >= float(low_atc) and item.cart_cvr is not None and item.cart_cvr < float(low_cart_cvr):
        flags.append("HIGH_CART_LOW_ORDER")
        evidence.append(f"同类成功 SKU 加购成交率 P25 为 {_fmt_percent(low_cart_cvr)}")
    if low_cvr is not None and item.cvr is not None and clicks_sufficient and item.cvr < float(low_cvr):
        flags.append("LOW_CVR")
        evidence.append(f"同类成功 SKU CVR P25 为 {_fmt_percent(low_cvr)}")
    enough_stop_clicks = stop_clicks is not None and item.clicks >= float(stop_clicks)
    enough_stop_spend = stop_spend is not None and item.ad_spend >= float(stop_spend)
    no_recent_orders = item.sold_qty == 0
    if no_recent_orders and enough_stop_clicks:
        flags.append("ZERO_ORDER_HIGH_CLICK")
    if no_recent_orders and enough_stop_spend:
        flags.append("ZERO_ORDER_HIGH_SPEND")
    if no_recent_orders and item.historical_sold_qty > 0 and (enough_stop_clicks or enough_stop_spend):
        flags.append("RECENT_ZERO_ORDER_DECAY")
    if financial["financial_status"] == "FINANCIAL_DANGER" and item.sold_qty > 0 and clicks_sufficient:
        flags.append("BAD_ECONOMICS")
    financial_safe_to_scale = bool(
        financial["passed"] is True
        and item.actual_drr is not None
        and item.actual_drr < float(financial["effective_break_even_drr"])
    )
    scale_signal = bool(
        typical_impressions is not None
        and item.impressions < float(typical_impressions)
        and healthy_cvr is not None
        and min_clicks is not None
        and min_orders is not None
        and item.clicks >= float(min_clicks)
        and item.sold_qty >= float(min_orders)
        and cvr_lower is not None
        and cvr_lower >= float(healthy_cvr)
        and financial_safe_to_scale
    )
    if scale_signal:
        flags.append("SCALE_CANDIDATE")
        evidence.append(f"同类成功 SKU 典型展示量为 {typical_impressions:.0f}")
    if cvr_lower is not None:
        evidence.append(f"CVR Wilson 95% 区间 {_fmt_percent(cvr_lower)} 至 {_fmt_percent(cvr_upper)}")
    if survival["model_status"] == "AVAILABLE":
        evidence.append(
            f"首单点击生存模型：当前点击暴露分位 {_fmt_percent(survival['clicks_to_first_order_percentile'])}，"
            f"估计观察上限内未来首单概率 {_fmt_percent(survival['estimated_future_order_probability'])}"
        )

    primary_priority = (
        "ACTIVE_NO_EXPOSURE", "NO_METRIC_DATA", "NO_RECENT_DATA", "STALE_DATA", "COLD_START",
        "RECENT_ZERO_ORDER_DECAY", "ZERO_ORDER_HIGH_SPEND", "ZERO_ORDER_HIGH_CLICK",
        "LOW_IMPRESSIONS", "LOW_CTR", "LOW_ATC", "HIGH_CART_LOW_ORDER", "LOW_CVR",
        "BAD_ECONOMICS", "SCALE_CANDIDATE", "INSUFFICIENT_CLICK_SAMPLE",
    )
    primary_state = next((flag for flag in primary_priority if flag in flags), "HEALTHY" if has_operational_thresholds else "OBSERVATION")
    secondary_flags = [flag for flag in flags if flag != primary_state]
    stop_evidence_flags: list[str] = []
    if enough_stop_clicks:
        stop_evidence_flags.append("FIRST_ORDER_CLICK_THRESHOLD")
    if enough_stop_spend:
        stop_evidence_flags.append("FIRST_ORDER_SPEND_THRESHOLD")
    if item.clicks > 0 and item.add_to_cart == 0:
        stop_evidence_flags.append("NO_CART_SIGNAL")
    elif "LOW_ATC" in flags:
        stop_evidence_flags.append("LOW_ATC")
    if item.clicks > 0 and item.break_even_cvr is not None and (item.cvr or 0) < item.break_even_cvr:
        stop_evidence_flags.append("BELOW_BREAK_EVEN_CVR")
    stop_evidence_count = len(stop_evidence_flags)
    stop_candidate = bool(
        no_recent_orders
        and (enough_stop_clicks or enough_stop_spend)
        and stop_evidence_count >= CODEX_STOP_MIN_EVIDENCE
    )
    decision_tier = "STANDARD"
    review_owner = "POLICY_ENGINE"
    budget_limited = any(
        target.get("budget_utilization_7d") is not None
        and float(target["budget_utilization_7d"]) >= 0.95
        for target in item.campaign_targets
    )
    if stop_candidate:
        candidate_action = "STOP_ADS_CANDIDATE"
        action_risk = "HIGH"
        if stop_evidence_count >= SYSTEM_STOP_MIN_EVIDENCE:
            decision_tier = "SYSTEM_HIGH_CONFIDENCE"
        else:
            decision_tier = "CODEX_REVIEW"
            review_owner = "CODEX"
    elif "BAD_ECONOMICS" in flags:
        candidate_action = "REDUCE_TARGET_DRR"
        action_risk = "MEDIUM"
        decision_tier = "CODEX_REVIEW"
        review_owner = "CODEX"
    elif scale_signal:
        candidate_action = "INCREASE_BUDGET_CANDIDATE" if budget_limited else "INCREASE_TARGET_DRR_CANDIDATE"
        action_risk = "HIGH"
        decision_tier = "CODEX_REVIEW"
        review_owner = "CODEX"
    elif active_no_exposure:
        candidate_action = "DIAGNOSE_NO_EXPOSURE"
        action_risk = "LOW"
        decision_tier = "SYSTEM_DIAGNOSIS"
    elif "LOW_CTR" in flags:
        candidate_action = "OPTIMIZE_CREATIVE"
        action_risk = "LOW"
    elif {"LOW_ATC", "LOW_CVR"}.intersection(flags):
        candidate_action = "OPTIMIZE_LISTING"
        action_risk = "LOW"
    elif "HIGH_CART_LOW_ORDER" in flags:
        candidate_action = "CHECK_OFFER"
        action_risk = "LOW"
    else:
        candidate_action = "OBSERVE" if primary_state != "HEALTHY" else "HOLD"
        action_risk = "LOW"

    first_order_quality_ok = any(
        _quality_status(thresholds, field) == "AVAILABLE"
        for field in ("stop_click_threshold", "stop_spend_threshold")
    )
    scale_quality_ok = all(
        _quality_status(thresholds, field) == "AVAILABLE"
        for field in ("typical_impressions", "min_click_sample", "min_order_sample", "healthy_cvr")
    )
    data_quality_ok = item.impressions >= item.clicks >= 0 and min(item.add_to_cart, item.sold_qty, item.ad_spend, item.ad_sales) >= 0
    cooldown_days = int(ceil(float(cold_days))) if cold_days is not None else None
    recent_change = False
    for target in item.campaign_targets:
        raw_changed = target.get("last_change_time")
        if not raw_changed or cooldown_days is None:
            continue
        changed = datetime.fromisoformat(str(raw_changed).replace("Z", "+00:00")).date()
        if (item.analysis_as_of - changed).days < cooldown_days:
            recent_change = True
            break
    maturity_ok = (
        item.sku.data_maturity in {"LEVEL_3", "LEVEL_4", "LEVEL_5"}
        if stop_candidate
        else item.sku.data_maturity in {"LEVEL_4", "LEVEL_5"}
        if scale_signal
        else True
    )
    observation_window_ok = bool(
        item.first_order_history_quality == "COMPLETE"
        or (
            item.observed_history_span_days >= int(ceil(float(cold_days or ANALYSIS_WINDOW_DAYS)))
            and (enough_stop_clicks or enough_stop_spend)
        )
    )
    safety_gate = {
        "data_freshness": item.data_freshness == "FRESH" or active_no_exposure,
        "data_maturity": maturity_ok,
        "cohort_sample_quality": (
            first_order_quality_ok if stop_candidate
            else scale_quality_ok if scale_signal
            else _quality_status(thresholds, "min_click_sample") == "AVAILABLE" if "BAD_ECONOMICS" in flags
            else cohort_size >= QUANTILE_MIN_SAMPLES["p25"]
        ),
        "first_order_threshold_quality": first_order_quality_ok if stop_candidate else True,
        "survival_estimate_quality": survival["model_status"] == "AVAILABLE" if stop_candidate else True,
        "cold_start_protection": "COLD_START" not in flags,
        "financial_confidence": financial["financial_confidence"] in {"MEDIUM", "HIGH"},
        "financial_safe_to_scale": financial_safe_to_scale if scale_signal else True,
        "recent_configuration_change_cooldown": not recent_change,
        "multi_campaign_conflict": not item.multi_campaign_conflict,
        "campaign_action_scope_available": bool(item.campaign_targets) if action_risk in {"HIGH", "MEDIUM"} else True,
        "data_quality": data_quality_ok,
        "observation_window_adequate": observation_window_ok if stop_candidate else True,
    }
    operational_checks = dict(safety_gate)
    high_risk_ready_for_review = all(operational_checks.values())
    recommendation = candidate_action
    if action_risk in {"HIGH", "MEDIUM"} and not high_risk_ready_for_review:
        recommendation = "REVIEW_REQUIRED"
        risk_flags.extend(key.upper() for key, passed in operational_checks.items() if not passed)
    if item.data_freshness != "FRESH" and not active_no_exposure:
        recommendation = "OBSERVE"
    risk_flags = list(dict.fromkeys(risk_flags))
    campaign_sku_actions = [{
        "scope": "CAMPAIGN_SKU_ACTION",
        "campaign_id": target["campaign_id"],
        "campaign_name": target["campaign_name"],
        "sku_id": item.sku.sku_id,
        "action": candidate_action,
        "current_target_drr": target["current_target_drr"],
        "proposed_target_drr": proposed_scale_target(target["current_target_drr"], financial)
        if candidate_action == "INCREASE_TARGET_DRR_CANDIDATE" else None,
        "decision_tier": decision_tier,
        "review_owner": "USER_CONFIRMATION",
        "auto_eligible": False,
        "review_required": True,
    } for target in item.campaign_targets]
    reason_by_state = {
        "NO_RECENT_DATA": "SKU 没有更新到全店统一分析窗口，禁止高风险动作。",
        "STALE_DATA": "SKU 数据已陈旧，禁止高风险动作。",
        "ACTIVE_NO_EXPOSURE": "SKU 在运行中的广告计划内但没有展示，需排查商品状态、预算、报价和目标费用约束。",
        "NO_METRIC_DATA": "SKU 没有可用广告指标，且当前未确认在运行中的广告计划。",
        "COLD_START": "仍处于数据驱动的首单保护期。",
        "RECENT_ZERO_ORDER_DECAY": "SKU 历史曾出单，但近 30 天零单且已超过当前有限测试额度。",
        "ZERO_ORDER_HIGH_CLICK": "近 30 天零单，点击已超过店铺首单 P80 动态参考。",
        "ZERO_ORDER_HIGH_SPEND": "近 30 天零单，消耗已超过店铺首单 P80 动态参考。",
        "LOW_IMPRESSIONS": "展示量低于同类成功 SKU 的历史下界。",
        "LOW_CTR": "展示充分，但点击率低于同类成功 SKU 的历史下界。",
        "LOW_ATC": "点击样本充分，但加购率偏低。",
        "HIGH_CART_LOW_ORDER": "加购信号存在，但加购后的成交能力偏弱。",
        "LOW_CVR": "点击样本充分，但成交率偏低。",
        "BAD_ECONOMICS": "广告可以成交，但实际费用率超过有效财务盈亏线。",
        "SCALE_CANDIDATE": "转化率置信区间和样本量支持放量候选。",
        "INSUFFICIENT_CLICK_SAMPLE": "点击样本不足，仅继续观察。",
        "OBSERVATION": "可用成功样本不足，当前只积累数据。",
        "HEALTHY": "未触发当前可用阈值的异常区间。",
    }
    evidence_strength, evidence_components = _evidence_strength(
        item, thresholds, cohort_size, financial, data_quality_ok
    )
    if stop_candidate and high_risk_ready_for_review:
        evidence_strength = "HIGH" if stop_evidence_count >= SYSTEM_STOP_MIN_EVIDENCE and financial["financial_confidence"] == "HIGH" else "MEDIUM"
    evidence_components["stop_evidence_count"] = str(stop_evidence_count)
    evidence_components["decision_tier"] = decision_tier
    return {
        "primary_state": primary_state,
        "secondary_flags": secondary_flags,
        "recommendation": recommendation,
        "candidate_action": candidate_action,
        "reason": reason_by_state[primary_state],
        "action_risk": action_risk,
        "auto_eligible": False,
        "review_required": True,
        "decision_tier": decision_tier,
        "review_owner": "USER_CONFIRMATION" if action_risk in {"HIGH", "MEDIUM"} else review_owner,
        "ready_for_shadow_action": high_risk_ready_for_review if action_risk in {"HIGH", "MEDIUM"} else True,
        "ready_for_execution": high_risk_ready_for_review if action_risk in {"HIGH", "MEDIUM"} else True,
        "stop_evidence_count": stop_evidence_count,
        "stop_evidence_flags": stop_evidence_flags,
        "budget_limited": budget_limited,
        "risk_flags": risk_flags,
        "safety_gate": safety_gate,
        "evidence_strength": evidence_strength,
        "evidence_strength_components": evidence_components,
        "confidence_compatibility": {"LOW": 0.25, "MEDIUM": 0.5, "HIGH": 0.75}[evidence_strength],
        "cvr_wilson_lower": _rounded(cvr_lower),
        "cvr_wilson_upper": _rounded(cvr_upper),
        "threshold_quality": thresholds.get("threshold_quality") or {},
        "action_scope": "SKU_DIAGNOSIS",
        "campaign_sku_actions": campaign_sku_actions,
        "multi_campaign_conflict": item.multi_campaign_conflict,
        "evidence": evidence,
        "financial": financial,
        "survival": survival,
    }


def analyze_store(session: Session, store_id: str) -> dict:
    store = session.get(Store, store_id)
    if not store or store.platform != "ozon":
        raise ValueError("OZON 店铺不存在")
    aggregates = aggregate_store(session, store_id)
    financial_policy = flatten_financial_policy(system_defaults(session).get("financial_policy"))
    session.execute(update(AdThresholdVersion).where(AdThresholdVersion.store_id == store_id, AdThresholdVersion.active.is_(True)).values(active=False))
    version = f"TV_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
    successes = [item for item in aggregates if effective_outcome(item) == "SUCCESS"]
    choices: dict[str, tuple[str, str, list[SkuAggregate]] | None] = {
        item.sku.id: _choose_cohort(item, successes) for item in aggregates
    }
    threshold_records: dict[tuple[str, str], AdThresholdVersion] = {}
    representatives: dict[tuple[str, str], SkuAggregate] = {}
    for item in aggregates:
        choice = choices[item.sku.id]
        if not choice:
            continue
        key, level, matched_successes = choice
        identity = (key, level)
        if identity in threshold_records:
            continue
        representatives[identity] = item
        all_matched = _matching(aggregates, key, level)
        learned = _with_first_order_fallback(item, _learn_thresholds(matched_successes, all_matched), aggregates)
        record = AdThresholdVersion(
            store_id=store_id,
            version=version,
            cohort_key=key,
            cohort_level=level,
            sample_size=len(all_matched),
            success_sample_size=len(matched_successes),
            window_days=ANALYSIS_WINDOW_DAYS,
            thresholds_json=dumps(learned),
            active=True,
        )
        session.add(record)
        threshold_records[identity] = record
    if not threshold_records:
        fallback_thresholds = _learn_thresholds(successes, aggregates)
        if aggregates:
            fallback_thresholds = _with_first_order_fallback(aggregates[0], fallback_thresholds, aggregates)
        fallback = AdThresholdVersion(
            store_id=store_id,
            version=version,
            cohort_key="全店",
            cohort_level="insufficient",
            sample_size=len(aggregates),
            success_sample_size=len(successes),
            window_days=ANALYSIS_WINDOW_DAYS,
            thresholds_json=dumps(fallback_thresholds),
            active=True,
        )
        session.add(fallback)
        threshold_records[("全店", "insufficient")] = fallback
    session.flush()

    for item in aggregates:
        choice = choices[item.sku.id]
        if choice:
            key, level, cohort_successes = choice
            threshold_record = threshold_records[(key, level)]
            thresholds = loads(threshold_record.thresholds_json, {})
            cohort_size = len(cohort_successes)
            cohort_items = _matching(aggregates, key, level)
        else:
            key, level, cohort_size = "全店", "insufficient", len(successes)
            threshold_record = threshold_records[("全店", "insufficient")]
            thresholds = loads(threshold_record.thresholds_json, {})
            cohort_items = aggregates
        decision = _decision(item, thresholds, cohort_size, financial_policy)
        metrics = {
            **item.metrics(),
            "primary_state": decision["primary_state"],
            "secondary_flags": decision["secondary_flags"],
            "candidate_action": decision["candidate_action"],
            "action_scope": decision["action_scope"],
            "campaign_sku_actions": decision["campaign_sku_actions"],
            "multi_campaign_conflict": decision["multi_campaign_conflict"],
            "action_risk": decision["action_risk"],
            "auto_eligible": False,
            "review_required": True,
            "decision_tier": decision["decision_tier"],
            "review_owner": decision["review_owner"],
            "ready_for_shadow_action": decision["ready_for_shadow_action"],
            "ready_for_execution": decision["ready_for_execution"],
            "stop_evidence_count": decision["stop_evidence_count"],
            "stop_evidence_flags": decision["stop_evidence_flags"],
            "budget_limited": decision["budget_limited"],
            "risk_flags": decision["risk_flags"],
            "safety_gate": decision["safety_gate"],
            "evidence_strength": decision["evidence_strength"],
            "evidence_strength_components": decision["evidence_strength_components"],
            "cohort": {
                "key": key,
                "level": level,
                "total_samples": len(cohort_items),
                "success_samples": cohort_size,
                "failure_samples": sum(1 for candidate in cohort_items if effective_outcome(candidate) == "FAILED"),
            },
            "threshold_quality": decision["threshold_quality"],
            "cvr_wilson_lower": decision["cvr_wilson_lower"],
            "cvr_wilson_upper": decision["cvr_wilson_upper"],
            "survival": decision["survival"],
        }
        current = session.scalar(select(AdSkuState).where(AdSkuState.ad_sku_id == item.sku.id))
        if not current:
            current = AdSkuState(ad_sku_id=item.sku.id)
            session.add(current)
        current.state = decision["primary_state"]
        current.confidence = decision["confidence_compatibility"]
        current.recommendation = decision["recommendation"]
        current.reason = decision["reason"]
        current.cohort_key = key
        current.cohort_level = level
        current.cohort_sample_size = cohort_size
        current.threshold_version_id = threshold_record.id
        current.metrics_json = dumps(metrics)
        current.evidence_json = dumps(decision["evidence"])
        current.financial_check_json = dumps(decision["financial"])
        session.add(AdSkuStateHistory(
            ad_sku_id=item.sku.id,
            state=decision["primary_state"],
            confidence=decision["confidence_compatibility"],
            recommendation=decision["recommendation"],
            reason=decision["reason"],
            cohort_key=key,
            threshold_version_id=threshold_record.id,
            metrics_json=current.metrics_json,
            evidence_json=current.evidence_json,
        ))
    session.commit()
    return {"store_id": store_id, "version": version, "analyzed": len(aggregates), "success_samples": len(successes)}


def _post_stop_outcomes(rows: list[AdDailyMetric], stop_date: date) -> dict[str, int | float | bool]:
    future = [row for row in rows if row.metric_date > stop_date]
    future_orders = sum(row.sold_qty for row in future)
    return {
        "future_spend_saved": round(sum(row.ad_spend for row in future), 2),
        "future_orders_lost": future_orders,
        "future_add_to_cart_lost": sum(row.add_to_cart for row in future),
        "false_kill": future_orders > 0,
    }


def _replay_maturity(item: SkuAggregate, min_click_sample: float | None) -> str:
    if item.impressions <= 0:
        return "LEVEL_0"
    if item.clicks <= 0:
        return "LEVEL_1"
    if min_click_sample is not None and item.clicks < min_click_sample:
        return "LEVEL_2"
    if item.historical_sold_qty <= 0:
        return "LEVEL_3"
    return "LEVEL_5" if item.historical_order_days > 1 else "LEVEL_4"


def _chronological_replay(session: Session, store_id: str, financial_policy: dict[str, float]) -> list[dict[str, Any]]:
    skus = session.scalars(select(AdSku).where(AdSku.store_id == store_id)).all()
    rows = session.scalars(
        select(AdDailyMetric)
        .where(AdDailyMetric.ad_sku_id.in_([sku.id for sku in skus]))
        .order_by(AdDailyMetric.metric_date)
    ).all() if skus else []
    if not rows:
        return []
    grouped: dict[str, list[AdDailyMetric]] = defaultdict(list)
    for row in rows:
        grouped[row.ad_sku_id].append(row)
    first_day = min(row.metric_date for row in rows)
    last_day = max(row.metric_date for row in rows)
    strategies = [
        {
            "strategy_id": f"P{int(quantile * 100)}_EVIDENCE_{minimum_evidence}",
            "first_order_quantile": quantile,
            "minimum_evidence": minimum_evidence,
        }
        for quantile in (0.80, 0.90, 0.95)
        for minimum_evidence in (2, 3)
    ]
    stop_events: dict[str, dict[str, dict[str, Any]]] = {strategy["strategy_id"]: {} for strategy in strategies}
    threshold_available_days: dict[str, int] = defaultdict(int)

    current_day = first_day
    while current_day <= last_day:
        visible_grouped = {
            sku_id: [row for row in history if row.metric_date <= current_day]
            for sku_id, history in grouped.items()
        }
        freshness_policy = _freshness_policy(visible_grouped)
        visible = [
            value for sku in skus
            if (value := _aggregate(
                sku, visible_grouped.get(sku.id, []), financial_policy, current_day, freshness_policy
            )) is not None
        ]
        _assign_success_eligibility(visible, financial_policy)
        formal_successes = [item for item in visible if effective_outcome(item) == "SUCCESS"]
        operational = _learn_thresholds(formal_successes, visible)
        first_order_complete = [item for item in visible if item.first_order_data_complete and item.first_order_date]
        for strategy in strategies:
            strategy_id = strategy["strategy_id"]
            quantile = float(strategy["first_order_quantile"])
            click_threshold, click_quality = _threshold_stat(
                (item.clicks_to_first_order for item in first_order_complete), quantile
            )
            spend_threshold, spend_quality = _threshold_stat(
                (item.spend_to_first_order for item in first_order_complete), quantile
            )
            if click_threshold is None and spend_threshold is None:
                continue
            threshold_available_days[strategy_id] += 1
            for item in visible:
                if item.sku.id in stop_events[strategy_id] or item.historical_sold_qty > 0:
                    continue
                maturity = _replay_maturity(item, operational.get("min_click_sample"))
                if item.data_freshness != "FRESH" or maturity not in {"LEVEL_3", "LEVEL_4", "LEVEL_5"}:
                    continue
                flags: list[str] = []
                if click_threshold is not None and item.clicks >= click_threshold:
                    flags.append("FIRST_ORDER_CLICK_THRESHOLD")
                if spend_threshold is not None and item.ad_spend >= spend_threshold:
                    flags.append("FIRST_ORDER_SPEND_THRESHOLD")
                if item.add_to_cart == 0:
                    flags.append("NO_CART_SIGNAL")
                low_atc = operational.get("low_atc_threshold")
                if low_atc is not None and item.atc_rate is not None and item.atc_rate < low_atc:
                    flags.append("LOW_ATC")
                if item.clicks > 0 and item.break_even_cvr is not None and (item.cvr or 0) < item.break_even_cvr:
                    flags.append("BELOW_BREAK_EVEN_CVR")
                if not {"FIRST_ORDER_CLICK_THRESHOLD", "FIRST_ORDER_SPEND_THRESHOLD"}.intersection(flags):
                    continue
                if len(flags) < int(strategy["minimum_evidence"]):
                    continue
                stop_events[strategy_id][item.sku.id] = {
                    "sku_id": item.sku.sku_id,
                    "stop_date": current_day.isoformat(),
                    "stop_clicks": item.clicks,
                    "stop_spend": round(item.ad_spend, 2),
                    "trigger_flags": flags,
                    "click_threshold": _rounded(click_threshold, 2),
                    "spend_threshold": _rounded(spend_threshold, 2),
                    "click_threshold_quality": click_quality,
                    "spend_threshold_quality": spend_quality,
                }
        current_day += timedelta(days=1)

    results: list[dict[str, Any]] = []
    final_zero_order_ids = {sku_id for sku_id, history in grouped.items() if sum(row.sold_qty for row in history) == 0}
    for strategy in strategies:
        strategy_id = strategy["strategy_id"]
        details = []
        for sku_record_id, event in stop_events[strategy_id].items():
            details.append({
                **event,
                **_post_stop_outcomes(grouped[sku_record_id], date.fromisoformat(event["stop_date"])),
            })
        stopped_ids = set(stop_events[strategy_id])
        false_kills = sum(1 for item in details if item["false_kill"])
        future_orders_lost = sum(int(item["future_orders_lost"]) for item in details)
        future_spend_saved = round(sum(float(item["future_spend_saved"]) for item in details), 2)
        false_keeps = len(final_zero_order_ids - stopped_ids)
        results.append({
            **strategy,
            "threshold_available_days": threshold_available_days[strategy_id],
            "stopped_sku_count": len(details),
            "false_kill_count": false_kills,
            "false_kill_rate": safe_div(false_kills, len(details)) or 0.0,
            "false_keep_count": false_keeps,
            "future_orders_lost": future_orders_lost,
            "future_spend_saved": future_spend_saved,
            "waste_saved": future_spend_saved,
            "replay_details": sorted(details, key=lambda item: (item["stop_date"], item["sku_id"])),
        })
    return results


def chronological_replay(session: Session, store_id: str) -> list[dict[str, Any]]:
    policy = flatten_financial_policy(system_defaults(session).get("financial_policy"))
    return _chronological_replay(session, store_id, policy)


def run_backtest(session: Session, store_id: str) -> AdBacktestRun:
    replay = chronological_replay(session, store_id)
    reference = next((item for item in replay if item["strategy_id"] == "P95_EVIDENCE_3"), None) or {
        "false_kill_count": 0, "false_keep_count": 0, "future_spend_saved": 0.0,
    }
    threshold = session.scalar(
        select(AdThresholdVersion).where(AdThresholdVersion.store_id == store_id, AdThresholdVersion.active.is_(True)).order_by(AdThresholdVersion.created_at.desc())
    )
    result = AdBacktestRun(
        store_id=store_id,
        threshold_version=threshold.version if threshold else "",
        sample_size=session.scalar(select(func.count()).select_from(AdSku).where(AdSku.store_id == store_id)) or 0,
        false_kill_count=int(reference["false_kill_count"]),
        false_keep_count=int(reference["false_keep_count"]),
        potential_savings=float(reference["future_spend_saved"]),
        summary_json=dumps({
            "method": "CHRONOLOGICAL_REPLAY",
            "future_leakage": False,
            "spend_counting": "ONLY_AFTER_STOP_DATE",
            "reference_strategy": "P95_EVIDENCE_3_LEGACY_COLUMNS_ONLY",
            "strategy_selection": "HUMAN_REQUIRED",
            "strategies": replay,
            "pareto_frontier": pareto_frontier(replay),
        }),
    )
    session.add(result)
    session.commit()
    session.refresh(result)
    return result


def pareto_frontier(strategies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    frontier = []
    for candidate in sorted(strategies, key=lambda item: (item["false_kill_rate"], -item["future_spend_saved"])):
        dominated = any(
            other["false_kill_rate"] <= candidate["false_kill_rate"]
            and other["future_spend_saved"] >= candidate["future_spend_saved"]
            and (
                other["false_kill_rate"] < candidate["false_kill_rate"]
                or other["future_spend_saved"] > candidate["future_spend_saved"]
            )
            for other in strategies
        )
        if not dominated:
            frontier.append({key: value for key, value in candidate.items() if key != "replay_details"})
    return frontier
