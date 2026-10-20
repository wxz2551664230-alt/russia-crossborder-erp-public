from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from erp.integrations.ozon_performance import OzonPerformanceClient
from erp.core.jsonutil import dumps, loads
from erp.core.security import vault
from erp.core.database import SessionLocal
from erp.models import AdAction, AdCampaign, AdCampaignSku, AdSku, Job, Store, SystemSetting
from erp.services.jobs import execute_job
from erp.services import ad_jobs  # noqa: F401 - register background handlers
from erp.services.ad_data_sync import sync_campaign_configuration, sync_seller_catalog
from erp.services.ad_finance import assess_financials
from erp.services.ad_optimization import _post_stop_outcomes, aggregate_store


def _store(client) -> str:
    response = client.post(
        "/api/v1/stores",
        json={
            "platform": "ozon",
            "name": "广告测试店",
            "external_account_id": "ad-test-client",
            "secrets": {"api_key": "secret"},
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_ad_sync_is_queued_without_calling_ozon_in_browser_request(client, monkeypatch):
    store_id = _store(client)
    calls = []
    monkeypatch.setattr("erp.api.routes.ad_optimization.sync_from_ozon", lambda **kwargs: calls.append(kwargs) or {"campaigns": 1})
    first = client.post(f"/api/v1/ad-optimization/jobs/sync?store_id={store_id}&days=30")
    second = client.post(f"/api/v1/ad-optimization/jobs/sync?store_id={store_id}&days=30")
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert second.json()["already_running"] is True
    assert calls == []
    with SessionLocal() as session:
        job = session.get(Job, first.json()["job_id"])
        assert job.type == "ad_optimization_sync"
        execute_job(session, job)
        assert job.status == "completed"
    assert len(calls) == 1


def test_campaign_sync_uses_prefetched_products(client):
    store_id = _store(client)
    class NoNetworkClient:
        def campaign_products(self, _campaign_id):
            raise AssertionError("campaign products must be fetched before database writes")
    with SessionLocal() as session:
        result = sync_campaign_configuration(
            session, session.get(Store, store_id), NoNetworkClient(),
            [{"id": "123", "title": "Test"}], product_snapshots={"123": []},
        )
        assert result["campaigns"] == 1
        assert result["campaign_products"] == 0


def test_ad_report_checkpoint_resumes_original_report(tmp_path, monkeypatch):
    performance = OzonPerformanceClient("client", "secret")
    submitted = []
    waited = []
    monkeypatch.setattr(performance, "submit_report", lambda batch, start, end, **kwargs: submitted.append(tuple(batch)) or "report-1")
    def wait(report_id, **kwargs):
        waited.append(report_id)
        if len(waited) == 1:
            raise RuntimeError("temporary timeout")
    monkeypatch.setattr(performance, "wait_report", wait)
    monkeypatch.setattr(performance, "download_report", lambda report_id: (b"csv content", "text/csv"))
    start, end = date(2026, 9, 1), date(2026, 9, 7)
    with pytest.raises(RuntimeError):
        list(performance.report_files(["1"], start, end, checkpoint_dir=tmp_path))
    assert list(performance.report_files(["1"], start, end, checkpoint_dir=tmp_path)) == [("1.csv", b"csv content")]
    assert list(performance.report_files(["1"], start, end, checkpoint_dir=tmp_path)) == [("1.csv", b"csv content")]
    assert submitted == [("1",)] and waited == ["report-1", "report-1"]


def test_ad_csv_import_runs_in_background(client):
    store_id = _store(client)
    response = client.post(
        "/api/v1/ad-optimization/imports/csv/background",
        data={"store_id": store_id},
        files={"file": ("history.csv", _csv(), "text/csv")},
    )
    assert response.status_code == 202, response.text
    with SessionLocal() as session:
        assert session.scalar(select(AdSku).where(AdSku.store_id == store_id)) is None
        job = session.get(Job, response.json()["job_id"])
        assert job.type == "ad_optimization_import"
        execute_job(session, job)
        assert job.status == "completed", job.error_message
        assert session.scalar(select(AdSku).where(AdSku.store_id == store_id)) is not None


def _csv() -> bytes:
    output = io.StringIO()
    fields = [
        "sku_id", "name", "category_path", "price", "live_date", "ad_start_date", "metric_date",
        "impressions", "clicks", "add_to_cart", "sold_qty", "ad_spend", "ad_sales", "target_drr",
        "review_count", "rating", "delivery_days", "break_even_drr", "outcome_label",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for index, orders in enumerate((4, 5, 6), start=1):
        writer.writerow({
            "sku_id": f"SUCCESS-{index}", "name": f"成功商品 {index}", "category_path": "家居/收纳",
            "price": 499, "live_date": "2026-08-01", "ad_start_date": "2026-08-02", "metric_date": "2026-09-01",
            "impressions": 1500, "clicks": 50, "add_to_cart": 10, "sold_qty": orders,
            "ad_spend": 180, "ad_sales": orders * 499, "target_drr": "7%", "review_count": 8,
            "rating": 4.7, "delivery_days": 9, "break_even_drr": "12%", "outcome_label": "SUCCESS",
        })
    writer.writerow({
        "sku_id": "FAILED-1", "name": "待停止商品", "category_path": "家居/收纳", "price": 499,
        "live_date": "2026-08-01", "ad_start_date": "2026-08-02", "metric_date": "2026-09-01",
        "impressions": 1500, "clicks": 50, "add_to_cart": 10, "sold_qty": 1, "ad_spend": 250,
        "ad_sales": 499, "target_drr": "7%", "review_count": 8, "rating": 4.7,
        "delivery_days": 9, "break_even_drr": "12%", "outcome_label": "FAILED",
    })
    return output.getvalue().encode("utf-8")


def _import_rows(client, store_id: str, rows: list[dict]) -> dict:
    output = io.StringIO()
    fields = sorted({key for row in rows for key in row})
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    response = client.post(
        "/api/v1/ad-optimization/imports/csv",
        data={"store_id": store_id},
        files={"file": ("rows.csv", output.getvalue().encode("utf-8"), "text/csv")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_store_analysis_as_of_marks_old_sku_no_recent_data(client):
    store_id = _store(client)
    _import_rows(client, store_id, [
        {"sku_id": "CURRENT", "metric_date": "2026-09-07", "impressions": 100, "clicks": 10, "sold_qty": 1},
        {"sku_id": "STALE", "metric_date": "2026-08-01", "ad_start_date": "2026-07-01", "impressions": 500, "clicks": 20, "sold_qty": 0},
    ])
    with SessionLocal() as session:
        items = {item.sku.sku_id: item for item in aggregate_store(session, store_id)}
    stale = items["STALE"]
    assert stale.analysis_as_of.isoformat() == "2026-09-07"
    assert stale.last_metric_date.isoformat() == "2026-08-01"
    assert stale.days_since_last_metric == 37
    assert stale.data_freshness == "NO_RECENT_DATA"
    assert stale.impressions == 0
    assert stale.age_days == 69

    overview = client.get(f"/api/v1/ad-optimization?store_id={store_id}").json()
    stale_state = next(item for item in overview["items"] if item["sku_id"] == "STALE")
    assert stale_state["recommendation"] == "OBSERVE"
    assert stale_state["metrics"]["data_freshness"] == "NO_RECENT_DATA"


def test_seller_catalog_uses_first_primary_image_when_api_returns_list(monkeypatch, client):
    store_id = _store(client)

    class FakeSeller:
        def products(self): return [{"sku": "IMAGE-SKU"}]
        def product_info(self, _skus): return [{"sku": "IMAGE-SKU", "primary_image": ["https://cdn.example/main.jpg"], "images": ["https://cdn.example/other.jpg"]}]
        def product_ratings(self, _skus): return []
        def analytics_by_sku(self, *_args): return []
        def category_tree(self, *_args): return []

    monkeypatch.setattr("erp.services.ad_data_sync.OzonSellerAdapter", lambda *_args, **_kwargs: FakeSeller())
    with SessionLocal() as session:
        store = session.get(Store, store_id)
        sync_seller_catalog(session, store)
        session.commit()
        sku = session.scalar(select(AdSku).where(AdSku.store_id == store_id, AdSku.sku_id == "IMAGE-SKU"))
        assert loads(sku.metadata_json, {})["image_url"] == "https://cdn.example/main.jpg"


def test_left_truncated_history_does_not_invent_first_order_metrics(client):
    store_id = _store(client)
    _import_rows(client, store_id, [
        {
            "sku_id": "LEFT-TRUNCATED", "metric_date": "2026-09-01", "ad_start_date": "2026-06-03",
            "impressions": 500, "clicks": 20, "sold_qty": 1, "ad_spend": 100,
        },
        {"sku_id": "AS-OF", "metric_date": "2026-09-07", "impressions": 10, "clicks": 1, "sold_qty": 0},
    ])
    with SessionLocal() as session:
        item = next(item for item in aggregate_store(session, store_id) if item.sku.sku_id == "LEFT-TRUNCATED")
    assert item.first_order_history_quality == "LEFT_TRUNCATED"
    assert item.first_order_data_complete is False
    assert item.first_order_date.isoformat() == "2026-09-01"
    assert item.clicks_to_first_order is None
    assert item.spend_to_first_order is None
    assert item.days_to_first_order is None


def test_single_order_is_positive_evidence_but_not_success_training_sample(client):
    store_id = _store(client)
    result = _import_rows(client, store_id, [{
        "sku_id": "ONE-ORDER", "metric_date": "2026-09-07", "ad_start_date": "2026-09-07",
        "impressions": 100, "clicks": 10, "sold_qty": 1, "ad_spend": 20, "ad_sales": 500,
    }])
    assert result["analysis"]["success_samples"] == 0
    overview = client.get(f"/api/v1/ad-optimization?store_id={store_id}").json()
    item = overview["items"][0]
    assert item["weak_label"] == "ORDERED"
    assert item["metrics"]["success_eligible"] is False
    assert item["metrics"]["first_order_data_complete"] is True


def test_ad_csv_import_learns_thresholds_without_executing(client):
    store_id = _store(client)
    response = client.post(
        "/api/v1/ad-optimization/imports/csv",
        data={"store_id": store_id},
        files={"file": ("history.csv", _csv(), "text/csv")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["imported"] == 4

    overview = client.get(f"/api/v1/ad-optimization?store_id={store_id}").json()
    assert overview["mode"] == "live_reviewed"
    assert overview["summary"]["sku_count"] == 4
    assert overview["thresholds"][0]["success_sample_size"] == 3
    assert overview["thresholds"][0]["values"]["low_cvr_threshold"] is None
    assert overview["thresholds"][0]["values"]["stop_click_threshold"] is None
    assert overview["thresholds"][0]["values"]["threshold_quality"]["stop_click_threshold"]["status"] == "INSUFFICIENT_TAIL_SAMPLE"
    failed = next(item for item in overview["items"] if item["sku_id"] == "FAILED-1")
    assert failed["recommendation"] != "STOP_ADS"

    proposed = client.post("/api/v1/ad-optimization/actions", json={"state_id": failed["id"]})
    assert proposed.status_code == 201
    assert proposed.json()["executed"] is False

    backtest = client.post(f"/api/v1/ad-optimization/backtests?store_id={store_id}")
    assert backtest.status_code == 201
    assert backtest.json()["sample_size"] == 4
    assert backtest.json()["false_kill_count"] == 0


def test_insufficient_history_never_invents_soft_thresholds(client):
    store_id = _store(client)
    rows = _csv().decode("utf-8").splitlines()
    one_row = "\n".join(rows[:2]).encode("utf-8")
    response = client.post(
        "/api/v1/ad-optimization/imports/csv",
        data={"store_id": store_id},
        files={"file": ("one.csv", one_row, "text/csv")},
    )
    assert response.status_code == 201
    overview = client.get(f"/api/v1/ad-optimization?store_id={store_id}").json()
    assert overview["items"][0]["state"] == "OBSERVATION"
    assert overview["thresholds"][0]["cohort_level"] == "insufficient"
    assert overview["thresholds"][0]["values"]["low_cvr_threshold"] is None
    assert overview["thresholds"][0]["values"]["threshold_quality"]["low_cvr_threshold"]["status"] == "INSUFFICIENT_SAMPLE"


def test_three_successes_cannot_produce_p95_stop_threshold(client):
    store_id = _store(client)
    rows = []
    for index in range(3):
        rows.append({
            "sku_id": f"MANUAL-{index}", "metric_date": "2026-09-07", "ad_start_date": "2026-09-07",
            "impressions": 500, "clicks": 20 + index, "sold_qty": 1, "ad_spend": 30,
            "ad_sales": 500, "outcome_label": "SUCCESS",
        })
    _import_rows(client, store_id, rows)
    overview = client.get(f"/api/v1/ad-optimization?store_id={store_id}").json()
    threshold = overview["thresholds"][0]["values"]
    assert threshold["quantile_min_samples"]["p95"] == 60
    assert threshold["stop_click_threshold"] is None
    assert threshold["stop_spend_threshold"] is None
    assert threshold["threshold_quality"]["stop_click_threshold"]["status"] == "INSUFFICIENT_TAIL_SAMPLE"


def test_backtest_savings_start_after_stop_date():
    start = date(2026, 9, 1)
    rows = [
        SimpleNamespace(
            metric_date=start + timedelta(days=index),
            ad_spend=10.0,
            sold_qty=1 if index == 11 else 0,
            add_to_cart=1 if index == 10 else 0,
        )
        for index in range(12)
    ]
    outcome = _post_stop_outcomes(rows, date(2026, 9, 10))
    assert outcome["future_spend_saved"] == 20.0
    assert outcome["future_orders_lost"] == 1
    assert outcome["future_add_to_cart_lost"] == 1
    assert outcome["false_kill"] is True


def _manual_success_rows(count: int) -> list[dict]:
    return [{
        "sku_id": f"BENCHMARK-{index}", "metric_date": "2026-09-07", "ad_start_date": "2026-09-07",
        "category_path": "测试类目", "price": 500, "impressions": 1000, "clicks": 50,
        "add_to_cart": 10, "sold_qty": 5, "ad_spend": 100, "ad_sales": 2500,
        "outcome_label": "SUCCESS",
    } for index in range(count)]


def test_high_impressions_low_clicks_is_low_ctr_not_traffic_starved(client):
    store_id = _store(client)
    rows = _manual_success_rows(12)
    rows.append({
        "sku_id": "LOW-CTR-TARGET", "metric_date": "2026-09-07", "category_path": "测试类目",
        "price": 500, "impressions": 10000, "clicks": 5, "add_to_cart": 0, "sold_qty": 0,
        "ad_spend": 50, "ad_sales": 0,
    })
    _import_rows(client, store_id, rows)
    item = next(row for row in client.get(f"/api/v1/ad-optimization?store_id={store_id}").json()["items"] if row["sku_id"] == "LOW-CTR-TARGET")
    assert item["primary_state"] == "LOW_CTR"
    assert "INSUFFICIENT_CLICK_SAMPLE" in item["secondary_flags"]
    assert item["state"] != "TRAFFIC_STARVED"


def test_one_click_one_order_cannot_trigger_scale(client):
    store_id = _store(client)
    rows = _manual_success_rows(12)
    rows.append({
        "sku_id": "ONE-OF-ONE", "metric_date": "2026-09-07", "ad_start_date": "2026-09-07",
        "category_path": "测试类目", "price": 500, "impressions": 100, "clicks": 1,
        "add_to_cart": 1, "sold_qty": 1, "ad_spend": 5, "ad_sales": 500,
    })
    _import_rows(client, store_id, rows)
    item = next(row for row in client.get(f"/api/v1/ad-optimization?store_id={store_id}").json()["items"] if row["sku_id"] == "ONE-OF-ONE")
    assert item["candidate_action"] != "INCREASE_TARGET_DRR_CANDIDATE"
    assert item["recommendation"] != "INCREASE_TARGET_DRR_CANDIDATE"
    assert item["metrics"]["cvr_wilson_lower"] < 0.5


def test_high_risk_zero_order_signal_becomes_primary_state(client):
    store_id = _store(client)
    rows = _manual_success_rows(60)
    rows.append({
        "sku_id": "MULTI-FLAG", "metric_date": "2026-09-07", "category_path": "测试类目",
        "price": 500, "impressions": 100000, "clicks": 500, "add_to_cart": 0,
        "sold_qty": 0, "ad_spend": 1000, "ad_sales": 0,
    })
    _import_rows(client, store_id, rows)
    item = client.get(f"/api/v1/ad-optimization?store_id={store_id}&search=MULTI-FLAG").json()["items"][0]
    assert item["primary_state"] == "ZERO_ORDER_HIGH_SPEND"
    assert "ZERO_ORDER_HIGH_CLICK" in item["secondary_flags"]
    assert "LOW_CTR" in item["secondary_flags"]
    assert item["candidate_action"] == "STOP_ADS_CANDIDATE"
    assert item["decision_tier"] == "SYSTEM_HIGH_CONFIDENCE"
    assert item["stop_evidence_count"] >= 3
    assert item["recommendation"] == "REVIEW_REQUIRED"
    assert item["auto_eligible"] is False
    assert "CAMPAIGN_ACTION_SCOPE_AVAILABLE" in item["risk_flags"]


def test_batch_execution_requires_confirmation_and_audits_success(client, monkeypatch):
    store_id = _store(client)
    rows = _manual_success_rows(60)
    rows.append({
        "sku_id": "EXECUTE-STOP", "metric_date": "2026-09-07", "ad_start_date": "2026-08-01",
        "category_path": "测试类目", "price": 500, "impressions": 100000, "clicks": 500,
        "add_to_cart": 0, "sold_qty": 0, "ad_spend": 1000, "ad_sales": 0,
    })
    _import_rows(client, store_id, rows)
    with SessionLocal() as session:
        sku = session.scalar(select(AdSku).where(AdSku.store_id == store_id, AdSku.sku_id == "EXECUTE-STOP"))
        campaign = AdCampaign(store_id=store_id, campaign_id="EXEC-CAMPAIGN", name="执行计划", state="CAMPAIGN_STATE_RUNNING")
        session.add(campaign)
        session.flush()
        session.add(AdCampaignSku(
            ad_campaign_id=campaign.id, sku_id=sku.sku_id, target_drr=0.05, status="CAMPAIGN_STATE_RUNNING",
        ))
        session.commit()
    client.post(f"/api/v1/ad-optimization/analyze?store_id={store_id}")
    before = client.get(f"/api/v1/ad-optimization?store_id={store_id}&search=EXECUTE-STOP").json()["items"][0]
    assert before["ready_for_execution"] is True, before["risk_flags"]

    calls = []
    monkeypatch.setattr(vault, "decrypt", lambda _value: {"performance_client_id": "id", "performance_client_secret": "secret"})
    monkeypatch.setattr(OzonPerformanceClient, "delete_campaign_products", lambda _self, campaign_id, skus: calls.append((campaign_id, skus)) or {"ok": True})

    rejected = client.post(
        f"/api/v1/ad-optimization/actions/execute-batch?store_id={store_id}",
        json={"action": "STOP_ADS_CANDIDATE", "confirmed": False, "state_ids": [before["id"]]},
    )
    assert rejected.status_code == 409
    assert calls == []

    preview = client.post(f"/api/v1/ad-optimization/actions/preview?store_id={store_id}", json={"action": "STOP_ADS_CANDIDATE", "state_ids": [before["id"]]})
    assert preview.status_code == 200, preview.text
    approval_id = preview.json()["approval_id"]
    executed = client.post(
        f"/api/v1/ad-optimization/actions/execute-batch?store_id={store_id}",
        json={"action": "STOP_ADS_CANDIDATE", "confirmed": True, "approval_id": approval_id, "state_ids": [before["id"]]},
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["succeeded"] == 1
    assert calls == [("EXEC-CAMPAIGN", ["EXECUTE-STOP"])]
    with SessionLocal() as session:
        audit = session.scalar(select(AdAction).where(AdAction.ad_sku_id == sku.id))
        config = session.scalar(select(AdCampaignSku).where(AdCampaignSku.sku_id == "EXECUTE-STOP"))
        assert audit.status == "executed"
        assert config.status == "REMOVED_BY_ERP"
    overview = client.get(f"/api/v1/ad-optimization?store_id={store_id}&search=EXECUTE-STOP").json()
    assert overview["items"] == []


def test_multi_campaign_targets_remain_exact_and_block_high_risk_action(client):
    store_id = _store(client)
    rows = _manual_success_rows(60)
    rows.append({
        "sku_id": "MULTI-CAMPAIGN", "metric_date": "2026-09-07", "ad_start_date": "2026-09-07",
        "category_path": "测试类目", "price": 500, "impressions": 100000, "clicks": 500,
        "add_to_cart": 0, "sold_qty": 0, "ad_spend": 1000, "ad_sales": 0, "target_drr": "7%",
    })
    _import_rows(client, store_id, rows)
    with SessionLocal() as session:
        sku = session.scalar(select(AdSku).where(AdSku.store_id == store_id, AdSku.sku_id == "MULTI-CAMPAIGN"))
        first = AdCampaign(store_id=store_id, campaign_id="A", name="Campaign A", state="CAMPAIGN_STATE_RUNNING")
        second = AdCampaign(store_id=store_id, campaign_id="B", name="Campaign B", state="CAMPAIGN_STATE_RUNNING")
        session.add_all((first, second))
        session.flush()
        session.add_all((
            AdCampaignSku(ad_campaign_id=first.id, sku_id=sku.sku_id, target_drr=0.05, status="CAMPAIGN_STATE_RUNNING"),
            AdCampaignSku(ad_campaign_id=second.id, sku_id=sku.sku_id, target_drr=0.09, status="CAMPAIGN_STATE_RUNNING"),
        ))
        session.commit()

    analyzed = client.post(f"/api/v1/ad-optimization/analyze?store_id={store_id}")
    assert analyzed.status_code == 200, analyzed.text
    item = client.get(f"/api/v1/ad-optimization?store_id={store_id}&search=MULTI-CAMPAIGN").json()["items"][0]
    assert item["target_drr"] == pytest.approx(0.07)
    assert item["multi_campaign_conflict"] is True
    assert item["candidate_action"] == "STOP_ADS_CANDIDATE"
    assert item["recommendation"] == "REVIEW_REQUIRED"
    assert item["safety_gate"]["multi_campaign_conflict"] is False
    assert [action["current_target_drr"] for action in item["campaign_sku_actions"]] == [0.05, 0.09]
    assert all(action["proposed_target_drr"] is None for action in item["campaign_sku_actions"])
    assert all(action["review_required"] is True and action["auto_eligible"] is False for action in item["campaign_sku_actions"])


def test_running_campaign_sku_without_metrics_is_diagnosed_as_no_exposure(client):
    store_id = _store(client)
    with SessionLocal() as session:
        sku = AdSku(store_id=store_id, sku_id="NO-EXPOSURE", name="无曝光商品", price=500)
        campaign = AdCampaign(
            store_id=store_id,
            campaign_id="RUNNING-1",
            name="运行中计划",
            state="CAMPAIGN_STATE_RUNNING",
        )
        session.add_all((sku, campaign))
        session.flush()
        session.add(AdCampaignSku(
            ad_campaign_id=campaign.id,
            sku_id=sku.sku_id,
            target_drr=0.05,
            status="CAMPAIGN_STATE_RUNNING",
        ))
        session.commit()

    analyzed = client.post(f"/api/v1/ad-optimization/analyze?store_id={store_id}")
    assert analyzed.status_code == 200, analyzed.text
    item = client.get(f"/api/v1/ad-optimization?store_id={store_id}&state=ACTIVE_NO_EXPOSURE").json()["items"][0]
    assert item["sku_id"] == "NO-EXPOSURE"
    assert item["primary_state"] == "ACTIVE_NO_EXPOSURE"
    assert item["candidate_action"] == "DIAGNOSE_NO_EXPOSURE"
    assert item["decision_tier"] == "SYSTEM_DIAGNOSIS"


def test_prior_order_does_not_protect_recent_zero_order_decay(client):
    store_id = _store(client)
    rows = _manual_success_rows(60)
    rows.extend((
        {
            "sku_id": "DECAYED", "metric_date": "2026-07-30", "ad_start_date": "2026-07-30",
            "category_path": "测试类目", "price": 500, "impressions": 100, "clicks": 10,
            "add_to_cart": 2, "sold_qty": 1, "ad_spend": 20, "ad_sales": 500,
        },
        {
            "sku_id": "DECAYED", "metric_date": "2026-09-07", "category_path": "测试类目",
            "price": 500, "impressions": 100000, "clicks": 500, "add_to_cart": 0,
            "sold_qty": 0, "ad_spend": 1000, "ad_sales": 0,
        },
    ))
    _import_rows(client, store_id, rows)
    item = client.get(f"/api/v1/ad-optimization?store_id={store_id}&search=DECAYED").json()["items"][0]
    assert item["primary_state"] == "RECENT_ZERO_ORDER_DECAY"
    assert item["candidate_action"] == "STOP_ADS_CANDIDATE"
    assert item["metrics"]["historical_sold_qty"] == 1
    assert item["metrics"]["sold_qty"] == 0


def test_unprofitable_high_conversion_sku_cannot_be_scaled(client):
    store_id = _store(client)
    rows = _manual_success_rows(60)
    rows.append({
        "sku_id": "HIGH-CVR-LOSS", "metric_date": "2026-09-07", "ad_start_date": "2026-09-07",
        "category_path": "测试类目", "price": 500, "impressions": 500, "clicks": 100,
        "add_to_cart": 50, "sold_qty": 20, "ad_spend": 600, "ad_sales": 1000,
    })
    _import_rows(client, store_id, rows)
    item = client.get(f"/api/v1/ad-optimization?store_id={store_id}&search=HIGH-CVR-LOSS").json()["items"][0]
    assert item["financial_check"]["financial_status"] == "FINANCIAL_DANGER"
    assert item["candidate_action"] == "REDUCE_TARGET_DRR"
    assert item["candidate_action"] != "INCREASE_TARGET_DRR_CANDIDATE"


def test_category_financial_fallback_drives_status_and_break_even_cvr(client):
    store_id = _store(client)
    with SessionLocal() as session:
        session.add(SystemSetting(
            key="defaults",
            value_json=dumps({"financial_policy": {"category_break_even_drr": {"测试类目": 0.18}}}),
        ))
        session.commit()
    _import_rows(client, store_id, [{
        "sku_id": "CATEGORY-FINANCE", "metric_date": "2026-09-07", "category_path": "测试类目",
        "price": 500, "impressions": 100, "clicks": 10, "sold_qty": 1,
        "ad_spend": 100, "ad_sales": 500,
    }])
    item = client.get(f"/api/v1/ad-optimization?store_id={store_id}&search=CATEGORY-FINANCE").json()["items"][0]
    financial = item["financial_check"]
    assert financial["source"] == "CATEGORY_DEFAULT"
    assert financial["effective_break_even_drr"] == pytest.approx(0.18)
    assert financial["financial_status"] == "FINANCIAL_DANGER"
    assert financial["estimated_min_remaining_margin"] == pytest.approx(-0.02)
    assert item["metrics"]["break_even_cvr"] == pytest.approx((100 / 10) / (500 * 0.18))


def test_survival_estimate_uses_complete_events_and_right_censoring(client):
    store_id = _store(client)
    rows = []
    for index, clicks in enumerate((10, 20, 30)):
        rows.append({
            "sku_id": f"EVENT-{index}", "metric_date": "2026-09-07", "ad_start_date": "2026-09-07",
            "impressions": 500, "clicks": clicks, "sold_qty": 1, "ad_spend": clicks * 2,
        })
    for index, clicks in enumerate((5, 15, 25)):
        rows.append({
            "sku_id": f"CENSORED-{index}", "metric_date": "2026-09-07", "ad_start_date": "2026-09-07",
            "impressions": 500, "clicks": clicks, "sold_qty": 0, "ad_spend": clicks * 2,
        })
    rows.append({
        "sku_id": "LEFT-OUT", "metric_date": "2026-09-07", "ad_start_date": "2026-08-01",
        "impressions": 500, "clicks": 40, "sold_qty": 0, "ad_spend": 80,
    })
    _import_rows(client, store_id, rows)
    item = client.get(f"/api/v1/ad-optimization?store_id={store_id}&search=CENSORED-0").json()["items"][0]
    survival = item["metrics"]["survival"]
    assert survival["model_status"] == "AVAILABLE"
    assert survival["eligible_sample_size"] == 6
    assert survival["event_count"] == 3
    assert survival["right_censored_count"] == 3
    assert survival["estimated_future_order_probability"] is not None
    assert survival["first_order_history_quality"] == "COMPLETE"


def test_parse_real_ozon_performance_csv_format():
    content = (";Кампания по продвижению товаров № 123, период 07.09.2026-07.09.2026\n"
               "День;sku;Название товара;Предельная цена, ₽;Показы;Клики;CTR, %;Добавления в корзину;Средняя стоимость клика, ₽;Расход, ₽;Продано товаров;Продажи в продвижении, ₽;Продано товаров модели;Продажи в продвижении с заказов модели, ₽;ДРР в продвижении, %;ДРР, %;Дата добавления\n"
               "07.09.2026;9988;测试商品;427,00;196;7;3,57;2;5,21;36,49;1;427,00;1;427,00;8,5;8,5;01.09.2026\n").encode("utf-8")
    rows = OzonPerformanceClient.parse_report(content)
    assert rows[0]["campaign_id"] == "123"
    assert rows[0]["sku_id"] == "9988"
    assert rows[0]["metric_date"].isoformat() == "2026-09-07"
    assert rows[0]["ad_spend"] == 36.49
    assert rows[0]["add_to_cart"] == 2
    assert rows[0]["direct_sold_qty"] == 1
    assert rows[0]["model_sold_qty"] == 1
    assert rows[0]["sold_qty"] == 2
    assert rows[0]["direct_ad_sales"] == 427.0
    assert rows[0]["model_ad_sales"] == 427.0
    assert rows[0]["ad_sales"] == 854.0


def test_seller_catalog_sync_keeps_aligned_order_windows(client, monkeypatch):
    store_id = _store(client)

    class FakeSeller:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def products():
            return [{"sku": 7788, "product_id": 9900}]

        @staticmethod
        def product_info(_skus):
            return [{"sku": 7788, "id": 9900, "name": "订单窗口商品", "price": "499"}]

        @staticmethod
        def product_ratings(_skus):
            return []

        @staticmethod
        def category_tree(_language):
            return []

        @staticmethod
        def analytics_by_sku(date_from, date_to):
            window_days = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1
            orders = {7: 1, 30: 4}.get(window_days, 7)
            return [{
                "dimensions": [{"id": "7788"}],
                "metric_names": ["ordered_units", "revenue", "cancellations", "returns", "delivered_units"],
                "metrics": [orders, orders * 499, 1, 0, max(orders - 1, 0)],
            }]

    monkeypatch.setattr("erp.services.ad_data_sync.OzonSellerAdapter", FakeSeller)
    with SessionLocal() as session:
        store = session.get(Store, store_id)
        result = sync_seller_catalog(session, store, days=62)
        session.commit()
        sku = session.scalar(select(AdSku).where(AdSku.store_id == store_id, AdSku.sku_id == "7788"))
        metadata = json.loads(sku.metadata_json)

    assert result["analytics_windows"] == 3
    assert metadata["seller_analytics_windows"]["7d"]["metrics"]["ordered_units"] == 1
    assert metadata["seller_analytics_windows"]["30d"]["metrics"]["ordered_units"] == 4
    assert metadata["seller_analytics_30d"]["ordered_units"] == 4
    assert metadata["seller_analytics_windows"]["quarter"]["date_to"] == (date.today() - timedelta(days=1)).isoformat()


def test_sync_from_ozon_aggregates_same_sku_across_campaigns(client, monkeypatch):
    store_id = _store(client)
    stored = client.get(f"/api/v1/stores/{store_id}")
    assert stored.status_code == 200
    date_to = date.today() - timedelta(days=1)
    date_from = date_to - timedelta(days=6)

    class FakePerformanceClient:
        include_second_campaign = True

        def __init__(self, *_args, **_kwargs):
            pass

        def campaigns(self):
            return [{"id": "101"}, {"id": "102"}] if self.include_second_campaign else [{"id": "101"}]

        def campaign_products(self, _campaign_id):
            return []

        def report_files(self, *_args):
            yield "101.csv", b"first"
            if self.include_second_campaign:
                yield "102.csv", b"second"

        @staticmethod
        def parse_report(content):
            campaign = "101" if content == b"first" else "102"
            return [{
                "campaign_id": campaign, "metric_date": date_to,
                "sku_id": "SYNC-1", "name": "同步商品", "price": 500.0,
                "impressions": 100, "clicks": 10, "add_to_cart": 2, "avg_cpc": 3.0,
                "ad_spend": 30.0, "sold_qty": 1, "ad_sales": 500.0, "ad_start_date": "01.09.2026",
            }]

    monkeypatch.setattr("erp.api.routes.ad_optimization.OzonPerformanceClient", FakePerformanceClient)
    monkeypatch.setattr("erp.api.routes.ad_optimization.sync_seller_catalog", lambda *_args, **_kwargs: {"products": 0})
    monkeypatch.setattr(vault, "decrypt", lambda _value: {"performance_client_id": "id", "performance_client_secret": "secret"})
    response = client.post(f"/api/v1/ad-optimization/sync?store_id={store_id}&days=30")
    assert response.status_code == 201, response.text
    assert response.json()["campaigns"] == 2
    assert response.json()["imported"] == 1
    assert response.json()["mode"] == "live_reviewed"
    assert response.json()["executed"] is False
    with SessionLocal() as session:
        sku = session.scalar(select(AdSku).where(AdSku.store_id == store_id, AdSku.sku_id == "SYNC-1"))
        sku.metadata_json = dumps({
            "seller_synced_at": "2026-09-08T01:00:00+00:00",
            "seller_analytics_windows": {
                "7d": {
                    "date_from": date_from.isoformat(),
                    "date_to": date_to.isoformat(),
                    "metrics": {"ordered_units": 5, "revenue": 2200, "cancellations": 1, "returns": 0, "delivered_units": 3},
                },
            },
        })
        session.commit()
    overview = client.get(f"/api/v1/ad-optimization?store_id={store_id}").json()
    assert overview["items"][0]["metrics"]["impressions"] == 200
    assert overview["items"][0]["metrics"]["ad_spend"] == 60.0

    dashboard = client.get(f"/api/v1/ad-optimization/dashboard?store_id={store_id}&period=7d")
    assert dashboard.status_code == 200
    payload = dashboard.json()
    assert payload["totals"]["ad_spend"] == 60.0
    assert payload["totals"]["ad_sales"] == 1000.0
    assert payload["totals"]["roas"] == 1000.0 / 60.0
    assert payload["funnel"]["impressions"] == 200
    assert payload["campaign_count"] == 2
    assert payload["profitability"]["planned_ad_drr"] == 0.10
    assert payload["profitability"]["estimated_break_even_drr"] == 0.25
    assert payload["profitability"]["financial_confidence"] == "MEDIUM"
    assert payload["order_overview"]["total_orders"] == 5
    assert payload["order_overview"]["ad_orders"] == 2
    assert payload["order_overview"]["estimated_organic_orders"] == 3
    assert payload["order_overview"]["cancellations"] == 1
    assert payload["order_overview"]["ad_order_share"] == pytest.approx(0.4)
    assert payload["sku_orders"][0]["order_profile"] == "MIXED"
    assert payload["sku_orders"][0]["ad_drr"] == pytest.approx(0.06)

    FakePerformanceClient.include_second_campaign = False
    refreshed = client.post(f"/api/v1/ad-optimization/sync?store_id={store_id}&days=30")
    assert refreshed.status_code == 201, refreshed.text
    dashboard = client.get(f"/api/v1/ad-optimization/dashboard?store_id={store_id}&period=7d")
    refreshed_payload = dashboard.json()
    assert refreshed_payload["totals"]["ad_spend"] == 60.0
    assert refreshed_payload["totals"]["ad_sales"] == 1000.0
    assert refreshed_payload["funnel"]["impressions"] == 200
    assert refreshed_payload["campaign_count"] == 2
    refreshed_overview = client.get(f"/api/v1/ad-optimization?store_id={store_id}").json()
    assert refreshed_overview["items"][0]["metrics"]["impressions"] == 200
    assert refreshed_overview["items"][0]["metrics"]["ad_spend"] == 60.0


@pytest.mark.parametrize(("actual_drr", "status", "remaining"), [
    (0.05, "PROFIT_HEALTHY", 0.15),
    (0.10, "PROFIT_HEALTHY", 0.15),
    (0.12, "PROFIT_ACCEPTABLE", 0.13),
    (0.15, "PROFIT_ACCEPTABLE", 0.10),
    (0.20, "GROWTH_COST", 0.05),
    (0.25, "STRATEGIC_TEST", 0.00),
    (0.30, "FINANCIAL_DANGER", -0.05),
])
def test_store_default_financial_policy(actual_drr, status, remaining):
    result = assess_financials(actual_drr)
    assert result["financial_status"] == status
    assert result["estimated_remaining_margin"] == pytest.approx(remaining)
    assert result["planned_ad_drr"] == 0.10
    assert result["break_even_drr"] == 0.25
    assert result["financial_model_source"] == "STORE_DEFAULT"
    assert result["financial_confidence"] == "MEDIUM"


def test_exact_sku_financial_model_overrides_store_default():
    result = assess_financials(0.18, exact_break_even_drr=0.17)
    assert result["financial_status"] == "FINANCIAL_DANGER"
    assert result["break_even_drr"] == 0.17
    assert result["financial_model_source"] == "SKU_EXACT"
    assert result["financial_confidence"] == "HIGH"


def test_order_weak_labels_are_positive_evidence_not_automatic_success(client):
    store_id = _store(client)
    output = io.StringIO()
    fields = [
        "sku_id", "metric_date", "impressions", "clicks", "add_to_cart", "sold_qty",
        "ad_spend", "ad_sales", "price",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for index in range(3):
        writer.writerow({
            "sku_id": f"AUTO-ORDER-{index}", "metric_date": "2026-09-07", "impressions": 1000,
            "clicks": 40 + index * 5, "add_to_cart": 8, "sold_qty": 1 + index,
            "ad_spend": 80, "ad_sales": 500 * (1 + index), "price": 500,
        })
    writer.writerow({
        "sku_id": "AUTO-ZERO", "metric_date": "2026-09-07", "impressions": 1800,
        "clicks": 120, "add_to_cart": 0, "sold_qty": 0, "ad_spend": 400, "ad_sales": 0, "price": 500,
    })
    response = client.post(
        "/api/v1/ad-optimization/imports/csv",
        data={"store_id": store_id},
        files={"file": ("weak.csv", output.getvalue().encode("utf-8"), "text/csv")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["analysis"]["success_samples"] == 1
    overview = client.get(f"/api/v1/ad-optimization?store_id={store_id}").json()
    ordered = [item for item in overview["items"] if item["sku_id"].startswith("AUTO-ORDER")]
    assert {item["weak_label"] for item in ordered} == {"ORDERED", "MULTI_ORDER"}
    assert all(item["data_maturity"] in {"LEVEL_4", "LEVEL_5"} for item in ordered)
    analysis = client.get(f"/api/v1/ad-optimization/analysis?store_id={store_id}")
    assert analysis.status_code == 200
    assert analysis.json()["mode"] == "live_reviewed"
    assert analysis.json()["review_required"] is True
    assert analysis.json()["first_order_sample_size"] == 0
    assert analysis.json()["survival"]["excluded_unknown"] == 4
    assert analysis.json()["survival"]["method"].startswith("Kaplan-Meier")
