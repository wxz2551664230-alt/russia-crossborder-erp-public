from __future__ import annotations

import json
import unittest
from typing import Any

from erp.modules.ozon_follow.legacy.services.alibaba_client import AlibabaNewtonClient
from erp.modules.ozon_follow.legacy.services.cloud_newton_client import (
    CloudNewtonError,
    CloudNewtonClient,
    NewtonChunk,
    NewtonTaskResult,
    NewtonTaskSnapshot,
    build_ozon_sourcing_message,
    build_selected_data,
)


class FakeAopClient:
    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def call_api(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.responses:
            return self.responses.pop(0)
        return {"success": True}


class CloudNewtonClientTests(unittest.TestCase):
    def test_aop_serializes_complex_parameters_as_json(self) -> None:
        encoded = AlibabaNewtonClient._encode_param([{"question": "颜色", "selected": "红色"}])
        self.assertEqual(json.loads(encoded)[0]["selected"], "红色")
        self.assertEqual(AlibabaNewtonClient._encode_param(False), "false")

    def test_creates_safe_procurement_task(self) -> None:
        fake = FakeAopClient(
            [{"success": True, "taskId": "t1", "sessionId": "s1", "status": "INIT"}]
        )
        client = CloudNewtonClient("k", "s", "t", aop_client=fake)  # type: ignore[arg-type]
        snapshot = client.create_procurement_search(
            "根据图片找10个货源",
            image_urls=["https://example.com/a.jpg"],
            image_names=["a.jpg"],
        )
        self.assertEqual(snapshot.task_id, "t1")
        call = fake.calls[0]
        self.assertEqual(call["api_name"], "newtoncloud.task.create")
        self.assertNotIn("workflowName", call["params"])
        self.assertFalse(call["params"]["auto"])
        self.assertIn(
            "forceSkillId: 1688-supplychain-procurement", call["params"]["message"]
        )
        self.assertIn("不要询盘，不要下单", call["params"]["message"])
        self.assertIn("不要付款", call["params"]["message"])

    def test_collects_incremental_chunks_and_extracts_table(self) -> None:
        table_content = {
            "scene": "newton",
            "subScene": "purchase",
            "id": "table-1",
            "stage": "recall",
        }
        fake = FakeAopClient(
            [
                {
                    "success": True,
                    "taskId": "t1",
                    "sessionId": "s1",
                    "status": "RUNNING",
                    "nextIndex": 1,
                    "chunks": json.dumps(
                        [{"type": "text", "content": "正在找货", "timestamp": 1}]
                    ),
                },
                {
                    "success": True,
                    "taskId": "t1",
                    "sessionId": "s1",
                    "status": "END",
                    "nextIndex": 2,
                    "chunks": json.dumps(
                        [
                            {
                                "type": "complex_table",
                                "content": json.dumps(table_content),
                                "timestamp": 2,
                            }
                        ]
                    ),
                },
            ]
        )
        client = CloudNewtonClient("k", "s", "t", aop_client=fake)  # type: ignore[arg-type]
        result = client.collect_task("t1", poll_interval=0, timeout_seconds=1)
        self.assertIsInstance(result, NewtonTaskResult)
        assert isinstance(result, NewtonTaskResult)
        self.assertEqual(len(result.chunks), 2)
        reference = result.table_references()[0]
        self.assertEqual(reference.table_id, "table-1")
        self.assertEqual(fake.calls[1]["params"]["fromIndex"], 1)

    def test_wait_user_returns_snapshot_with_questions(self) -> None:
        fake = FakeAopClient(
            [
                {
                    "success": True,
                    "taskId": "t2",
                    "sessionId": "s2",
                    "status": "WAIT_USER",
                    "nextIndex": 1,
                    "chunks": [
                        {
                            "type": "wait_user",
                            "content": json.dumps(
                                {
                                    "questions": [
                                        {"question": "采购多少个？", "isRequired": True}
                                    ]
                                }
                            ),
                        }
                    ],
                }
            ]
        )
        client = CloudNewtonClient("k", "s", "t", aop_client=fake)  # type: ignore[arg-type]
        result = client.collect_task("t2", poll_interval=0, timeout_seconds=1)
        self.assertIsInstance(result, NewtonTaskSnapshot)
        assert isinstance(result, NewtonTaskSnapshot)
        self.assertEqual(result.questions()[0]["question"], "采购多少个？")
        self.assertEqual(
            build_selected_data([("采购多少个？", "10个")]),
            [{"question": "采购多少个？", "selected": "10个"}],
        )

    def test_result_tables_prefer_latest_inline_data(self) -> None:
        fake = FakeAopClient()
        client = CloudNewtonClient("k", "s", "t", aop_client=fake)  # type: ignore[arg-type]
        common = {"scene": "newton", "subScene": "purchase", "id": "table-1"}
        result = NewtonTaskResult(
            task_id="t1",
            session_id="s1",
            status="END",
            chunks=[
                NewtonChunk(type="complex_table", content={**common, "data": [{"id": 1}]}),
                NewtonChunk(
                    type="complex_table",
                    content={**common, "__dataFilled": True, "data": [{"id": 1}, {"id": 2}]},
                ),
            ],
            next_index=2,
            raw={},
        )

        self.assertEqual(client.fetch_result_tables(result), [[{"id": 1}, {"id": 2}]])
        self.assertEqual(fake.calls, [])

    def test_preserves_machine_error_code(self) -> None:
        fake = FakeAopClient(
            [
                {
                    "success": False,
                    "errorCode": "RATE_LIMIT_QUEUE_FULL",
                    "error": "排队队列已满，请稍后再试。",
                }
            ]
        )
        client = CloudNewtonClient("k", "s", "t", aop_client=fake)  # type: ignore[arg-type]
        with self.assertRaisesRegex(CloudNewtonError, "RATE_LIMIT_QUEUE_FULL"):
            client.create_task("找货源")
        self.assertFalse(fake.calls[0]["raise_api_errors"])

    def test_classifies_credit_shortage_without_platform_error_code(self) -> None:
        fake = FakeAopClient(
            [{"success": False, "message": "积分不足，请前往牛顿客户端领取"}]
        )
        client = CloudNewtonClient("k", "s", "t", aop_client=fake)  # type: ignore[arg-type]
        with self.assertRaisesRegex(CloudNewtonError, "INSUFFICIENT_CREDITS"):
            client.create_task("找货源")

    def test_builds_ozon_sourcing_prompt_without_guessing(self) -> None:
        message = build_ozon_sourcing_message(
            title="宠物玩具",
            ozon_url="https://www.ozon.ru/product/123",
            image_url="https://example.com/pet-toy.jpg",
            candidate_count=12,
        )
        self.assertIn("寻找12个", message)
        self.assertIn("https://example.com/pet-toy.jpg", message)
        self.assertIn("宠物玩具", message)
        self.assertNotIn("www.ozon.ru", message)
        self.assertIn("商品主图为首要证据", message)
        self.assertIn("仅用于确认实际销售主体，不用于替代图片匹配", message)
        self.assertIn("展示道具", message)
        self.assertIn("全部SKU规格组合", message)
        self.assertIn("不允许推测", message)


if __name__ == "__main__":
    unittest.main()
