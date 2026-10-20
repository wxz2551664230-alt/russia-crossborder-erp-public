from __future__ import annotations

import unittest

from erp.modules.ozon_follow.legacy.services.sourcing_selector import (
    ExactMatchDecision,
    MatchVerdict,
    build_manual_review_candidates,
    build_exact_match_verification_message,
    parse_exact_match_decisions,
    parse_exact_match_decision_chunks,
    select_best_source,
)


def row(
    item_id: str,
    *,
    price: float = 10,
    delivery: float | None = 2,
    sales: str | int | None = "10+",
    moq: str = "1",
) -> dict:
    return {
        "itemId": item_id,
        "skuId": f"sku-{item_id}",
        "title": f"商品{item_id}",
        "skuName": "同款规格",
        "imageUrl": f"https://example.com/{item_id}.jpg",
        "realtimeSinglePrice": price,
        "realtimeDeliveryPrice": delivery,
        "salesCountNum": sales,
        "newtonData": {"min_order_qty": moq},
    }


def exact(item_id: str, confidence: float = 0.99) -> ExactMatchDecision:
    return ExactMatchDecision(
        item_id=item_id,
        sku_id=f"sku-{item_id}",
        verdict=MatchVerdict.EXACT,
        confidence=confidence,
    )


class SourcingSelectorTests(unittest.TestCase):
    def test_requires_explicit_high_confidence_exact_match(self) -> None:
        rows = [row("1"), row("2", price=5)]
        decisions = [
            exact("1"),
            ExactMatchDecision(
                "2", "sku-2", MatchVerdict.UNCERTAIN, 0.99, "结构看不清"
            ),
        ]
        result = select_best_source(rows, decisions)
        self.assertEqual(result.selected.item_id, "1")  # type: ignore[union-attr]
        self.assertEqual(result.rejected[("2", "sku-2")], "结构看不清")

    def test_accepts_exact_match_at_ninety_percent(self) -> None:
        result = select_best_source([row("1")], [exact("1", 0.90)])
        self.assertEqual(result.status, "SELECTED")
        self.assertEqual(result.selected.item_id, "1")  # type: ignore[union-attr]

    def test_rejects_exact_match_below_ninety_percent(self) -> None:
        result = select_best_source([row("1")], [exact("1", 0.899)])
        self.assertEqual(result.status, "NO_EXACT_MATCH")
        self.assertIn("置信度不足", result.rejected[("1", "sku-1")])

    def test_requires_one_piece_wholesale(self) -> None:
        result = select_best_source([row("1", moq="2")], [exact("1")])
        self.assertEqual(result.status, "NO_EXACT_MATCH")
        self.assertIn("一件起批", result.rejected[("1", "sku-1")])

    def test_manual_review_keeps_every_one_piece_candidate_without_ai_score(self) -> None:
        candidates = build_manual_review_candidates([row("1")], [exact("1", 0.899)])
        self.assertEqual([(item["itemId"], item["confidence"]) for item in candidates], [("1", None)])

    def test_manual_review_excludes_non_one_piece_candidate(self) -> None:
        candidates = build_manual_review_candidates([row("1", moq="2")], [exact("1", 0.899)])
        self.assertEqual(candidates, [])

    def test_manual_review_ignores_old_ai_verdicts(self) -> None:
        decision = ExactMatchDecision("1", "sku-1", MatchVerdict.NOT_EXACT, 0.50, "结构不同")
        candidates = build_manual_review_candidates([row("1")], [decision])
        self.assertEqual(candidates[0]["verdict"], "HUMAN_REVIEW")

    def test_manual_review_keeps_candidate_regardless_of_old_confidence(self) -> None:
        decision = ExactMatchDecision("1", "sku-1", MatchVerdict.NOT_EXACT, 0.90, "结构不同")
        self.assertEqual(len(build_manual_review_candidates([row("1")], [decision])), 1)

    def test_manual_review_preserves_newton_relevance_order(self) -> None:
        candidates = build_manual_review_candidates([
            row("1", price=1, delivery=0, sales=0),
            row("2", price=9, delivery=2, sales=5),
            row("3", price=8, delivery=2, sales=1),
        ])
        self.assertEqual([item["itemId"] for item in candidates], ["1", "2", "3"])

    def test_price_plus_delivery_wins_before_sales(self) -> None:
        rows = [
            row("1", price=10, delivery=2, sales="10+"),
            row("2", price=8, delivery=3, sales="1"),
            row("3", price=7, delivery=5, sales="3000+"),
        ]
        result = select_best_source(rows, [exact("1"), exact("2"), exact("3")])
        self.assertEqual(result.selected.item_id, "2")  # type: ignore[union-attr]

    def test_zero_sales_is_excluded_when_any_candidate_has_sales(self) -> None:
        rows = [row("1", price=1, delivery=0, sales=0), row("2", price=10, sales=1)]
        result = select_best_source(rows, [exact("1"), exact("2")])
        self.assertEqual(result.selected.item_id, "2")  # type: ignore[union-attr]

    def test_all_zero_sales_falls_back_to_lowest_landed_cost(self) -> None:
        rows = [row("1", price=8, delivery=4, sales=0), row("2", price=9, delivery=1, sales=0)]
        result = select_best_source(rows, [exact("1"), exact("2")])
        self.assertEqual(result.selected.item_id, "2")  # type: ignore[union-attr]

    def test_unknown_sales_is_not_assumed_to_be_zero(self) -> None:
        result = select_best_source([row("1", sales=None)], [exact("1")])
        self.assertEqual(result.status, "INSUFFICIENT_DATA")
        self.assertIsNone(result.selected)

    def test_confirmed_zero_sales_wins_over_unknown_when_no_positive_sales(self) -> None:
        rows = [row("1", price=1, sales=None), row("2", price=10, sales=0)]
        result = select_best_source(rows, [exact("1"), exact("2")])
        self.assertEqual(result.selected.item_id, "2")  # type: ignore[union-attr]

    def test_sales_breaks_equal_cost_tie(self) -> None:
        rows = [row("1", price=8, delivery=2, sales=10), row("2", price=9, delivery=1, sales=20)]
        result = select_best_source(rows, [exact("1"), exact("2")])
        self.assertEqual(result.selected.item_id, "2")  # type: ignore[union-attr]

    def test_missing_delivery_is_not_assumed_to_be_zero(self) -> None:
        result = select_best_source([row("1", delivery=None)], [exact("1")])
        self.assertIsNone(result.selected)
        self.assertIn("运费", result.rejected[("1", "sku-1")])

    def test_builds_and_parses_strict_verification_payload(self) -> None:
        message = build_exact_match_verification_message(
            reference_title="Ozon猫玩具",
            reference_image_url="https://ozon.example/main.jpg",
            rows=[row("1")],
        )
        self.assertIn("宁可没有结果", message)
        self.assertIn("https://example.com/1.jpg", message)
        decisions = parse_exact_match_decisions(
            '```json\n[{"itemId":"1","skuId":"sku-1","verdict":"EXACT",'
            '"confidence":0.98,"reason":"结构一致"}]\n```'
        )
        self.assertEqual(decisions[0].verdict, MatchVerdict.EXACT)
        self.assertEqual(decisions[0].confidence, 0.98)

    def test_parses_tokenized_stream_with_reasoning_before_json(self) -> None:
        decisions = parse_exact_match_decision_chunks([
            "先分析候选。```json\n[",
            '{"itemId":"1","skuId":"sku-1","verdict":"EXACT",',
            '"confidence":0.99,"reason":"结构一致"}]\n```',
        ])
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].verdict, MatchVerdict.EXACT)


if __name__ == "__main__":
    unittest.main()
