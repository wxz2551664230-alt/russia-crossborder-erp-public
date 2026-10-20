import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from erp.modules.ozon_follow.legacy.services.elim_client import ElimApiLimitError, ElimClient, normalize_elim_candidates


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self.payload


class ElimClientTests(unittest.TestCase):
    def test_upload_uses_api_key_and_alibaba_platform(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "sample.jpg"
            image.write_bytes(b"jpg")
            with patch("erp.modules.ozon_follow.legacy.services.elim_client.requests.post", return_value=Response({"imageId": "img-1"})) as post:
                result = ElimClient("secret").upload_image(image)
        self.assertEqual(result, "img-1")
        self.assertEqual(post.call_args.kwargs["headers"]["x-api-key"], "secret")
        self.assertEqual(post.call_args.kwargs["data"]["platform"], "alibaba")

    def test_search_uses_supported_image_parameters(self):
        with patch("erp.modules.ozon_follow.legacy.services.elim_client.requests.post", return_value=Response({"items": []})) as post:
            ElimClient("secret").search_by_image("img-1", size=50)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["platform"], "alibaba")
        self.assertEqual(body["size"], 20)
        self.assertNotIn("filter", body)
        self.assertNotIn("lang", body)

    def test_quota_error_is_distinct(self):
        with patch("erp.modules.ozon_follow.legacy.services.elim_client.requests.post", return_value=Response({"message": "Usage quota exhausted"}, 400)):
            with self.assertRaises(ElimApiLimitError):
                ElimClient("secret").search_by_image("img-1")

    def test_normalize_candidates(self):
        search = {"items": [{"id": "123", "title": "搜索标题", "sold": 9, "img_url": "https://cbu01.alicdn.com/a.jpg"}]}
        details = {"123": {"id": "123", "title": "详情标题", "shop_name": "工厂", "extra_info": {"isOnePsale": True}, "skus": [{"id": "sku-1", "price": "12.50", "name": "红色 / M"}]}}
        rows = normalize_elim_candidates(search, details)
        self.assertEqual(rows[0]["itemId"], "123")
        self.assertEqual(rows[0]["skuId"], "sku-1")
        self.assertEqual(rows[0]["realtimeSinglePrice"], "12.50")
        self.assertEqual(rows[0]["imageUrl"], "https://cbu01.alicdn.com/a.jpg")
        self.assertEqual(rows[0]["newtonData"]["min_order_qty"], 1)
        self.assertFalse(rows[0]["newtonData"]["elim_needs_detail"])

    def test_search_only_candidate_defers_detail(self):
        search = {"items": [{
            "id": "456", "title": "商品级候选", "dropship_price": "8.00",
            "img_url": "https://cbu01.alicdn.com/b.jpg", "is_one_psale": True,
        }]}
        rows = normalize_elim_candidates(search, {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["skuId"], "456")
        self.assertTrue(rows[0]["newtonData"]["elim_needs_detail"])


if __name__ == "__main__":
    unittest.main()
