from __future__ import annotations

import unittest
from decimal import Decimal

from erp.modules.ozon_follow.legacy.services.pricing import (
    calculate_actual_sale_for_target_cost_margin,
    calculate_ozon_profit,
)


def test_numeric_zero_is_a_valid_optional_rate():
    result = calculate_ozon_profit(
        actual_sale=100,
        purchase_price=20,
        length_mm=100,
        width_mm=100,
        height_mm=100,
        weight_g=500,
        category_module="家居与汽车用品",
        product_category="装饰、清洁与储物",
        advertising_rate=0,
    )
    assert result.advertising_cost == Decimal("3")


class PricingTests(unittest.TestCase):
    def test_reference_workbook_example_stays_consistent_in_cny(self) -> None:
        result = calculate_ozon_profit(
            actual_sale=37,
            purchase_price=12.8,
            length_mm=100,
            width_mm=100,
            height_mm=30,
            weight_g=100,
            category_module="爱好与运动",
            product_category="运动和休闲用品",
        )
        self.assertEqual(result.ozon_price, Decimal("111.00"))
        self.assertEqual(result.ozon_old_price, Decimal("136.90"))
        self.assertEqual(result.freight, Decimal("6.18"))
        self.assertEqual(result.commission_rate, Decimal("0.12"))
        self.assertEqual(result.commission, Decimal("4.44"))
        self.assertEqual(result.logistics_commission, Decimal("0.74"))
        self.assertEqual(result.return_cost, Decimal("2.448"))
        self.assertEqual(result.profit, Decimal("4.892"))

    def test_target_margin_returns_lowest_cent_that_qualifies(self) -> None:
        result = calculate_actual_sale_for_target_cost_margin(
            target_cost_profit_margin=30,
            purchase_price=12.8,
            length_mm=100,
            width_mm=100,
            height_mm=30,
            weight_g=100,
            category_module="爱好与运动",
            product_category="运动和休闲用品",
        )
        self.assertGreaterEqual(result.cost_profit_margin, Decimal("0.30"))
        previous = calculate_ozon_profit(
            actual_sale=result.actual_sale - Decimal("0.01"),
            purchase_price=12.8,
            length_mm=100,
            width_mm=100,
            height_mm=30,
            weight_g=100,
            category_module="爱好与运动",
            product_category="运动和休闲用品",
        )
        self.assertLess(previous.cost_profit_margin, Decimal("0.30"))


if __name__ == "__main__":
    unittest.main()
