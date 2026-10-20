from __future__ import annotations

import unittest
import re

from erp.modules.ozon_follow.legacy.services.ozon_listing import (
    OzonCategoryMatch,
    OzonListingDraft,
    OzonListingValidationError,
    OzonListingWorkflow,
    build_ozon_import_item,
    match_exported_category,
    prepare_listing_draft,
    required_attribute_skeleton,
    validate_listing_draft,
)


class FakeOzonClient:
    def __init__(self) -> None:
        self.imported_item = None
        self.stock_call = None

    def category_attributes(self, _category_id, _type_id, _language):
        return [{"id": 10, "name": "品牌", "is_required": True}]

    def import_product(self, item):
        self.imported_item = item
        return 321

    def wait_import(self, _task_id):
        return {"result": {"items": [{"status": "imported", "errors": []}]}}

    def import_info(self, _task_id):
        return {"result": {"items": [{"status": "imported", "errors": []}]}}

    def wait_product_available(self, offer_id):
        return {"offer_id": offer_id}

    def update_stock(self, offer_id, warehouse_id, stock):
        self.stock_call = (offer_id, warehouse_id, stock)
        return {"result": [{"updated": True}]}


class OzonListingTests(unittest.TestCase):
    def test_default_package_dimensions_are_applied_to_new_and_saved_drafts(self) -> None:
        draft = OzonListingDraft(
            source_sku="1", offer_id="AUTO-1", source_ozon_url="",
            title="商品", exported_category="宠物玩具",
        )
        self.assertEqual((draft.depth_mm, draft.width_mm, draft.height_mm), (300, 200, 100))
        restored = OzonListingDraft.from_dict({**draft.to_dict(), "depth_mm": None, "width_mm": "", "height_mm": None})
        self.assertEqual((restored.depth_mm, restored.width_mm, restored.height_mm), (300, 200, 100))

    def test_saved_draft_removes_main_image_from_additional_images(self) -> None:
        restored = OzonListingDraft.from_dict({
            "source_sku": "1", "offer_id": "AUTO-1", "source_ozon_url": "",
            "title": "商品", "exported_category": "类目",
            "primary_image": "https://oss.example/main.jpg",
            "images": ["https://oss.example/main.jpg", "https://ozon.example/side.jpg"],
        })
        self.assertEqual(restored.images, ["https://ozon.example/side.jpg"])

    def test_prepare_uses_exported_category_and_weight(self) -> None:
        match = OzonCategoryMatch(100, 200, "家居 / 收纳", "Дом", "收纳")
        draft = prepare_listing_draft(
            {
                "sku": "12345",
                "title": "收纳盒",
                "category": "收纳",
                "weight_grams": 450,
                "image_url": "https://cdn.example/wc50/a.jpg",
            },
            category_match=match,
            offer_id="AUTO-20260830-001",
        )
        self.assertEqual(draft.exported_category, "收纳")
        self.assertEqual(draft.weight_g, 450)
        self.assertEqual(draft.description_category_id, 100)
        self.assertEqual(draft.primary_image, "https://cdn.example/wc1000/a.jpg")

    def test_prepare_uses_date_offer_id_and_original_additional_images(self) -> None:
        draft = prepare_listing_draft({
            "sku": "12345", "title": "商品", "category": "类目", "weight_grams": 100,
            "image_url": "https://cdn.example/wc50/main.jpg",
            "image_urls": [
                "https://cdn.example/wc50/main.jpg",
                "https://cdn.example/wc50/side.jpg",
            ],
        }, category_match=None, offer_id="AUTO-20260830-002")
        self.assertEqual(draft.offer_id, "AUTO-20260830-002")
        self.assertEqual(draft.primary_image, "https://cdn.example/wc1000/main.jpg")
        self.assertEqual(draft.images, ["https://cdn.example/wc1000/side.jpg"])

    def test_generated_main_and_original_additional_images_build_payload(self) -> None:
        draft = self._ready_draft()
        draft.primary_image = "https://oss.example/generated.jpg"
        draft.images = [
            "https://ozon.example/side-1.jpg",
            "https://ozon.example/side-2.jpg",
            "https://oss.example/generated.jpg",
        ]
        item = build_ozon_import_item(draft, [{"id": 10, "name": "品牌", "is_required": True}])
        self.assertEqual(item["primary_image"], "https://oss.example/generated.jpg")
        self.assertEqual(item["images"], ["https://ozon.example/side-1.jpg", "https://ozon.example/side-2.jpg"])

    def test_submission_is_blocked_until_every_image_is_on_oss(self) -> None:
        draft = self._ready_draft()
        draft.images_oss_ready = False
        self.assertIn("主图和附图尚未全部上传到OSS", validate_listing_draft(draft, [
            {"id": 10, "name": "品牌", "is_required": True}
        ]))

    def test_category_matches_only_one_live_category(self) -> None:
        zh_tree = [{
            "category_name": "家居",
            "description_category_id": 100,
            "children": [{"type_id": 200, "type_name": "收纳"}],
        }]
        ru_tree = [{
            "category_name": "Дом",
            "description_category_id": 100,
            "children": [{"type_id": 200, "type_name": "Хранение"}],
        }]
        match = match_exported_category("收纳\nХранение", zh_tree, ru_tree)
        self.assertIsNotNone(match)
        self.assertEqual((match.description_category_id, match.type_id), (100, 200))

    def test_required_attribute_skeleton_preserves_complex_group(self) -> None:
        attributes, complex_attributes = required_attribute_skeleton([
            {"id": 10, "name": "品牌", "is_required": True},
            {"id": 20, "name": "颜色", "is_required": True, "attribute_complex_id": 9},
            {"id": 30, "name": "可选", "is_required": False},
        ])
        self.assertEqual([item["id"] for item in attributes], [10])
        self.assertEqual(complex_attributes[0]["_complex_id"], 9)
        self.assertEqual(complex_attributes[0]["attributes"][0]["id"], 20)

    def _ready_draft(self) -> OzonListingDraft:
        return OzonListingDraft(
            source_sku="source-1",
            offer_id="AUTO-source-1",
            source_ozon_url="https://ozon.example/1",
            title="测试商品",
            exported_category="收纳",
            description_category_id=100,
            type_id=200,
            weight_g=450,
            depth_mm=100,
            width_mm=80,
            height_mm=60,
            price="999",
            old_price="1200",
            currency_code="RUB",
            primary_image="https://cdn.example/a.jpg",
            images_oss_ready=True,
            attributes=[{"id": 10, "values": [{"value": "Нет бренда"}]}],
            warehouse_id=777,
            stock=3,
        )

    def test_build_import_item_and_submit_then_publish_stock(self) -> None:
        draft = self._ready_draft()
        item = build_ozon_import_item(
            draft, [{"id": 10, "name": "品牌", "is_required": True}]
        )
        self.assertEqual(item["weight"], 450)
        self.assertEqual(item["description_category_id"], 100)

        client = FakeOzonClient()
        workflow = OzonListingWorkflow(client)
        result = workflow.submit(draft)
        self.assertEqual(result.task_id, 321)
        self.assertEqual(draft.status, "IMPORT_COMPLETED_STOCK_PENDING")
        workflow.publish_stock(draft)
        self.assertEqual(client.stock_call, ("AUTO-source-1", 777, 3))
        self.assertEqual(draft.status, "STOCK_COMPLETED_PROMOTION_PENDING")

    def test_import_task_id_is_exposed_before_waiting_for_result(self) -> None:
        draft = self._ready_draft()
        events = []

        class OrderedClient(FakeOzonClient):
            def wait_import(self, task_id):
                events.append(("wait", task_id, draft.task_id, draft.status))
                return super().wait_import(task_id)

        workflow = OzonListingWorkflow(OrderedClient())
        workflow.submit(
            draft,
            on_task_created=lambda current: events.append(
                ("saved", current.task_id, current.status)
            ),
        )
        self.assertEqual(events[0], ("saved", 321, "IMPORT_SUBMITTED"))
        self.assertEqual(events[1], ("wait", 321, 321, "IMPORT_SUBMITTED"))

    def test_hashtag_attribute_uses_ozon_space_separator_without_mutating_draft(self) -> None:
        draft = self._ready_draft()
        draft.attributes.append({
            "id": 23171,
            "values": [{"value": "#игрушка,#для_кошек"}],
        })
        item = build_ozon_import_item(
            draft, [{"id": 10, "name": "品牌", "is_required": True}]
        )
        hashtag = next(value for value in item["attributes"] if value["id"] == 23171)
        self.assertEqual(hashtag["values"][0]["value"], "#игрушка #для_кошек")
        self.assertEqual(draft.attributes[1]["values"][0]["value"], "#игрушка,#для_кошек")

    def test_color_is_limited_to_three_values_and_multicolor_is_exclusive(self) -> None:
        draft = self._ready_draft()
        draft.attributes.append({
            "id": 10096, "_name": "商品颜色", "values": [
                {"value": "Красный", "dictionary_value_id": 1},
                {"value": "Синий", "dictionary_value_id": 2},
                {"value": "Белый", "dictionary_value_id": 3},
                {"value": "Черный", "dictionary_value_id": 4},
            ],
        })
        item = build_ozon_import_item(
            draft, [{"id": 10, "name": "品牌", "is_required": True}]
        )
        color = next(value for value in item["attributes"] if value["id"] == 10096)
        self.assertEqual([value["value"] for value in color["values"]], ["Красный", "Синий", "Белый"])

        draft.attributes[-1]["values"].append(
            {"value": "Разноцветный", "dictionary_value_id": 5}
        )
        item = build_ozon_import_item(
            draft, [{"id": 10, "name": "品牌", "is_required": True}]
        )
        color = next(value for value in item["attributes"] if value["id"] == 10096)
        self.assertEqual(color["values"], [{"value": "Разноцветный", "dictionary_value_id": 5}])

    def test_stock_is_blocked_before_successful_import(self) -> None:
        draft = self._ready_draft()
        with self.assertRaises(OzonListingValidationError):
            OzonListingWorkflow(FakeOzonClient()).publish_stock(draft)

    def test_zero_stock_is_rejected(self) -> None:
        draft = self._ready_draft()
        draft.status = "IMPORT_COMPLETED_STOCK_PENDING"
        draft.stock = 0
        with self.assertRaisesRegex(OzonListingValidationError, "必须大于0"):
            OzonListingWorkflow(FakeOzonClient()).publish_stock(draft)

    def test_existing_task_cannot_be_submitted_again(self) -> None:
        draft = self._ready_draft()
        draft.task_id = 321
        draft.status = "IMPORT_PENDING"
        with self.assertRaisesRegex(OzonListingValidationError, "不要重复提交"):
            OzonListingWorkflow(FakeOzonClient()).submit(draft)

    def test_existing_import_task_can_be_resumed_without_resubmission(self) -> None:
        draft = self._ready_draft()
        draft.task_id = 321
        draft.status = "IMPORT_PENDING"
        client = FakeOzonClient()
        OzonListingWorkflow(client).refresh_import(draft)
        self.assertEqual(draft.status, "IMPORT_COMPLETED_STOCK_PENDING")
        self.assertIsNone(client.imported_item)

    def test_imported_product_with_warnings_is_successful(self) -> None:
        draft = self._ready_draft()

        class WarningClient(FakeOzonClient):
            def wait_import(self, _task_id):
                return {"result": {"items": [{
                    "status": "imported",
                    "product_id": 123,
                    "errors": [{"level": "warning", "code": "VALUE_CORRECTED"}],
                }]}}

        result = OzonListingWorkflow(WarningClient()).submit(draft)
        self.assertEqual(result.status, "IMPORT_COMPLETED_STOCK_PENDING")
        self.assertEqual(draft.blockers, [])


if __name__ == "__main__":
    unittest.main()
