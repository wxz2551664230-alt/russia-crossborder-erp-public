from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

from erp.modules.ozon_follow.legacy.services.alibaba_client import AlibabaApiError, AlibabaNewtonClient


class AlibabaNewtonClientTests(unittest.TestCase):
    def test_idempotent_task_get_retries_connect_timeout(self) -> None:
        class Response:
            text = ""

            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                return {"success": True}

        client = AlibabaNewtonClient("key", "secret", attempts=3)
        with patch(
            "erp.modules.ozon_follow.legacy.services.alibaba_client.requests.post",
            side_effect=[requests.ConnectTimeout("timeout"), Response()],
        ) as post:
            with patch("erp.modules.ozon_follow.legacy.services.alibaba_client.time.sleep"):
                result = client.call_api(
                    "com.alibaba.agent", "newtoncloud.task.get", 1, {"taskId": "t1"}
                )

        self.assertTrue(result["success"])
        self.assertEqual(post.call_count, 2)

    def test_task_create_does_not_retry_ambiguous_read_timeout(self) -> None:
        client = AlibabaNewtonClient("key", "secret", attempts=4)
        with patch(
            "erp.modules.ozon_follow.legacy.services.alibaba_client.requests.post",
            side_effect=requests.ReadTimeout("timeout"),
        ) as post:
            with self.assertRaises(AlibabaApiError):
                client.call_api(
                    "com.alibaba.agent", "newtoncloud.task.create", 1, {"message": "x"}
                )

        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
