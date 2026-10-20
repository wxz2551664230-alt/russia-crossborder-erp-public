from __future__ import annotations

import io
import hashlib
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import requests
from requests import ConnectTimeout

from erp.modules.ozon_follow.legacy.services.ai_listing import (
    ChatJsonClient,
    _extract_json,
    _dictionary_search_queries,
    _unique_dictionary_match,
    apply_ai_attributes,
    apply_content_attributes,
    build_product_master,
    build_main_image_prompt,
    image_difference_score,
    ListingContentService,
    ImageEditService,
    OssUploader,
    VisionAnalysisService,
    approve_pending_main_image,
    enforce_system_attributes,
    enforce_known_fact_attributes,
    format_hashtags,
    normalize_hashtags,
    normalize_existing_draft,
    prepare_listing_with_ai,
    refresh_additional_images,
    download_reference_image,
)
from erp.modules.ozon_follow.legacy.config import AppConfig
from erp.modules.ozon_follow.legacy.services.ozon_listing import OzonListingDraft
from erp.modules.ozon_follow.legacy.services.ozon_gallery import OzonGallery, additional_gallery_images, normalize_gallery_images


class AiListingTests(unittest.TestCase):
    def test_chat_client_falls_back_when_default_model_has_no_channel(self) -> None:
        class Response:
            def __init__(self, status_code, payload):
                self.status_code, self._payload = status_code, payload
                self.text = json.dumps(payload)
                self.ok = status_code < 400

            def raise_for_status(self):
                if not self.ok:
                    raise requests.HTTPError(response=self)

            def json(self):
                return self._payload

        responses = [
            Response(503, {"error": {"code": "model_not_found", "message": "No available channel"}}),
            Response(200, {"choices": [{"message": {"content": '{"ok":true}'}}]}),
        ]
        with patch("erp.modules.ozon_follow.legacy.services.ai_listing.requests.post", side_effect=responses) as post:
            result = ChatJsonClient("https://example.test", "key", "primary", models=("primary", "backup")).complete("s", "p")
        self.assertTrue(result["ok"])
        self.assertEqual([call.kwargs["json"]["model"] for call in post.call_args_list], ["primary", "backup"])

    def test_reference_image_download_retries_transient_connection_failure(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (20, 20), "white").save(buffer, "JPEG")

        class Response:
            content = buffer.getvalue()

            @staticmethod
            def raise_for_status() -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "erp.modules.ozon_follow.legacy.services.ai_listing.requests.get",
                side_effect=[ConnectTimeout("timeout"), Response()],
            ) as get:
                with patch("erp.modules.ozon_follow.legacy.services.ai_listing.time.sleep"):
                    target = download_reference_image(
                        "https://ir.ozone.ru/s3/multimedia/detail.jpg",
                        Path(directory),
                        "detail.jpg",
                    )

            self.assertTrue(target.is_file())
            self.assertEqual(get.call_count, 2)

    def test_reference_image_download_reuses_valid_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            url = "https://ir.ozone.ru/s3/multimedia/detail.jpg"
            fingerprint = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
            target = Path(directory) / f"detail_{fingerprint}.jpg"
            Image.new("RGB", (20, 20), "white").save(target, "JPEG")

            with patch("erp.modules.ozon_follow.legacy.services.ai_listing.requests.get") as get:
                result = download_reference_image(
                    url,
                    Path(directory),
                    "detail.jpg",
                )

            self.assertEqual(result, target)
            get.assert_not_called()

    def test_reference_image_cache_is_scoped_to_source_url(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (20, 20), "white").save(buffer, "JPEG")

        class Response:
            content = buffer.getvalue()

            @staticmethod
            def raise_for_status() -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            with patch("erp.modules.ozon_follow.legacy.services.ai_listing.requests.get", return_value=Response()) as get:
                first = download_reference_image("https://example.com/a.jpg", Path(directory), "detail.jpg")
                second = download_reference_image("https://example.com/b.jpg", Path(directory), "detail.jpg")
            self.assertNotEqual(first, second)
            self.assertEqual(get.call_count, 2)

    def test_ai_prepare_reuses_imported_gallery_without_scraping_ozon_page(self) -> None:
        product = {
            "image_url": "https://cdn.test/main.jpg",
            "image_urls": ["https://cdn.test/main.jpg", "https://cdn.test/side.jpg"],
            "title": "test",
        }
        draft = OzonListingDraft(source_sku="123456789", offer_id="AUTO-1", source_ozon_url="https://www.ozon.ru/product/demo-123456789/", title="", exported_category="")
        gallery_result = {"additional_image_count": 1}
        with patch("erp.modules.ozon_follow.legacy.services.ai_listing.scrape_ozon_gallery") as scrape:
            with patch("erp.modules.ozon_follow.legacy.services.ai_listing.upload_captured_gallery_images", return_value=gallery_result) as upload:
                with patch("erp.modules.ozon_follow.legacy.services.ai_listing.download_reference_image", side_effect=RuntimeError("stop after gallery")):
                    with self.assertRaises(RuntimeError):
                        prepare_listing_with_ai(config=AppConfig(), product=product, draft=draft, definitions=[], ozon_client=object())
        scrape.assert_not_called()
        self.assertEqual(upload.call_args.kwargs["gallery"].source, "imported-catalog")

    def test_image_generation_retries_connection_timeout(self) -> None:
        class Response:
            status_code = 200
            text = ""

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                import base64
                import io
                buffer = io.BytesIO()
                Image.new("RGB", (20, 20), "white").save(buffer, "PNG")
                return {"data": [{"b64_json": base64.b64encode(buffer.getvalue()).decode()}]}

        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / "reference.jpg"
            Image.new("RGB", (20, 20), "white").save(reference)
            with patch("erp.modules.ozon_follow.legacy.services.ai_listing.requests.post", side_effect=[ConnectTimeout("timeout"), Response()]) as post:
                with patch("erp.modules.ozon_follow.legacy.services.ai_listing.time.sleep"):
                    generated = ImageEditService("https://example.test/v1/images/edits", "key", "model").generate(
                        str(reference), "prompt", Path(directory)
                    )
            self.assertTrue(generated.is_file())
            self.assertEqual(post.call_count, 2)

    def test_gpt_image_25_uses_supported_low_quality(self) -> None:
        class Response:
            status_code = 200
            text = ""

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                import base64
                buffer = io.BytesIO()
                Image.new("RGB", (20, 20), "white").save(buffer, "PNG")
                return {"data": [{"b64_json": base64.b64encode(buffer.getvalue()).decode()}]}

        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / "reference.jpg"
            Image.new("RGB", (20, 20), "white").save(reference)
            with patch("erp.modules.ozon_follow.legacy.services.ai_listing.requests.post", return_value=Response()) as post:
                ImageEditService(
                    "https://example.test/v1/images/edits", "key", "gpt-image-2.5-flare"
                ).generate(str(reference), "prompt", Path(directory))

        self.assertEqual(post.call_args.kwargs["data"]["quality"], "low")

    def test_known_mug_facts_fill_safe_optional_attributes(self) -> None:
        draft = OzonListingDraft(
            source_sku="1", offer_id="AUTO-1", source_ozon_url="", title="",
            exported_category="", type_id=92499,
        )
        definitions = [
            {"id": 9024, "name": "卖家代码"},
            {"id": 11650, "name": "原厂包装数量"},
            {"id": 4383, "name": "商品重量，克"},
            {"id": 4497, "name": "含包装重量，克"},
            {"id": 8449, "name": "为谁", "dictionary_id": 449, "is_collection": True},
            {"id": 9390, "name": "目标受众", "dictionary_id": 1183, "is_collection": True},
            {"id": 6953, "name": "炊具的特点", "dictionary_id": 1224, "is_collection": True},
            {"id": 8448, "name": "假日", "dictionary_id": 347, "is_collection": True},
        ]
        applied = enforce_known_fact_attributes(draft, definitions, {"weight_grams": 455})
        self.assertEqual(len({item["attribute_id"] for item in applied}), 4)
        self.assertFalse(any(item["id"] == 6953 for item in draft.attributes))

    def test_known_ozon_type_uses_only_exact_category_option(self) -> None:
        definitions = [{"id": 8229, "name": "Тип", "dictionary_id": 1960,
                        "allowed_values": [{"id": 92692, "value": "Скатерть"}]}]
        matching = OzonListingDraft(source_sku="1", offer_id="AUTO-1",
                                    source_ozon_url="", title="", exported_category="",
                                    type_id=92692)
        applied = enforce_known_fact_attributes(matching, definitions, {})
        self.assertEqual(applied[0]["dictionary_value_id"], 92692)
        self.assertEqual(matching.attributes[0]["values"],
                         [{"value": "Скатерть", "dictionary_value_id": 92692}])

        unmatched = OzonListingDraft(source_sku="2", offer_id="AUTO-2",
                                     source_ozon_url="", title="", exported_category="",
                                     type_id=99999)
        self.assertEqual(enforce_known_fact_attributes(unmatched, definitions, {}), [])
        self.assertEqual(unmatched.attributes, [])

    def test_ozon_gallery_deduplicates_sizes_and_excludes_primary(self) -> None:
        values = [
            "https://ir.ozone.ru/s3/multimedia-1-a/wc300/main.jpg",
            "https://ir.ozone.ru/s3/multimedia-1-a/wc1000/main.jpg",
            "https://ir.ozone.ru/s3/multimedia-1-b/wc300/detail.jpg",
            "https://ir.ozone.ru/s3/video-1/example/cover/wc1000/cover.jpg",
        ]
        normalized = normalize_gallery_images(values)
        self.assertEqual(len(normalized), 2)
        self.assertTrue(all("/wc2000/" in url for url in normalized))
        self.assertEqual(
            additional_gallery_images(normalized, values[0]),
            ["https://ir.ozone.ru/s3/multimedia-1-b/wc2000/detail.jpg"],
        )

    def test_manual_gallery_refresh_prefers_direct_gallery_before_edge_bridge(self) -> None:
        main = "https://ir.ozone.ru/s3/multimedia-1-a/wc300/main.jpg"
        detail = "https://ir.ozone.ru/s3/multimedia-1-b/wc300/detail.jpg"
        public_detail = "https://bucket.example/附图01.jpg"
        draft = OzonListingDraft(
            source_sku="1",
            offer_id="AUTO-1",
            source_ozon_url="https://www.ozon.ru/product/1",
            title="",
            exported_category="",
            primary_image="https://bucket.example/主图.jpg",
        )
        config = AppConfig(
            oss_bucket="bucket",
            oss_endpoint="example",
            oss_public_base_url="https://bucket.example",
        )
        gallery = OzonGallery(
            url=draft.source_ozon_url,
            title="商品",
            images=[main, detail],
            source="visible-browser",
        )

        with patch("erp.modules.ozon_follow.legacy.services.ai_listing.scrape_ozon_gallery", return_value=gallery) as scrape:
            with patch("erp.modules.ozon_follow.legacy.services.ai_listing.download_reference_image", return_value=Path("detail.jpg")):
                with patch.object(OssUploader, "upload", return_value=public_detail):
                    result = refresh_additional_images(
                        config=config,
                        product={"image_url": main, "ozon_url": draft.source_ozon_url},
                        draft=draft,
                    )

        self.assertFalse(scrape.call_args.kwargs["browser_fallback"])
        self.assertEqual(scrape.call_args.kwargs["timeout"], 15)
        self.assertEqual(draft.images, [public_detail])
        self.assertTrue(draft.images_oss_ready)
        self.assertEqual(result["original_gallery_count"], 2)
        self.assertEqual(result["additional_image_count"], 1)
        self.assertEqual(draft.ai_preparation["gallery_status"], "READY")

    def test_main_image_only_gallery_is_ready_when_main_is_on_oss(self) -> None:
        main = "https://ir.ozone.ru/s3/multimedia-1-a/wc300/main.jpg"
        draft = OzonListingDraft(
            source_sku="1", offer_id="AUTO-1",
            source_ozon_url="https://www.ozon.ru/product/1",
            title="", exported_category="",
            primary_image="https://bucket.example/主图.jpg",
            images=["https://stale.example/old-detail.jpg"],
        )
        config = AppConfig(
            oss_bucket="bucket", oss_endpoint="example",
            oss_public_base_url="https://bucket.example",
        )
        gallery = OzonGallery(
            url=draft.source_ozon_url, title="商品", images=[main], source="visible-browser"
        )
        with patch("erp.modules.ozon_follow.legacy.services.ai_listing.scrape_ozon_gallery", return_value=gallery):
            result = refresh_additional_images(
                config=config,
                product={"image_url": main, "ozon_url": draft.source_ozon_url},
                draft=draft,
            )
        self.assertEqual(draft.images, [])
        self.assertTrue(draft.images_oss_ready)
        self.assertIn("只有主图", result["gallery_warning"])

    def test_oss_key_uses_month_day_offer_and_image_name(self) -> None:
        key = OssUploader.object_key(
            "ozon-auto-listing", "AUTO-20260830-1", "附图01.jpg",
            month="08", day="30",
        )
        self.assertEqual(key, "ozon-auto-listing/08/30/AUTO-20260830-1/附图01.jpg")

    def test_extracts_json_from_chat_completion_fence(self) -> None:
        result = _extract_json({
            "choices": [{"message": {"content": "```json\n{\"title_ru\":\"Игрушка\"}\n```"}}]
        })
        self.assertEqual(result["title_ru"], "Игрушка")

    def test_dictionary_match_requires_unique_candidate(self) -> None:
        options = [{"id": 1, "value": "Красный"}, {"id": 2, "value": "Темно-красный"}]
        self.assertEqual(_unique_dictionary_match("Красный", options)["id"], 1)
        self.assertIsNone(_unique_dictionary_match("крас", options))

    def test_collection_dictionary_values_split_on_semicolon(self) -> None:
        class FakeClient:
            def search_category_attribute_values(self, *_args, **_kwargs):
                query = _args[3]
                return [{"id": 33754 if "Кош" in query else 33746, "value": "猫类商品" if "Кош" in query else "狗类商品"}]

        draft = OzonListingDraft(
            source_sku="1", offer_id="AUTO-1", source_ozon_url="", title="",
            exported_category="", description_category_id=10, type_id=20,
        )
        result = apply_ai_attributes(draft, [{
            "id": 4958, "name": "专为", "dictionary_id": 1,
            "is_collection": True, "max_value_count": 5,
        }], [{
            "attribute_id": 4958, "value": "Кошки;Собаки",
            "confidence": 0.98, "evidence": "商品适用于猫和狗",
        }], FakeClient())  # type: ignore[arg-type]
        self.assertFalse(result["pending"])
        self.assertEqual(
            [value["dictionary_value_id"] for value in draft.attributes[0]["values"]],
            [33754, 33746],
        )

    def test_dictionary_queries_include_conservative_russian_stems(self) -> None:
        queries = _dictionary_search_queries("Кошки")
        self.assertEqual(queries[0], "Кошки")
        self.assertIn("Кош", queries)

    def test_content_fields_are_written_to_ozon_attributes(self) -> None:
        draft = OzonListingDraft(
            source_sku="1", offer_id="AUTO-1", source_ozon_url="", title="old",
            exported_category="Игрушки",
        )
        definitions = [
            {"id": 10, "name": "Название товара"},
            {"id": 20, "name": "Описание товара"},
            {"id": 30, "name": "#Хештеги"},
        ]
        applied = apply_content_attributes(draft, definitions, {
            "title_ru": "Новая игрушка",
            "description_ru": "Описание",
            "tags_ru": ["игрушка для кошек", "мяч"],
        })
        self.assertEqual(len(applied), 3)
        self.assertEqual(draft.attributes[0]["values"][0]["value"], "Новая игрушка")
        self.assertEqual(draft.attributes[0]["_source"], "AI")
        self.assertEqual(draft.attributes[2]["values"][0]["value"], "#игрушка_для_кошек #мяч")

    def test_hashtags_are_normalized_and_limited(self) -> None:
        values = ["уютный дом", "#минимализм", "#минимализм", *[f"тег{i}" for i in range(40)]]
        tags = normalize_hashtags(values)
        self.assertEqual(tags[:2], ["#уютный_дом", "#минимализм"])
        self.assertEqual(len(tags), 30)
        self.assertIn(" ", format_hashtags(tags))

    def test_brand_model_name_and_origin_country_are_system_locked(self) -> None:
        draft = OzonListingDraft(
            source_sku="1", offer_id="AUTO-20260830-1", source_ozon_url="",
            title="", exported_category="",
        )
        enforce_system_attributes(draft, [
            {"id": 85, "name": "品牌", "dictionary_id": 1},
            {"id": 77, "name": "型号名称（针对合并为一张商品卡片）"},
            {"id": 4389, "name": "原产国", "dictionary_id": 1935},
            {"id": 9070, "name": "签名18+"},
            {"id": 10400, "name": "保证", "dictionary_id": 46700526},
        ])
        self.assertEqual(draft.attributes[0]["values"][0]["dictionary_value_id"], 126745801)
        self.assertEqual(draft.attributes[1]["values"][0]["value"], draft.offer_id)
        self.assertEqual(draft.attributes[2]["values"][0]["dictionary_value_id"], 90296)
        self.assertEqual(draft.attributes[3]["values"][0]["value"], "false")
        self.assertEqual(draft.attributes[4]["values"][0]["dictionary_value_id"], 970716397)

    def test_old_draft_content_and_attributes_are_migrated(self) -> None:
        draft = OzonListingDraft(
            source_sku="1", offer_id="AUTO-20260830-1", source_ozon_url="",
            title="Игрушка Secret Lavka скидка", exported_category="",
            description_ru="Secret Lavka хит продаж",
            tags_ru=["игрушка Secret Lavka", "уютный дом"],
            attributes=[
                {"id": 85, "_name": "品牌", "values": [{"value": "Secret Lavka"}]},
                {"id": 9048, "_name": "型号名称（针对合并为一张商品卡片）", "values": []},
                {"id": 11254, "_name": "JSON富内容", "values": [{"value": "普通文本"}]},
            ],
        )
        self.assertTrue(normalize_existing_draft(draft, {"brand": "Secret Lavka"}))
        self.assertNotIn("Secret Lavka", draft.title)
        self.assertEqual(draft.tags_ru, ["#уютный_дом"])
        self.assertEqual(draft.attributes[0]["values"][0]["dictionary_value_id"], 126745801)
        self.assertEqual(draft.attributes[1]["values"][0]["value"], draft.offer_id)
        self.assertFalse(any(item["id"] == 11254 for item in draft.attributes))

    def test_rich_content_json_is_not_filled_without_a_valid_template(self) -> None:
        draft = OzonListingDraft(
            source_sku="1", offer_id="AUTO-1", source_ozon_url="", title="",
            exported_category="",
        )
        applied = apply_content_attributes(draft, [
            {"id": 11254, "name": "JSON富内容"},
        ], {"description_ru": "普通俄文描述"})
        self.assertEqual(applied, [])
        self.assertEqual(draft.attributes, [])

    def test_main_image_prompt_locks_sku_and_text(self) -> None:
        prompt = build_main_image_prompt({
            "poster_text_lines_ru": ["Игрушка для кошек"],
            "creative_direction_en": "A bright playful scene",
            "visual_analysis": {
                "unit_count": 2,
                "unit_count_confidence": 0.95,
                "art_direction_en": "A layered pet-care room scene",
            },
        })
        self.assertIn("uploaded image is the sole authority", prompt)
        self.assertIn('"Игрушка для кошек"', prompt)
        self.assertIn("Never add, remove, recolor", prompt)
        self.assertIn("exactly 2 complete units", prompt)
        self.assertIn("foreground/base", prompt)
        self.assertIn("A layered pet-care room scene", prompt)
        self.assertLessEqual(len(prompt), 4000)

    def test_listing_content_service_executes_generation_path(self) -> None:
        class FakeClient:
            def complete(self, _system, _prompt):
                return {
                    "title_ru": "Игрушка для кошек",
                    "description_ru": "Описание товара",
                    "tags_ru": ["игрушка"],
                    "poster_text_lines_ru": ["Игрушка для кошек"],
                    "creative_direction_en": "Designed pet-care scene",
                    "attributes": [],
                }

        draft = OzonListingDraft(
            source_sku="1", offer_id="AUTO-1", source_ozon_url="",
            title="", exported_category="",
        )
        result = ListingContentService(FakeClient()).generate({}, draft, [])  # type: ignore[arg-type]
        self.assertEqual(result["title_ru"], "Игрушка для кошек")
        self.assertEqual(result["creative_direction_en"], "Designed pet-care scene")

    def test_similar_store_copy_retries_and_accepts_distinct_second_response(self) -> None:
        class FakeClient:
            def __init__(self):
                self.calls = []

            def complete(self, _system, prompt):
                self.calls.append(prompt)
                if len(self.calls) == 1:
                    return {
                        "title_ru": "Игрушка для кошек",
                        "description_ru": "Описание товара для домашних кошек",
                        "tags_ru": [], "poster_text_lines_ru": ["Игрушка"],
                        "creative_direction_en": "same", "attributes": [],
                    }
                return {
                    "title_ru": "Для активной игры питомца — кошачий мяч",
                    "description_ru": "В домашней обстановке предмет помогает разнообразить игру. Видимые детали показаны на фото.",
                    "tags_ru": [], "poster_text_lines_ru": ["Время играть"],
                    "creative_direction_en": "different", "attributes": [],
                }

        client = FakeClient()
        draft = OzonListingDraft(
            source_sku="1", offer_id="AUTO-1", source_ozon_url="", title="", exported_category="",
            ai_preparation={
                "store_creative_profile": {"profile_id": "warm_lifestyle"},
                "must_differ_from_existing_copy": {"primary": {
                    "title_ru": "Игрушка для кошек",
                    "description_ru": "Описание товара для домашних кошек",
                }},
            },
        )
        result = ListingContentService(client).generate({}, draft, [])  # type: ignore[arg-type]
        self.assertEqual(len(client.calls), 2)
        self.assertIn("过于相似", client.calls[1])
        self.assertEqual(result["creative_direction_en"], "different")
        self.assertEqual(result["store_creative_profile"]["profile_id"], "warm_lifestyle")

    def test_similar_store_copy_twice_is_rejected(self) -> None:
        class FakeClient:
            def complete(self, _system, _prompt):
                return {
                    "title_ru": "Игрушка для кошек",
                    "description_ru": "Описание товара для домашних кошек",
                    "tags_ru": [], "poster_text_lines_ru": ["Игрушка"],
                    "creative_direction_en": "same", "attributes": [],
                }

        draft = OzonListingDraft(
            source_sku="1", offer_id="AUTO-1", source_ozon_url="", title="", exported_category="",
            ai_preparation={"must_differ_from_existing_copy": {"primary": {
                "title_ru": "Игрушка для кошек",
                "description_ru": "Описание товара для домашних кошек",
            }}},
        )
        with self.assertRaisesRegex(RuntimeError, "连续两次未达到差异要求"):
            ListingContentService(FakeClient()).generate({}, draft, [])  # type: ignore[arg-type]

    def test_main_image_prompt_enforces_store_visual_profile(self) -> None:
        prompt = build_main_image_prompt({
            "poster_text_lines_ru": ["Точный текст"],
            "creative_direction_en": "A polished scene",
            "store_creative_profile": {
                "composition": "asymmetrical side composition",
                "camera_viewpoint": "25-degree elevated view",
                "background_environment": "layered home setting",
                "palette": "terracotta and deep teal",
                "lighting": "warm window light",
                "typography_placement": "vertical headline on the right",
            },
        })
        self.assertIn("STORE VARIANT ART DIRECTION", prompt)
        self.assertIn("asymmetrical side composition", prompt)
        self.assertIn("terracotta and deep teal", prompt)
        self.assertIn("vertical headline on the right", prompt)
        self.assertIn('"Точный текст"', prompt)
        self.assertLessEqual(len(prompt), 4000)

    def test_main_image_prompt_explicitly_excludes_source_poster_design(self) -> None:
        prompt = build_main_image_prompt({
            "poster_text_lines_ru": ["Точный текст"],
            "visual_analysis": {"source_design_to_avoid": {
                "background_en": "beige vanity with mirror and plant",
                "typography_en": "large heading at the top",
            }},
        })
        self.assertIn("SOURCE POSTER DESIGN EXCLUSION", prompt)
        self.assertIn("beige vanity with mirror and plant", prompt)
        self.assertIn("large heading at the top", prompt)

    def test_product_master_crops_to_detected_product_bbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            Image.new("RGB", (1000, 1200), "white").save(source)
            master = build_product_master(source, {
                "product_bbox_norm": [0.2, 0.25, 0.8, 0.9],
            }, root)
            with Image.open(master) as image:
                self.assertLess(image.width, 1000)
                self.assertLess(image.height, 1200)

    def test_image_difference_score_rejects_identical_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.jpg"
            second = root / "second.jpg"
            Image.new("RGB", (120, 160), "red").save(first)
            Image.new("RGB", (120, 160), "red").save(second)
            self.assertLess(image_difference_score(first, second), 0.001)

    def test_broad_marketing_attribute_accepts_lower_inference_confidence(self) -> None:
        class FakeClient:
            def complete(self, _system, _prompt):
                return {
                    "title_ru": "Кружка",
                    "description_ru": "Описание кружки",
                    "tags_ru": [],
                    "poster_text_lines_ru": ["Кружка"],
                    "creative_direction_en": "A bright kitchen scene",
                    "attributes": [
                        {"attribute_id": 9390, "value": "Взрослая", "confidence": 0.7, "evidence": "Категория товара"},
                        {"attribute_id": 4382, "value": "100 x 100", "confidence": 0.7, "evidence": "Предположение"},
                    ],
                }

        draft = OzonListingDraft(
            source_sku="1", offer_id="AUTO-1", source_ozon_url="", title="", exported_category="",
        )
        result = ListingContentService(FakeClient()).generate({}, draft, [
            {"id": 9390, "name": "目标受众", "dictionary_id": 1183},
            {"id": 4382, "name": "尺寸，毫米"},
        ])  # type: ignore[arg-type]
        self.assertEqual([item["attribute_id"] for item in result["attributes"]], [9390])

    def test_visual_verification_rejects_mismatch(self) -> None:
        service = VisionAnalysisService("https://example.test", "key", "vision")
        service._vision_json = lambda *_args, **_kwargs: {
            "matches_sku": True,
            "confidence": 0.62,
            "reason_ru": "Изменена форма",
        }
        with self.assertRaisesRegex(RuntimeError, "未通过商品一致性校验"):
            service.verify_generated(None, None)

    def test_human_approval_uploads_pending_image_and_marks_preparation_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / "ai_main.jpg"
            Image.new("RGB", (30, 40), "white").save(generated, "JPEG")
            draft = OzonListingDraft(
                source_sku="sku-1", offer_id="AUTO-1", source_ozon_url="",
                title="商品", exported_category="类目",
            )
            draft.ai_preparation = {
                "generated_image_path": str(generated),
                "image_review_status": "PENDING",
                "original_additional_image_urls": [],
            }
            with patch("erp.modules.ozon_follow.legacy.services.ai_listing.DATA_DIR", Path(directory)):
                allowed = Path(directory) / "generated" / draft.source_sku
                allowed.mkdir(parents=True)
                candidate = allowed / generated.name
                generated.replace(candidate)
                draft.ai_preparation["generated_image_path"] = str(candidate)
                with patch.object(OssUploader, "upload", return_value="https://oss.example/main.jpg") as upload:
                    approve_pending_main_image(
                        config=AppConfig(oss_public_base_url="https://oss.example"),
                        draft=draft,
                    )

            upload.assert_called_once()
            self.assertEqual(draft.primary_image, "https://oss.example/main.jpg")
            self.assertTrue(draft.images_oss_ready)
            self.assertEqual(draft.ai_preparation["image_review_status"], "APPROVED")
            self.assertEqual(draft.ai_preparation["image_review_source"], "HUMAN")
            self.assertTrue(draft.ai_preparation["prepared_at"])

    def test_human_approval_preserves_existing_oss_additional_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            draft = OzonListingDraft(
                source_sku="sku-1", offer_id="AUTO-1", source_ozon_url="",
                title="商品", exported_category="类目",
                images=[
                    "https://oss.example/products/detail-1.jpg",
                    "https://oss.example/products/detail-2.jpg",
                ],
            )
            allowed = Path(directory) / "generated" / draft.source_sku
            allowed.mkdir(parents=True)
            candidate = allowed / "ai_main.jpg"
            Image.new("RGB", (30, 40), "white").save(candidate, "JPEG")
            draft.ai_preparation = {
                "generated_image_path": str(candidate),
                "image_review_status": "PENDING",
                # This stale source list must not cause a second attachment upload.
                "original_additional_image_urls": [
                    "https://ir.ozone.ru/s3/multimedia/detail-1.jpg",
                    "https://ir.ozone.ru/s3/multimedia/detail-2.jpg",
                ],
            }
            with patch("erp.modules.ozon_follow.legacy.services.ai_listing.DATA_DIR", Path(directory)):
                with patch.object(
                    OssUploader, "upload", return_value="https://oss.example/products/main.jpg"
                ) as upload:
                    with patch("erp.modules.ozon_follow.legacy.services.ai_listing.download_reference_image") as download:
                        approve_pending_main_image(
                            config=AppConfig(oss_public_base_url="https://oss.example"),
                            draft=draft,
                        )

            upload.assert_called_once()
            download.assert_not_called()
            self.assertEqual(draft.images, [
                "https://oss.example/products/detail-1.jpg",
                "https://oss.example/products/detail-2.jpg",
            ])
            self.assertTrue(draft.images_oss_ready)


if __name__ == "__main__":
    unittest.main()
