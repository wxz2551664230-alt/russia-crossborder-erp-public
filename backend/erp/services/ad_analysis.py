from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from erp.core.jsonutil import loads
from erp.models import AdCampaign, AdCampaignDailyMetric, AdDailyMetric, AdSku, AdSkuState, AdThresholdVersion
from erp.services.ad_optimization import (
    SkuAggregate,
    _first_order_history_quality,
    aggregate_store,
    chronological_replay,
    pareto_frontier,
    percentile,
    safe_div,
)


QUANTILES = (0.25, 0.50, 0.75, 0.80, 0.90, 0.95)


@dataclass
class Timeline:
    sku: AdSku
    rows: list[AdDailyMetric]
    first_order_date: date | None
    clicks_to_first_order: int | None
    impressions_to_first_order: int | None
    carts_to_first_order: int | None
    spend_to_first_order: float | None
    days_to_first_order: int | None
    live_days_to_first_order: int | None
    first_order_history_quality: str

    @property
    def left_truncated(self) -> bool:
        return self.first_order_history_quality == "LEFT_TRUNCATED"

    @property
    def first_order_data_complete(self) -> bool:
        return self.first_order_history_quality == "COMPLETE"

    @property
    def total_clicks(self) -> int:
        return sum(row.clicks for row in self.rows)

    @property
    def total_spend(self) -> float:
        return sum(row.ad_spend for row in self.rows)

    @property
    def observed_days(self) -> int:
        if not self.rows:
            return 0
        origin = max(self.sku.ad_start_date.date(), self.rows[0].metric_date) if self.sku.ad_start_date else self.rows[0].metric_date
        return max((self.rows[-1].metric_date - origin).days + 1, 0)


def _timelines(session: Session, store_id: str) -> list[Timeline]:
    skus = session.scalars(select(AdSku).where(AdSku.store_id == store_id)).all()
    if not skus:
        return []
    rows = session.scalars(
        select(AdDailyMetric)
        .where(AdDailyMetric.ad_sku_id.in_([sku.id for sku in skus]))
        .order_by(AdDailyMetric.metric_date)
    ).all()
    grouped: dict[str, list[AdDailyMetric]] = defaultdict(list)
    for row in rows:
        grouped[row.ad_sku_id].append(row)
    analysis_as_of = max((row.metric_date for row in rows), default=date.today())
    result: list[Timeline] = []
    for sku in skus:
        history = grouped.get(sku.id, [])
        if not history:
            continue
        first_index = next((index for index, row in enumerate(history) if row.sold_qty > 0), None)
        first_date = history[first_index].metric_date if first_index is not None else None
        through_first = history[: first_index + 1] if first_index is not None else []
        history_quality = _first_order_history_quality(sku, history, analysis_as_of, first_date)
        complete = history_quality == "COMPLETE"
        ad_origin = max(sku.ad_start_date.date(), history[0].metric_date) if sku.ad_start_date else history[0].metric_date
        result.append(Timeline(
            sku=sku,
            rows=history,
            first_order_date=first_date,
            clicks_to_first_order=sum(row.clicks for row in through_first) if through_first and complete else None,
            impressions_to_first_order=sum(row.impressions for row in through_first) if through_first and complete else None,
            carts_to_first_order=sum(row.add_to_cart for row in through_first) if through_first and complete else None,
            spend_to_first_order=round(sum(row.ad_spend for row in through_first), 4) if through_first and complete else None,
            days_to_first_order=max((first_date - ad_origin).days + 1, 0) if first_date and complete else None,
            live_days_to_first_order=max((first_date - sku.live_date.date()).days + 1, 0) if first_date and complete and sku.live_date and sku.live_date.date() >= history[0].metric_date else None,
            first_order_history_quality=history_quality,
        ))
    return result


def _distribution(name: str, values: Iterable[float | int | None]) -> list[dict[str, Any]]:
    clean = [float(value) for value in values if value is not None]
    return [
        {"metric": name, "quantile": f"P{int(level * 100)}", "value": percentile(clean, level), "sample_size": len(clean)}
        for level in QUANTILES
    ]


def _survival(rows: list[tuple[int, bool]]) -> list[dict[str, Any]]:
    by_duration: dict[int, list[bool]] = defaultdict(list)
    for duration, event in rows:
        by_duration[max(int(duration), 0)].append(event)
    at_risk = len(rows)
    survival = 1.0
    result: list[dict[str, Any]] = []
    for duration in sorted(by_duration):
        flags = by_duration[duration]
        events = sum(flags)
        censored = len(flags) - events
        if at_risk:
            survival *= 1 - events / at_risk
        result.append({
            "duration": duration,
            "at_risk": at_risk,
            "events": events,
            "censored": censored,
            "survival_probability": round(survival, 6),
        })
        at_risk -= len(flags)
    return result


def _rank(value: float, values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(1 for item in values if item <= value) / len(values)


def _candidate(item: SkuAggregate, state: AdSkuState | None, kind: str, evidence: str, risk: float) -> dict[str, Any]:
    return {
        "sku_id": item.sku.sku_id,
        "name": item.sku.name,
        "category_path": item.sku.category_path,
        "candidate_type": kind,
        "data_maturity": item.sku.data_maturity,
        "weak_label": item.sku.weak_label,
        "impressions": item.impressions,
        "clicks": item.clicks,
        "add_to_cart": item.add_to_cart,
        "orders": item.sold_qty,
        "ad_spend": round(item.ad_spend, 2),
        "ctr": item.ctr,
        "atc_rate": item.atc_rate,
        "cvr": item.cvr,
        "actual_drr": item.actual_drr,
        "waste_risk": round(min(max(risk, 0), 1), 4),
        "recommendation": state.recommendation if state else "REVIEW_REQUIRED",
        "confidence": state.confidence if state else 0.0,
        "evidence": evidence,
    }


def _waste_candidates(session: Session, store_id: str, aggregates: list[SkuAggregate]) -> dict[str, list[dict[str, Any]]]:
    states = {
        item.ad_sku_id: item
        for item in session.scalars(
            select(AdSkuState).join(AdSku).where(AdSku.store_id == store_id)
        ).all()
    }
    zero = [item for item in aggregates if item.sold_qty == 0]
    click_values = [float(item.clicks) for item in zero]
    spend_values = [item.ad_spend for item in zero]
    impression_values = [float(item.impressions) for item in aggregates]
    ctr_values = [item.ctr for item in aggregates if item.ctr is not None and item.clicks > 0]
    atc_values = [item.atc_rate for item in aggregates if item.atc_rate is not None and item.clicks > 0]
    click_cut = percentile(click_values, 0.90) or 0
    spend_cut = percentile(spend_values, 0.90) or 0
    impression_cut = percentile(impression_values, 0.75) or 0
    ctr_cut = percentile(ctr_values, 0.25)
    atc_cut = percentile(atc_values, 0.75)

    high_click = [
        _candidate(item, states.get(item.sku.id), "ZERO_ORDER_HIGH_CLICK", f"0 单，点击 {item.clicks}，达到零订单 SKU 的 P90={click_cut:.1f}", _rank(item.clicks, click_values))
        for item in zero if item.clicks >= click_cut and item.clicks > 0
    ]
    high_spend = [
        _candidate(item, states.get(item.sku.id), "ZERO_ORDER_HIGH_SPEND", f"0 单，消耗 {item.ad_spend:.2f} ₽，达到零订单 SKU 的 P90={spend_cut:.2f} ₽", _rank(item.ad_spend, spend_values))
        for item in zero if item.ad_spend >= spend_cut and item.ad_spend > 0
    ]
    high_cart = [
        _candidate(item, states.get(item.sku.id), "HIGH_CART_LOW_ORDER", f"0 单，加购率 {item.atc_rate:.2%}，不低于全店有点击 SKU 的 P75={atc_cut:.2%}", _rank(item.atc_rate or 0, atc_values))
        for item in zero if atc_cut is not None and item.atc_rate is not None and item.atc_rate >= atc_cut and item.add_to_cart > 0
    ]
    low_ctr = [
        _candidate(item, states.get(item.sku.id), "HIGH_IMPRESSION_LOW_CTR", f"展示 {item.impressions} 达到 P75={impression_cut:.0f}，CTR {item.ctr:.2%} 不高于 P25={ctr_cut:.2%}", 1 - _rank(item.ctr or 0, ctr_values))
        for item in aggregates if ctr_cut is not None and item.impressions >= impression_cut and item.impressions > 0 and item.ctr is not None and item.ctr <= ctr_cut
    ]
    high_click.sort(key=lambda row: (row["clicks"], row["ad_spend"]), reverse=True)
    high_spend.sort(key=lambda row: (row["ad_spend"], row["clicks"]), reverse=True)
    high_cart.sort(key=lambda row: (row["add_to_cart"], row["clicks"]), reverse=True)
    low_ctr.sort(key=lambda row: (row["impressions"], -row["ctr"]), reverse=True)
    return {
        "zero_order_high_spend": high_spend,
        "zero_order_high_click": high_click,
        "high_cart_low_order": high_cart,
        "high_impression_low_ctr": low_ctr,
    }


def _value_at_stop(item: Timeline, dimension: str) -> int | float:
    if dimension == "clicks":
        return item.clicks_to_first_order if item.first_order_date else item.total_clicks
    if dimension == "spend":
        return item.spend_to_first_order if item.first_order_date else item.total_spend
    return item.days_to_first_order if item.first_order_date else item.observed_days


def _saved_after(item: Timeline, dimension: str, threshold: float) -> float:
    if item.first_order_date:
        return 0.0
    if dimension == "spend":
        return max(item.total_spend - threshold, 0)
    if dimension == "clicks":
        return item.total_spend * max(item.total_clicks - threshold, 0) / item.total_clicks if item.total_clicks else 0.0
    origin = max(item.sku.ad_start_date.date(), item.rows[0].metric_date) if item.sku.ad_start_date else item.rows[0].metric_date
    spend_before = sum(row.ad_spend for row in item.rows if (row.metric_date - origin).days + 1 <= threshold)
    return max(item.total_spend - spend_before, 0)


def _backtest(timelines: list[Timeline]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    timelines = [item for item in timelines if item.first_order_data_complete]
    ordered = [item for item in timelines if item.first_order_date]
    failed = [item for item in timelines if not item.first_order_date]
    total_failed_spend = sum(item.total_spend for item in failed)
    rows: list[dict[str, Any]] = []
    for dimension in ("clicks", "days", "spend"):
        candidates = sorted({float(_value_at_stop(item, dimension)) for item in timelines if _value_at_stop(item, dimension) > 0})
        for threshold in candidates:
            false_kills = sum(1 for item in ordered if float(_value_at_stop(item, dimension)) > threshold)
            stopped_failed = sum(1 for item in failed if float(_value_at_stop(item, dimension)) >= threshold)
            savings = sum(_saved_after(item, dimension, threshold) for item in failed)
            rows.append({
                "dimension": dimension,
                "threshold": threshold,
                "ordered_sample_size": len(ordered),
                "zero_order_sample_size": len(failed),
                "stopped_failed_skus": stopped_failed,
                "missed_failed_skus": len(failed) - stopped_failed,
                "false_kill_count": false_kills,
                "false_kill_rate": safe_div(false_kills, len(ordered)) or 0.0,
                "waste_saved": round(savings, 2),
                "waste_saved_rate": safe_div(savings, total_failed_spend) or 0.0,
            })
    pareto: list[dict[str, Any]] = []
    click_rows = [row for row in rows if row["dimension"] == "clicks"]
    for constraint in (0.01, 0.02, 0.05, 0.10):
        feasible = [row for row in click_rows if row["false_kill_rate"] <= constraint]
        best = max(feasible, key=lambda row: (row["waste_saved"], row["stopped_failed_skus"]), default=None)
        pareto.append({"false_kill_constraint": constraint, **(best or {"dimension": "clicks", "threshold": None, "false_kill_rate": None, "waste_saved": 0.0, "waste_saved_rate": 0.0, "stopped_failed_skus": 0, "missed_failed_skus": len(failed), "false_kill_count": 0})})
    return rows, pareto


def _coverage(items: list[AdSku], predicate) -> dict[str, Any]:
    covered = sum(1 for item in items if predicate(item))
    return {"covered": covered, "missing": len(items) - covered, "coverage": safe_div(covered, len(items))}


def build_ad_analysis(session: Session, store_id: str) -> dict[str, Any]:
    skus = session.scalars(select(AdSku).where(AdSku.store_id == store_id)).all()
    timelines = _timelines(session, store_id)
    aggregates = [item for item in aggregate_store(session, store_id) if item.has_metric_history]
    advertised = [item.sku for item in timelines]
    all_rows = [row for item in timelines for row in item.rows]
    campaign_rows = session.scalars(select(AdCampaignDailyMetric).where(AdCampaignDailyMetric.store_id == store_id)).all()
    campaign_count = session.scalar(select(func.count()).select_from(AdCampaign).where(AdCampaign.store_id == store_id)) or 0
    duplicate_keys = Counter((row.ad_sku_id, row.metric_date) for row in all_rows)
    attribution_keys = Counter((row.sku_id, row.metric_date) for row in campaign_rows)
    coverage = {
        "product_id": _coverage(advertised, lambda item: bool(item.product_id)),
        "name": _coverage(advertised, lambda item: bool(item.name)),
        "category": _coverage(advertised, lambda item: bool(item.category_path or item.category_id)),
        "price": _coverage(advertised, lambda item: item.price is not None),
        "live_date": _coverage(advertised, lambda item: item.live_date is not None),
        "buyer_review_count": _coverage(advertised, lambda item: item.review_count is not None),
        "buyer_rating": _coverage(advertised, lambda item: item.rating is not None),
        "delivery_days": _coverage(advertised, lambda item: item.delivery_days is not None),
        "target_drr": _coverage(advertised, lambda item: item.target_drr is not None),
        "exact_break_even_drr": _coverage(advertised, lambda item: item.break_even_drr is not None),
        "fulfillment_type": _coverage(advertised, lambda item: bool(item.fulfillment_type)),
        "content_rating": _coverage(advertised, lambda item: loads(item.metadata_json, {}).get("content_rating") is not None),
    }
    warnings: list[str] = []
    if coverage["exact_break_even_drr"]["coverage"] != 1:
        warnings.append("DATA_QUALITY_WARNING: 逐 SKU 精确成本不完整；财务判断优先使用类目盈亏线，仍缺失时使用 25% 店铺估算盈亏线并标记 MEDIUM。")
    if coverage["buyer_rating"]["coverage"] != 1:
        warnings.append("DATA_QUALITY_WARNING: Seller API 的 rating-by-sku 是商品卡内容质量分，不是买家星级；未写入 buyer rating。")
    if any(count > 1 for count in attribution_keys.values()):
        warnings.append("DATA_QUALITY_WARNING: 同一 SKU/日期可能出现在多个广告计划；SKU 日表已聚合，计划表保留原始归因以便审计。")
    left_truncated = sum(1 for item in timelines if item.left_truncated)
    unknown_first_order_history = sum(1 for item in timelines if item.first_order_history_quality == "UNKNOWN")
    if left_truncated:
        warnings.append(f"DATA_QUALITY_WARNING: {left_truncated} 个 SKU 的广告开始时间早于当前数据窗口；首单统计是窗口内首次观测订单，不等同于商品生命周期真实首单。")
    anomalies = {
        "negative_metric_rows": sum(1 for row in all_rows if min(row.impressions, row.clicks, row.add_to_cart, row.sold_qty, row.ad_spend, row.ad_sales) < 0),
        "clicks_over_impressions_rows": sum(1 for row in all_rows if row.clicks > row.impressions),
        "duplicate_sku_day_rows": sum(count - 1 for count in duplicate_keys.values() if count > 1),
        "multi_campaign_sku_day_keys": sum(1 for count in attribution_keys.values() if count > 1),
        "blank_campaign_rows": sum(1 for row in all_rows if not row.campaign_id),
        "left_truncated_skus": left_truncated,
        "unknown_first_order_history_skus": unknown_first_order_history,
    }
    complete_timelines = [item for item in timelines if item.first_order_data_complete]
    first_order_rows = [item for item in complete_timelines if item.first_order_date]
    distribution = (
        _distribution("clicks_to_first_order", (item.clicks_to_first_order for item in first_order_rows))
        + _distribution("days_to_first_order", (item.days_to_first_order for item in first_order_rows))
        + _distribution("impressions_to_first_order", (item.impressions_to_first_order for item in first_order_rows))
        + _distribution("carts_to_first_order", (item.carts_to_first_order for item in first_order_rows))
        + _distribution("spend_to_first_order", (item.spend_to_first_order for item in first_order_rows))
        + _distribution("live_days_to_first_order", (item.live_days_to_first_order for item in first_order_rows))
    )
    click_survival = _survival([(item.clicks_to_first_order or item.total_clicks, bool(item.first_order_date)) for item in complete_timelines])
    day_survival = _survival([(item.days_to_first_order or item.observed_days, bool(item.first_order_date)) for item in complete_timelines])
    candidates = _waste_candidates(session, store_id, aggregates)
    backtest_rows = chronological_replay(session, store_id)
    pareto = pareto_frontier(backtest_rows)
    states = session.scalars(select(AdSkuState).join(AdSku).where(AdSku.store_id == store_id)).all()
    diagnostics = [loads(item.metrics_json, {}) for item in states]
    current_stop_candidates = sum(1 for item in diagnostics if item.get("candidate_action") == "STOP_ADS_CANDIDATE")
    legacy_proxy_skus = {
        row["sku_id"]
        for key in ("zero_order_high_spend", "zero_order_high_click")
        for row in candidates[key]
    }
    threshold_versions = session.scalars(
        select(AdThresholdVersion)
        .where(AdThresholdVersion.store_id == store_id, AdThresholdVersion.active.is_(True))
        .order_by(AdThresholdVersion.cohort_level, AdThresholdVersion.cohort_key)
    ).all()
    threshold_quality: list[dict[str, Any]] = []
    for version in threshold_versions:
        values = loads(version.thresholds_json, {})
        for field, quality in (values.get("threshold_quality") or {}).items():
            threshold_quality.append({
                "cohort_key": version.cohort_key,
                "cohort_level": version.cohort_level,
                "field": field,
                "value": values.get(field),
                **quality,
            })
    maturity = Counter(item.data_maturity or "LEVEL_0" for item in advertised)
    weak_labels = Counter(item.weak_label or "UNLABELED" for item in advertised)
    totals = {
        field: sum(getattr(row, field) for row in all_rows)
        for field in ("impressions", "clicks", "add_to_cart", "sold_qty", "ad_spend", "ad_sales")
    }
    return {
        "mode": "live_reviewed",
        "review_required": True,
        "audit": {
            "date_from": min((row.metric_date for row in all_rows), default=None),
            "date_to": max((row.metric_date for row in all_rows), default=None),
            "catalog_sku_count": len(skus),
            "advertised_sku_count": len(timelines),
            "sku_day_rows": len(all_rows),
            "campaign_metric_rows": len(campaign_rows),
            "campaign_count": campaign_count or len({row.campaign_id for row in campaign_rows}),
            "coverage": coverage,
            "anomalies": anomalies,
            "warnings": warnings,
        },
        "overview": {**totals, "ctr": safe_div(totals["clicks"], totals["impressions"]), "cvr": safe_div(totals["sold_qty"], totals["clicks"]), "acos": safe_div(totals["ad_spend"], totals["ad_sales"]), "roas": safe_div(totals["ad_sales"], totals["ad_spend"])},
        "maturity": dict(sorted(maturity.items())),
        "weak_labels": dict(weak_labels.most_common()),
        "first_order_sample_size": len(first_order_rows),
        "first_order_distribution": distribution,
        "survival": {"clicks": click_survival, "days": day_survival, "eligible_sample_size": len(complete_timelines), "excluded_left_truncated": left_truncated, "excluded_unknown": unknown_first_order_history, "method": "Kaplan-Meier empirical estimator; complete zero-order histories are right-censored; left-truncated and unknown histories are excluded."},
        "candidates": candidates,
        "candidate_counts": {key: len(value) for key, value in candidates.items()},
        "decision_comparison": {
            "legacy_single_signal_proxy_count": len(legacy_proxy_skus),
            "current_stop_candidate_count": current_stop_candidates,
            "candidate_reduction": len(legacy_proxy_skus) - current_stop_candidates,
            "comparison_note": "旧版数量为高点击或高消耗零订单的单信号代理，不代表可信的历史自动停止结果。",
        },
        "threshold_quality": threshold_quality,
        "backtest": backtest_rows,
        "pareto": pareto,
        "automation_readiness": {
            "stop_ads": False,
            "increase_target_drr": False,
            "reason": "候选来自历史证据；当前采用正式逐批审批执行。无人值守停投和放量尚未开放，放量仍需独立时间回放验证。",
        },
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        if not fields:
            return
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return f"{value:,}" if isinstance(value, int) else str(value)


def write_ad_reports(session: Session, store_id: str, root: Path | None = None) -> dict[str, Any]:
    result = build_ad_analysis(session, store_id)
    base = root or Path(__file__).resolve().parents[3] / "reports"
    data_dir = base / "data"
    audit = result["audit"]
    overview = result["overview"]
    coverage_lines = "\n".join(
        f"| {name} | {value['covered']} | {value['missing']} | {_fmt((value['coverage'] or 0) * 100)}% |"
        for name, value in audit["coverage"].items()
    )
    warning_lines = "\n".join(f"- {item}" for item in audit["warnings"]) or "- 无"
    (base / "DATA_AUDIT.md").parent.mkdir(parents=True, exist_ok=True)
    (base / "DATA_AUDIT.md").write_text(
        "# Ozon 广告数据质量审计\n\n"
        f"- 数据范围：{audit['date_from']} 至 {audit['date_to']}\n"
        f"- 商品目录 SKU：{audit['catalog_sku_count']}\n"
        f"- 有广告数据 SKU：{audit['advertised_sku_count']}\n"
        f"- SKU/日记录：{audit['sku_day_rows']}\n"
        f"- 广告计划：{audit['campaign_count']}\n\n"
        "## 字段覆盖率\n\n| 字段 | 已覆盖 | 缺失 | 覆盖率 |\n|---|---:|---:|---:|\n"
        f"{coverage_lines}\n\n## 异常检查\n\n"
        + "\n".join(f"- {key}: {value}" for key, value in audit["anomalies"].items())
        + f"\n\n## 数据质量警告\n\n{warning_lines}\n\n"
        "日表由 Ozon Performance API 日粒度报表生成，不把累计值当日增量。窗口内首次订单发生日无法确定精确点击顺序，因此首单点击数是该日结束时的累计上界；窗口前已启动的广告属于左截断样本。\n",
        encoding="utf-8",
    )
    (base / "STORE_AD_OVERVIEW.md").write_text(
        "# 全店广告概览\n\n"
        f"- 展示：{_fmt(overview['impressions'])}\n- 点击：{_fmt(overview['clicks'])}\n- 加购：{_fmt(overview['add_to_cart'])}\n"
        f"- 广告订单：{_fmt(overview['sold_qty'])}\n- 消耗：{_fmt(overview['ad_spend'])} ₽\n- 归因销售额：{_fmt(overview['ad_sales'])} ₽\n"
        f"- CTR：{_fmt((overview['ctr'] or 0) * 100)}%\n- CVR：{_fmt((overview['cvr'] or 0) * 100)}%\n- DRR/ACOS：{_fmt((overview['acos'] or 0) * 100)}%\n\n"
        "当前为 LIVE / REVIEWED 正式执行模式。报告仅提供候选依据；实际变更需在 ERP 预览具体批次并确认。\n",
        encoding="utf-8",
    )
    distribution_lines = "\n".join(f"| {row['metric']} | {row['quantile']} | {_fmt(row['value'])} | {row['sample_size']} |" for row in result["first_order_distribution"])
    (base / "FIRST_ORDER_ANALYSIS.md").write_text(
        "# 首单分布分析\n\n"
        f"窗口内观测到订单的 SKU 样本：{result['first_order_sample_size']}。这里的首单是当前数据窗口内首次观测订单，日内点击数采用累计上界；左截断样本不能解释为商品生命周期真实首单。\n\n"
        f"| 指标 | 分位数 | 数值 | 样本量 |\n|---|---|---:|---:|\n{distribution_lines}\n",
        encoding="utf-8",
    )
    (base / "SURVIVAL_ANALYSIS.md").write_text(
        "# 生存分析\n\n使用 Kaplan-Meier 经验估计；已出单为事件，观察期内未出单为右删失，不把所有零订单 SKU 当永久失败。"
        f"\n\n- 点击曲线节点：{len(result['survival']['clicks'])}\n- 天数曲线节点：{len(result['survival']['days'])}\n",
        encoding="utf-8",
    )
    candidate_lines = "\n".join(f"- {key}: {count}" for key, count in result["candidate_counts"].items())
    (base / "WASTE_ANALYSIS.md").write_text(
        "# 广告浪费候选\n\n候选阈值来自当前店铺分位数，仅用于人工审核，不直接写回 Ozon。\n\n"
        f"{candidate_lines}\n",
        encoding="utf-8",
    )
    replay_lines = "\n".join(
        f"| {row['strategy_id']} | {row['threshold_available_days']} | {row['stopped_sku_count']} | {row['false_kill_count']} | {_fmt(row['false_kill_rate'] * 100)}% | {row['future_orders_lost']} | {_fmt(row['future_spend_saved'])} ₽ |"
        for row in result["backtest"]
    ) or "| 无可用策略 | 0 | 0 | 0 | 0.00% | 0 | 0.00 ₽ |"
    pareto_lines = "\n".join(
        f"| {row['strategy_id']} | {row['stopped_sku_count']} | {_fmt(row['false_kill_rate'] * 100)}% | {row['future_orders_lost']} | {_fmt(row['future_spend_saved'])} ₽ |"
        for row in result["pareto"]
    ) or "| 无可用策略 | 0 | 0.00% | 0 | 0.00 ₽ |"
    (base / "BACKTEST_REPORT.md").write_text(
        "# 逐日时间回放\n\n每天只使用当日及以前数据重新学习阈值并产生首次停止候选。后续消耗从停止日次日开始统计；后续出单记为 False Kill。策略必须人工选择。\n\n"
        f"| 策略 | 阈值可用天数 | 停止候选 | False Kill | False Kill 率 | 后续订单损失 | 停止后消耗 |\n|---|---:|---:|---:|---:|---:|---:|\n{replay_lines}\n",
        encoding="utf-8",
    )
    (base / "THRESHOLD_REPORT.md").write_text(
        "# 数据驱动阈值候选\n\n系统不硬编码运营淘汰阈值。Pareto 前沿来自无未来泄漏的逐日回放；最终经营偏好需要人工选择。\n\n"
        f"| 策略 | 停止候选 | False Kill 率 | 后续订单损失 | 停止后消耗 |\n|---|---:|---:|---:|---:|\n{pareto_lines}\n",
        encoding="utf-8",
    )
    comparison = result["decision_comparison"]
    anomaly_lines = "\n".join(f"- {key}: {value}" for key, value in audit["anomalies"].items())
    (base / "DATA_QUALITY_AFTER_FIX.md").write_text(
        "# 修复后广告数据质量\n\n"
        f"- 统一分析截止日：{audit['date_to']}\n"
        f"- 广告 SKU：{audit['advertised_sku_count']}\n"
        f"- SKU/日记录：{audit['sku_day_rows']}\n\n"
        "## 修复后的边界\n\n"
        "- 所有 SKU 使用同一 analysis_as_of 和 30 天窗口。\n"
        "- 左截断与历史不完整 SKU 不参与真实首单阈值训练。\n"
        "- 零订单完整历史作为右删失样本，不自动标记失败。\n"
        "- 同 SKU 多广告计划保留 Campaign+SKU 原始目标，不使用平均值执行。\n"
        "- rating-by-sku 仅保存在 content_rating 元数据，不写入买家评分。\n\n"
        f"## 当前异常\n\n{anomaly_lines}\n\n## 当前警告\n\n{warning_lines}\n\n"
        f"旧单信号代理候选 {comparison['legacy_single_signal_proxy_count']} 个；新版经组合证据与安全门的停止候选 {comparison['current_stop_candidate_count']} 个；变化 {comparison['candidate_reduction']} 个。"
        "该旧口径仅用于解释规则收紧，不等同于可信的旧版自动停止结果。\n",
        encoding="utf-8",
    )
    (base / "BACKTEST_V2.md").write_text(
        "# Chronological Replay Backtest V2\n\n"
        "回放按日推进，每天只使用当日及以前的历史。停止发生在首次满足候选条件的日期，Waste Saved 仅统计次日起的后续消耗。\n\n"
        f"| 策略 | 阈值可用天数 | 停止候选 | False Kill | False Kill 率 | 后续订单损失 | Waste Saved |\n|---|---:|---:|---:|---:|---:|---:|\n{replay_lines}\n\n"
        "旧回测使用完整历史回看过去，存在 future leakage，且曾把停止前花费计入节省，不能作为数值基线。新版数值是当前唯一有效回测口径。\n\n"
        "结论：不自动选择 P80/P90/P95 或证据数量，所有策略继续人工审阅。\n",
        encoding="utf-8",
    )
    quality_lines = "\n".join(
        f"| {row['cohort_level']} | {row['cohort_key']} | {row['field']} | {row.get('quantile', '')} | {row['sample_size']} / {row['minimum_sample_size']} | {row['status']} | {row.get('stability', 'UNAVAILABLE')} | {row.get('source_level', 'primary')} |"
        for row in result["threshold_quality"]
    ) or "| insufficient | 全店 | 无 | - | 0 / 0 | UNAVAILABLE | UNAVAILABLE | unavailable |"
    reliable = [row for row in result["threshold_quality"] if row.get("status") == "AVAILABLE"]
    insufficient = [row for row in result["threshold_quality"] if row.get("status") != "AVAILABLE"]
    store_fallback = [row for row in result["threshold_quality"] if row.get("source_level") == "store"]
    (base / "THRESHOLD_STABILITY.md").write_text(
        "# 阈值稳定性\n\n"
        "最低样本按尾部至少三个期望观测推导：P25=12、P50=6、P80=15、P90=30、P95=60；可用阈值再用确定性 bootstrap 评估稳定性。\n\n"
        f"- 当前可用阈值记录：{len(reliable)}\n- 当前样本不足阈值记录：{len(insufficient)}\n- 回退到 Store 的阈值记录：{len(store_fallback)}\n\n"
        f"| Cohort 层级 | Cohort | 字段 | 分位数 | 样本 / 最低要求 | 状态 | 稳定性 | 来源 |\n|---|---|---|---|---:|---|---|---|\n{quality_lines}\n",
        encoding="utf-8",
    )
    (base / "FALSE_KILL_WASTE_SAVED.md").write_text(
        "# False Kill 与 Waste Saved\n\n"
        f"| Pareto 策略 | 停止候选 | False Kill 率 | 后续订单损失 | Waste Saved |\n|---|---:|---:|---:|---:|\n{pareto_lines}\n\n"
        "## 修改前与修改后\n\n"
        "1. 旧版可能误判：陈旧 SKU、左截断首单、仅一单样本、极小样本 P95、高曝光低点击、1/1 CVR、多计划平均目标和财务回退不一致。\n"
        f"2. 停止候选：旧单信号代理 {comparison['legacy_single_signal_proxy_count']}，新版组合证据候选 {comparison['current_stop_candidate_count']}，减少 {comparison['candidate_reduction']}。\n"
        "3. False Kill：旧数值因未来泄漏无效；新版各策略见上表，不伪造不可比的下降百分比。\n"
        "4. Waste Saved：只统计 stop_date 次日以后消耗，各策略见上表。\n"
        f"5. 当前可靠阈值记录：{len(reliable)}，其状态均为 AVAILABLE，具体见 THRESHOLD_STABILITY.md。\n"
        f"6. 当前不足阈值记录：{len(insufficient)}，包括 INSUFFICIENT_SAMPLE / INSUFFICIENT_TAIL_SAMPLE。\n"
        f"7. 回退到 Store 的阈值记录：{len(store_fallback)}，具体 Cohort 与字段见 THRESHOLD_STABILITY.md。\n"
        "8. STOP_ADS：使用 P80 三重证据生成候选，通过安全门并完成批次审批后可正式执行；生成报告本身不执行变更。\n"
        "9. INCREASE_TARGET_DRR 无人值守自动化：尚缺独立时间回放验证。正式执行仍需满足财务边界、证据安全门和逐批审批。\n",
        encoding="utf-8",
    )
    for key, rows in result["candidates"].items():
        _write_csv(data_dir / f"{key}.csv", rows)
    _write_csv(data_dir / "first_order_distribution.csv", result["first_order_distribution"])
    _write_csv(data_dir / "backtest_results.csv", result["backtest"])
    _write_csv(data_dir / "threshold_candidates.csv", result["pareto"])
    _write_csv(data_dir / "threshold_stability.csv", result["threshold_quality"])
    _write_csv(data_dir / "survival_clicks.csv", result["survival"]["clicks"])
    _write_csv(data_dir / "survival_days.csv", result["survival"]["days"])
    return {"report_dir": str(base), "analysis": result}
