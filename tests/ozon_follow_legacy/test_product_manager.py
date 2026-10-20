from __future__ import annotations

import unittest
from decimal import Decimal

from erp.modules.ozon_follow.legacy.config import AppConfig
from erp.modules.ozon_follow.legacy.services.ozon_listing import OzonListingDraft
from erp.modules.ozon_follow.legacy.services.product_manager import ProductManagerApi, build_product_manager_payload
from erp.modules.ozon_follow.legacy.services.sourcing_selector import SourcingCandidate
from erp.modules.ozon_follow.legacy.web.server import _sync_product_manager


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def mount(self, *_args):
        pass

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(self.responses.pop(0))


def source_with_spec(spec_model: str = "颜色:P03绿-突突球") -> SourcingCandidate:
    raw = {
        "itemId": "823145561760",
        "skuId": "6070945559825",
        "title": "电动猫咪玩具智能滚滚球",
        "skuName": spec_model,
        "realtimeSinglePrice": "6.5",
    }
    return SourcingCandidate(
        item_id=raw["itemId"],
        sku_id=raw["skuId"],
        title=raw["title"],
        sku_name=spec_model.strip(),
        image_url="https://example.com/source.jpg",
        company="供货商",
        unit_price=Decimal("6.5"),
        delivery_price=Decimal("2"),
        minimum_order_quantity=Decimal("1"),
        sales=10,
        raw=raw,
    )


class ProductManagerApiTests(unittest.TestCase):
    def test_upsert_uses_https_bearer_token_and_expected_endpoint(self) -> None:
        session = FakeSession([{"ok": True, "result": {"operation": "created"}}])
        client = ProductManagerApi(
            "https://products.example.com/api/v1", "secret-token", session=session
        )
        result = client.upsert_product({"sku": "AUTO-001"})

        self.assertEqual(result["operation"], "created")
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://products.example.com/api/v1/products/upsert/")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret-token")

    def test_rejects_insecure_remote_api(self) -> None:
        with self.assertRaisesRegex(ValueError, "必须使用HTTPS"):
            ProductManagerApi("http://products.example.com/api/v1", "token")

    def test_payload_preserves_raw_1688_sku_name_exactly(self) -> None:
        raw_spec = " 颜色:P03绿-突突球  "
        draft = OzonListingDraft(
            source_sku="1952388613",
            source_ozon_url="https://www.ozon.ru/product/1952388613",
            offer_id="AUTO-20260830-001",
            title="测试商品",
            exported_category="宠物用品",
            primary_image="https://oss.example.com/08/30/001/main.jpg",
            selected_1688_url="https://detail.1688.com/offer/999999999999.html",
            product_manager_color="红色",
            product_manager_size="M",
            product_manager_remark="优先采购",
        )
        payload = build_product_manager_payload(draft, source_with_spec(raw_spec), 8)

        self.assertEqual(payload["spec_model"], raw_spec)
        self.assertEqual(payload["chinese_name"], "电动猫咪玩具智能滚滚球")
        self.assertEqual(
            payload["source_url"],
            "https://detail.1688.com/offer/999999999999.html",
        )
        self.assertEqual(payload["color"], "红色")
        self.assertEqual(payload["size"], "M")
        self.assertEqual(payload["remark"], "优先采购")
        self.assertEqual(payload["purchase_price"], "8.50")
        self.assertEqual(
            payload["image_url"], "https://oss.example.com/08/30/001/main.jpg"
        )

    def test_payload_uses_unprefixed_base_offer_id_for_ecs(self) -> None:
        draft = OzonListingDraft(
            source_sku="1", offer_id="WXZ-AUTO-20260902-001",
            base_offer_id="AUTO-20260902-001", source_ozon_url="", title="",
            exported_category="", selected_1688_url="https://detail.1688.com/offer/123.html",
        )
        source = source_with_spec("红色 / M")
        payload = build_product_manager_payload(draft, source, 8)
        self.assertEqual(payload["sku"], "AUTO-20260902-001")


class ProductManagerSyncTests(unittest.TestCase):
    def test_failure_keeps_ozon_success_state_and_can_retry_independently(self) -> None:
        draft = OzonListingDraft(
            source_sku="1952388613",
            source_ozon_url="https://www.ozon.ru/product/1952388613",
            offer_id="AUTO-20260830-001",
            title="测试商品",
            exported_category="宠物用品",
            primary_image="https://oss.example.com/main.jpg",
            status="IMPORT_COMPLETED_STOCK_PENDING",
        )

        class Repository:
            @staticmethod
            def get_latest_sourcing_task_for_sku(_sku):
                return {"selected": source_with_spec().raw}

        class ConfigStore:
            @staticmethod
            def load():
                return AppConfig(
                    product_manager_enabled="1",
                    product_manager_shop_id="8",
                    product_manager_shop_name="WXZ-01",
                )

        class App:
            repository = Repository()
            config_store = ConfigStore()
            attempts = 0

            def product_manager_client(self):
                parent = self

                class Client:
                    @staticmethod
                    def upsert_product(_payload):
                        parent.attempts += 1
                        if parent.attempts == 1:
                            raise RuntimeError("temporary ECS failure")
                        return {"operation": "updated", "sku": draft.offer_id}

                return Client()

        app = App()
        with self.assertRaisesRegex(RuntimeError, "temporary ECS failure"):
            _sync_product_manager(app, draft)
        self.assertEqual(draft.status, "IMPORT_COMPLETED_STOCK_PENDING")
        self.assertEqual(draft.product_manager_sync["status"], "FAILED")

        result = _sync_product_manager(app, draft)
        self.assertEqual(app.attempts, 2)
        self.assertEqual(result["status"], "SYNCED")
        self.assertEqual(result["operation"], "updated")
        self.assertEqual(result["spec_model"], "颜色:P03绿-突突球")


if __name__ == "__main__":
    unittest.main()
