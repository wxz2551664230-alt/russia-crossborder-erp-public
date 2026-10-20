from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from erp.modules.ozon_follow.legacy.services.daji_client import DajiClient, normalize_daji_candidates


class DajiClientTests(unittest.TestCase):
    def test_signature_matches_documented_sorted_md5_rule(self) -> None:
        client = DajiClient("key", "secret")
        # MD5("a=1&appKey=key&z=2&secret=secret"), uppercase.
        self.assertEqual(
            client.sign({"z": 2, "appKey": "key", "a": 1}),
            "E53998EAD58E8DC4D7C2E4F1C664C6DE",
        )

    def test_upload_sends_base64_and_returns_image_id(self) -> None:
        class Response:
            status_code = 200
            text = ""

            @staticmethod
            def raise_for_status() -> None: pass

            @staticmethod
            def json() -> dict: return {"code": 200, "data": {"imageId": "img-1"}}

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "source.jpg"
            image.write_bytes(b"image")
            with patch("erp.modules.ozon_follow.legacy.services.daji_client.requests.request", return_value=Response()) as request:
                result = DajiClient("key", "secret").upload_image(image)

        self.assertEqual(result, "img-1")
        data = request.call_args.kwargs["data"]
        self.assertEqual(data["image_base64"], "aW1hZ2U=")
        self.assertIn("sign", data)
        self.assertNotIn("appSecret", data)

    def test_normalizes_one_piece_skus_for_existing_review_flow(self) -> None:
        search = {"code": 200, "data": {"data": [{
            "offerId": 123,
            "subject": "测试商品",
            "imageUrl": "https://example.test/main.jpg",
            "isOnePsale": True,
            "monthSold": 99,
            "priceInfo": {"consignPrice": "12.50"},
        }]}}
        details = {"123": {"code": 200, "data": {
            "subject": "测试商品详情",
            "productSkuInfos": [{
                "skuId": "sku-1",
                "amountOnSale": 20,
                "price": "13.00",
                "skuAttributes": [{"attributeName": "颜色", "value": "红色", "skuImageUrl": "https://example.test/red.jpg"}],
            }],
        }}}
        rows = normalize_daji_candidates(search, details, {("123", "sku-1"): "3.50"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["itemId"], "123")
        self.assertEqual(rows[0]["skuId"], "sku-1")
        self.assertEqual(rows[0]["skuName"], "颜色:红色")
        self.assertEqual(rows[0]["imageUrl"], "https://example.test/red.jpg")
        self.assertEqual(rows[0]["realtimeSinglePrice"], "13.00")
        self.assertEqual(rows[0]["realtimeDeliveryPrice"], "3.50")
        self.assertEqual(rows[0]["newtonData"]["min_order_qty"], 1)

    def test_http_problem_detail_is_exposed(self) -> None:
        class Response:
            status_code = 400
            text = '{"detail":"签名验证失败"}'

            @staticmethod
            def json() -> dict: return {"detail": "签名验证失败"}

        with patch("erp.modules.ozon_follow.legacy.services.daji_client.requests.request", return_value=Response()):
            with self.assertRaisesRegex(Exception, "签名验证失败"):
                DajiClient("key", "secret").product_detail("123")


if __name__ == "__main__":
    unittest.main()
