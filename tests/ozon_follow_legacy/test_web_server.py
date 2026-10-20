from __future__ import annotations

import unittest
import time
import json
import threading
import urllib.error
import urllib.request
import inspect
from decimal import Decimal
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from erp.modules.ozon_follow.legacy.config import AppConfig
from erp.modules.ozon_follow.legacy.models import ProductCandidate
from erp.modules.ozon_follow.legacy.storage import ProductRepository
from erp.modules.ozon_follow.legacy.web.server import (
    BackgroundJob,
    JobRegistry,
    LocalWebServer,
    WebApplication,
    _activate_promotion,
    _is_elastic_promotion,
    _promotion_action_price,
    _aggregate_store_status,
    _manually_complete_store_publication,
    _configured_ozon_stores,
    _migrate_store_publications,
    _update_draft,
    RequestHandler,
    _public_config,
    _extract_alibaba_variants,
    _normalize_1688_source,
)
from erp.modules.ozon_follow.legacy.services.ozon_listing import OzonListingDraft


class WebServerTests(unittest.TestCase):
    def test_ai_stage_prepares_a_saved_variant_for_every_store(self) -> None:
        stores = [
            {"id":"a", "name":"店铺A", "prefix":"AA", "client_id":"1", "api_key":"key-a", "api_base":"https://example.test"},
            {"id":"b", "name":"店铺B", "prefix":"BB", "client_id":"2", "api_key":"key-b", "api_base":"https://example.test"},
        ]
        config = AppConfig(ozon_stores_json=json.dumps(stores, ensure_ascii=False))
        saved = []

        class Repository:
            def save_listing_draft(self, sku, payload):
                saved.append((sku, payload))

        class Client:
            def category_attributes(self, *_args): return []

        handler = object.__new__(RequestHandler)
        handler.server = SimpleNamespace(app=SimpleNamespace(repository=Repository()))
        draft = OzonListingDraft(
            source_sku="sku-1", offer_id="AUTO-1", base_offer_id="AUTO-1",
            source_ozon_url="", title="主店文案", exported_category="类目",
            ai_preparation={"prepared_at":"now", "image_review_status":"APPROVED"},
            images_oss_ready=True, primary_image="https://oss.example/main.jpg",
        )

        def prepare_variant(**kwargs):
            publication = kwargs["draft"]
            publication.title = f"{publication.ai_preparation['content_variant_label']}文案"
            publication.primary_image = f"https://oss.example/{publication.offer_id}.jpg"
            publication.images_oss_ready = True
            publication.ai_preparation.update({"prepared_at":"now", "image_review_status":"APPROVED"})

        with (
            patch("erp.modules.ozon_follow.legacy.web.server._store_client", return_value=Client()),
            patch("erp.modules.ozon_follow.legacy.web.server.prepare_listing_with_ai", side_effect=prepare_variant) as prepare,
            patch("erp.modules.ozon_follow.legacy.web.server._apply_automatic_pricing"),
            patch("erp.modules.ozon_follow.legacy.web.server.enforce_system_attributes"),
            patch("erp.modules.ozon_follow.legacy.web.server.validate_listing_draft", return_value=[]),
        ):
            results = handler._prepare_store_variants(
                SimpleNamespace(message=""), draft, {"sku":"sku-1"}, {}, config
            )

        self.assertEqual([item["status"] for item in results], ["READY", "READY"])
        self.assertEqual(set(draft.store_publications), {"a", "b"})
        self.assertEqual(draft.store_publications["a"]["offer_id"], "AA-AUTO-1")
        self.assertEqual(draft.store_publications["b"]["offer_id"], "BB-AUTO-1")
        self.assertEqual(draft.store_publications["b"]["title"], "店铺B文案")
        self.assertEqual(prepare.call_count, 1)
        profile_a = draft.store_publications["a"]["draft"]["ai_preparation"]["store_creative_profile"]
        profile_b = draft.store_publications["b"]["draft"]["ai_preparation"]["store_creative_profile"]
        self.assertNotEqual(profile_a["profile_id"], profile_b["profile_id"])
        self.assertNotEqual(profile_a["composition"], profile_b["composition"])
        self.assertTrue(saved)

    def test_submit_branch_does_not_generate_ai_content_or_images(self) -> None:
        source = inspect.getsource(RequestHandler._listing_job)
        submit_branch = source.split('if action == "submit":', 1)[1].split('if action == "refresh":', 1)[0]
        self.assertNotIn("prepare_listing_with_ai", submit_branch)
        self.assertNotIn("prepare_main_image_from_checkpoint", submit_branch)

    def test_store_status_aggregates_partial_progress_without_rolling_back_successes(self) -> None:
        self.assertEqual(_aggregate_store_status({
            "a": {"status": "COMPLETED"},
            "b": {"status": "FAILED"},
        }), "PUBLISHED")
        self.assertEqual(_aggregate_store_status({
            "a": {"status": "IMPORT_COMPLETED_STOCK_PENDING"},
            "b": {"status": "FAILED"},
        }), "IMPORT_COMPLETED_STOCK_PENDING")
        self.assertEqual(_aggregate_store_status({
            "a": {"status": "FAILED"},
            "b": {"status": "FAILED"},
        }), "STORE_PUBLICATIONS_FAILED")

    def test_manually_completes_only_the_target_store_and_preserves_audit_data(self) -> None:
        draft = OzonListingDraft(
            source_sku="1", offer_id="AUTO-1", source_ozon_url="",
            title="商品", exported_category="类目",
            store_action_target="a",
            store_publications={
                "a": {
                    "store_id": "a", "store_name": "店铺A",
                    "status": "IMPORT_COMPLETED_STOCK_PENDING",
                    "draft": {"status": "IMPORT_COMPLETED_STOCK_PENDING"},
                },
                "b": {"store_id": "b", "status": "COMPLETED", "draft": {"status": "COMPLETED"}},
            },
        )
        record = _manually_complete_store_publication(draft, "a")
        self.assertEqual(record["status"], "COMPLETED")
        self.assertEqual(record["stage"], "MANUALLY_COMPLETED")
        self.assertEqual(record["completion_mode"], "MANUAL")
        self.assertEqual(record["manual_completion_from_status"], "IMPORT_COMPLETED_STOCK_PENDING")
        self.assertEqual(record["draft"]["status"], "COMPLETED")
        self.assertTrue(record["manual_completed_at"])
        self.assertEqual(draft.store_publications["b"]["status"], "COMPLETED")
        self.assertEqual(draft.status, "COMPLETED")
        self.assertEqual(draft.store_action_target, "")

    def test_manual_completion_rejects_a_store_before_ozon_import(self) -> None:
        draft = OzonListingDraft(
            source_sku="1", offer_id="AUTO-1", source_ozon_url="",
            title="商品", exported_category="类目",
            store_publications={"a": {"status": "READY", "draft": {"status": "READY"}}},
        )
        with self.assertRaisesRegex(ValueError, "不在待添加库存或待添加促销阶段"):
            _manually_complete_store_publication(draft, "a")

    def test_legacy_published_draft_is_migrated_to_primary_store_task(self) -> None:
        draft = OzonListingDraft(
            source_sku="1", offer_id="OLD-1", source_ozon_url="", title="商品",
            exported_category="类目", status="IMPORT_COMPLETED_STOCK_PENDING", task_id=77,
        )
        changed = _migrate_store_publications(draft, AppConfig(
            ozon_store_name="主店", ozon_store_prefix="MAIN",
        ))
        self.assertTrue(changed)
        record = draft.store_publications["primary"]
        self.assertEqual(record["store_name"], "主店")
        self.assertEqual(record["task_id"], 77)
        self.assertEqual(record["draft"]["store_publications"], {})

    def test_arbitrary_store_configuration_migrates_legacy_fields(self) -> None:
        stores = _configured_ozon_stores(AppConfig(
            multi_store_enabled="1", ozon_store_name="A", ozon_store_prefix="AA",
            ozon_secondary_store_name="B", ozon_secondary_store_prefix="BB",
        ))
        self.assertEqual([item["id"] for item in stores], ["primary", "secondary"])

    def test_store_action_settings_are_persisted_by_draft_update(self) -> None:
        with TemporaryDirectory() as directory:
            repository = ProductRepository(Path(directory) / "products.db")
            repository.upsert_many([ProductCandidate(
                source_file="test.xlsx", source_row=2, sku="sku-1", title="商品", category="类目",
            )])
            draft = OzonListingDraft(
                source_sku="sku-1", offer_id="AUTO-1", source_ozon_url="",
                title="商品", exported_category="类目",
            )
            repository.save_listing_draft("sku-1", draft.to_dict())
            updated = _update_draft(repository, "sku-1", {
                "store_action_target": "store-b",
                "store_inventory": {"store-b": {"warehouse_id": 12, "stock": 8}},
                "store_promotions": {"store-b": {"action_id": 99}},
            })
            self.assertEqual(updated.store_action_target, "store-b")
            self.assertEqual(updated.store_inventory["store-b"]["stock"], 8)
            self.assertEqual(updated.store_promotions["store-b"]["action_id"], 99)

    def test_normalizes_manual_1688_variant_source_url(self) -> None:
        url, item_id = _normalize_1688_source(
            "https://detail.1688.com/offer/1061176060847.html?spm=test",
            "old-id",
        )
        self.assertEqual(item_id, "1061176060847")
        self.assertEqual(url, "https://detail.1688.com/offer/1061176060847.html")

        url, item_id = _normalize_1688_source("", "1061176060847")
        self.assertEqual(item_id, "1061176060847")
        self.assertEqual(url, "https://detail.1688.com/offer/1061176060847.html")

    def test_extracts_nested_alibaba_variants(self) -> None:
        payload = {"result": {"skuInfos": [{
            "skuId": 123, "attributes": [
                {"attributeName": "颜色", "attributeValue": "红色"},
                {"attributeName": "容量", "attributeValue": "200ml"},
            ], "price": "12.50",
        }]}}
        self.assertEqual(_extract_alibaba_variants(payload), [{
            "sku_id": "123", "sku_name": "颜色:红色;容量:200ml", "price": "12.50",
        }])

    def test_elastic_promotion_filter(self) -> None:
        self.assertTrue(_is_elastic_promotion({"title": "Эластичный бустинг x4"}))
        self.assertTrue(_is_elastic_promotion({"title": "Elastic boost"}))
        self.assertFalse(_is_elastic_promotion({"title": "Скидка на сток"}))

    def test_promotion_completes_only_after_ozon_accepts_product(self) -> None:
        draft = OzonListingDraft(
            source_sku="1", offer_id="AUTO-1", source_ozon_url="", title="商品",
            exported_category="类目", stock=5, price="50",
            pricing_result={"actual_sale": "37"},
            status="STOCK_COMPLETED_PROMOTION_PENDING",
        )

        class Client:
            def promotions(self):
                return [{"id": 700, "title": "Эластичный бустинг x4"}]

            def product_info_by_offer(self, _offer):
                return {"id": 99}

            def find_promotion_candidate(self, action_id, product_id):
                self.candidate = (action_id, product_id)
                return {"id": 99, "max_action_price": "40"}

            def activate_promotion(self, action_id, product_id, price, stock):
                self.activation = (action_id, product_id, str(price), stock)
                return {"result": {"product_ids": [99], "rejected": []}}

        client = Client()

        class App:
            @staticmethod
            def ozon_client():
                return client

        _activate_promotion(App(), draft, 700)
        self.assertEqual(client.candidate, (700, 99))
        self.assertEqual(client.activation, (700, 99, "37.00", 5))
        self.assertEqual(draft.status, "COMPLETED")
        self.assertEqual(draft.promotion_action_title, "Эластичный бустинг x4")

    def test_rejected_promotion_remains_pending(self) -> None:
        draft = OzonListingDraft(
            source_sku="1", offer_id="AUTO-1", source_ozon_url="", title="商品",
            exported_category="类目", stock=5, price="37",
            status="STOCK_COMPLETED_PROMOTION_PENDING",
        )

        class Client:
            def promotions(self): return [{"id": 700, "title": "Бустинг"}]
            def product_info_by_offer(self, _offer): return {"id": 99}
            def find_promotion_candidate(self, _action, _product): return {"id": 99}
            def activate_promotion(self, *_args):
                return {"result": {"product_ids": [], "rejected": [{"product_id": 99}]}}

        class App:
            @staticmethod
            def ozon_client(): return Client()

        with self.assertRaisesRegex(RuntimeError, "拒绝加入促销"):
            _activate_promotion(App(), draft, 700)
        self.assertEqual(draft.status, "STOCK_COMPLETED_PROMOTION_PENDING")

    def test_legacy_draft_promotion_price_is_reconstructed_from_listing_multiplier(self) -> None:
        draft = OzonListingDraft(
            source_sku="1", offer_id="AUTO-1", source_ozon_url="", title="商品",
            exported_category="类目", price="85.92", pricing_result={},
        )
        self.assertEqual(_promotion_action_price(draft, AppConfig(pricing_price_multiplier="3")), Decimal("28.64"))

    def test_persisted_actual_sale_has_priority_for_promotion(self) -> None:
        draft = OzonListingDraft(
            source_sku="1", offer_id="AUTO-1", source_ozon_url="", title="商品",
            exported_category="类目", price="85.92", pricing_result={"actual_sale": "31.25"},
        )
        self.assertEqual(_promotion_action_price(draft, AppConfig(pricing_price_multiplier="3")), Decimal("31.25"))

    def test_public_config_never_returns_secrets(self) -> None:
        value = _public_config(AppConfig(
            sourcing_provider="elim",
            elim_api_key="elim-secret",
            daji_app_key="daji-key",
            daji_app_secret="daji-secret",
            daji_enabled="1",
            daji_freight_province_code="330000",
            daji_freight_city_code="330700",
            daji_freight_district_code="330782",
            alibaba_app_key="app-key",
            alibaba_app_secret="secret",
            alibaba_access_token="token",
            ozon_client_id="client",
            ozon_api_key="api-key",
            product_manager_api_token="ecs-secret",
        ))
        self.assertNotIn("alibaba_app_secret", value)
        self.assertNotIn("daji_app_secret", value)
        self.assertNotIn("elim_api_key", value)
        self.assertEqual(value["sourcing_provider"], "elim")
        self.assertTrue(value["elim_api_key_configured"])
        self.assertNotIn("alibaba_access_token", value)
        self.assertNotIn("ozon_api_key", value)
        self.assertNotIn("product_manager_api_token", value)
        self.assertTrue(value["alibaba_app_secret_configured"])
        self.assertTrue(value["daji_app_secret_configured"])
        self.assertTrue(value["daji_enabled"])
        self.assertTrue(value["daji_freight_ready"])
        self.assertEqual(value["daji_freight_address"], "浙江省 · 金华市 · 义乌市")
        self.assertTrue(value["ozon_api_key_configured"])
        self.assertTrue(value["product_manager_api_token_configured"])
        self.assertEqual(value["target_cost_profit_margin"], "30")
        self.assertIn("爱好与运动", value["pricing_tariffs"])

    def test_background_job_serialization(self) -> None:
        job = BackgroundJob(id="job-1", kind="test", status="DONE", result={"ok": True})
        self.assertEqual(job.to_dict()["result"], {"ok": True})

    def test_registry_captures_worker_errors(self) -> None:
        registry = JobRegistry()

        def fail(_job):
            raise ValueError("expected failure")

        job = registry.start("test", fail)
        for _ in range(100):
            if job.status != "RUNNING":
                break
            time.sleep(0.01)
        self.assertEqual(job.status, "ERROR")
        self.assertIn("expected failure", job.error)

    def test_registry_exposes_product_job_for_workflow_tracking(self) -> None:
        registry = JobRegistry()
        job = registry.start("sourcing", lambda _job: {"ok": True}, source_sku="sku-1")
        for _ in range(100):
            if job.status != "RUNNING":
                break
            time.sleep(0.01)
        listed = registry.list()
        self.assertEqual(listed[0].source_sku, "sku-1")

    def test_registry_rejects_concurrent_jobs_for_same_product(self) -> None:
        registry = JobRegistry()
        release = threading.Event()
        started = threading.Event()

        def wait_for_release(_job):
            started.set()
            release.wait(timeout=2)

        first = registry.start("sourcing", wait_for_release, source_sku="sku-1")
        self.assertTrue(started.wait(timeout=1))
        try:
            with self.assertRaisesRegex(RuntimeError, "正在执行"):
                registry.start("ai-prepare", lambda _job: None, source_sku="sku-1")
        finally:
            release.set()
        for _ in range(100):
            if first.status != "RUNNING":
                break
            time.sleep(0.01)
        self.assertEqual(first.status, "DONE")

    def test_registry_allows_jobs_for_different_products(self) -> None:
        registry = JobRegistry()
        release = threading.Event()
        first = registry.start(
            "sourcing", lambda _job: release.wait(timeout=2), source_sku="sku-1"
        )
        second = registry.start("ai-prepare", lambda _job: None, source_sku="sku-2")
        release.set()
        for _ in range(100):
            if first.status != "RUNNING" and second.status != "RUNNING":
                break
            time.sleep(0.01)
        self.assertEqual(first.status, "DONE")
        self.assertEqual(second.status, "DONE")

    def test_local_http_bootstrap_and_csrf_rejection(self) -> None:
        server = LocalWebServer(("127.0.0.1", 0), WebApplication())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base + "/api/bootstrap") as response:
                payload = json.load(response)
            self.assertIn("csrf_token", payload)
            request = urllib.request.Request(
                base + "/api/warehouses",
                data=b"{}",
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
