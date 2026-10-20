from __future__ import annotations

import unittest
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from erp.modules.ozon_follow.legacy.services.ozon_gallery import (
    _extension_capture_url,
    _open_normal_browser,
    _recent_extension_gallery,
    normalize_gallery_images,
)


class OzonGalleryTests(unittest.TestCase):
    def test_normalizes_current_ozon_china_cdn(self) -> None:
        images = normalize_gallery_images([
            "https://ir-20.ozonstatic.cn/s3/multimedia-1-x/wc100/13700035437.jpg",
            "https://ir-20.ozonstatic.cn/s3/multimedia-1-x/wc1000/13700035437.jpg",
        ])
        self.assertEqual(images, ["https://ir-20.ozonstatic.cn/s3/multimedia-1-x/wc2000/13700035437.jpg"])

    def test_program_capture_url_is_marked_without_changing_product_or_query(self) -> None:
        source = "https://www.ozon.ru/product/example-123456/?from=share"
        self.assertEqual(
            _extension_capture_url(source),
            "https://www.ozon.ru/product/example-123456/"
            "?from=share&__ozon_gallery_bridge=1"
            "#ozon-gallery-bridge-auto-close",
        )

    def test_windows_browser_open_uses_visible_system_handler(self) -> None:
        with patch("erp.modules.ozon_follow.legacy.services.ozon_gallery.os.name", "nt"):
            with patch("erp.modules.ozon_follow.legacy.services.ozon_gallery.os.startfile", create=True) as startfile:
                with patch("erp.modules.ozon_follow.legacy.services.ozon_gallery.webbrowser.open") as fallback:
                    self.assertTrue(_open_normal_browser("https://www.ozon.ru/product/123456/"))
        startfile.assert_called_once()
        fallback.assert_not_called()

    def test_recent_frontend_capture_is_reused_before_backend_opens_another_tab(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sku.json"
            target.write_text(json.dumps({
                "captured_at": time.time(),
                "page_url": "https://www.ozon.ru/product/example-123456/",
                "title": "商品",
                "images": ["https://ir.ozone.ru/s3/multimedia-1-a/wc1000/image.jpg"],
            }), encoding="utf-8")
            gallery = _recent_extension_gallery(target, "https://www.ozon.ru/product/123456/")
        self.assertIsNotNone(gallery)
        self.assertEqual(len(gallery.images), 1)

if __name__ == "__main__":
    unittest.main()
