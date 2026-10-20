from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP


TWO_PLACES = Decimal("0.01")
THREE_PLACES = Decimal("0.001")
OZON_PRICE_MULTIPLIER = Decimal("3")
OZON_OLD_PRICE_MULTIPLIER = Decimal("3.7")

# Extracted from the template's visible RFBS tariff table.  The module list
# follows the template's Sheet2 validation list, not the Ozon upload taxonomy.
OZON_RFBS_COMMISSION_TARIFFS: dict[str, dict[str, tuple[Decimal, Decimal, Decimal]]] = {
    "家居与汽车用品": {
        "装饰、清洁与储物": (Decimal("0.12"), Decimal("0.14"), Decimal("0.18")),
        "住宅和花园": (Decimal("0.12"), Decimal("0.14"), Decimal("0.20")),
        "汽车用品": (Decimal("0.12"), Decimal("0.17"), Decimal("0.17")),
        "手动工具和测量仪器": (Decimal("0.12"), Decimal("0.17"), Decimal("0.17")),
        "建筑和装修": (Decimal("0.12"), Decimal("0.18"), Decimal("0.18")),
        "康复设备": (Decimal("0.12"), Decimal("0.14"), Decimal("0.17")),
        "重型建筑": (Decimal("0.11"), Decimal("0.11"), Decimal("0.11")),
        "儿童餐具": (Decimal("0.12"), Decimal("0.14"), Decimal("0.18")),
        "家具": (Decimal("0.10"), Decimal("0.10"), Decimal("0.10")),
        "轮胎": (Decimal("0.10"), Decimal("0.10"), Decimal("0.10")),
        "装饰材料": (Decimal("0.12"), Decimal("0.14"), Decimal("0.14")),
        "卫浴设备": (Decimal("0.12"), Decimal("0.14"), Decimal("0.14")),
        "日化": (Decimal("0.12"), Decimal("0.18"), Decimal("0.18")),
        "建筑、装修和园艺设备": (Decimal("0.12"), Decimal("0.16"), Decimal("0.16")),
        "新年装饰用品": (Decimal("0.12"), Decimal("0.14"), Decimal("0.20")),
        "电动滑板车": (Decimal("0.12"), Decimal("0.17"), Decimal("0.17")),
        "船只、马达和充气艇": (Decimal("0.12"), Decimal("0.15"), Decimal("0.15")),
        "自行车": (Decimal("0.12"), Decimal("0.15"), Decimal("0.15")),
        "水过滤器": (Decimal("0.12"), Decimal("0.17"), Decimal("0.17")),
        "运动手表": (Decimal("0.12"), Decimal("0.12"), Decimal("0.12")),
        "成品房": (Decimal("0.12"), Decimal("0.145"), Decimal("0.145")),
        "汽车、汽车房和特种设备": (Decimal("0.10"), Decimal("0.10"), Decimal("0.10")),
    },
    "美容": {
        "服装和配饰": (Decimal("0.12"), Decimal("0.14"), Decimal("0.205")),
        "鞋类": (Decimal("0.12"), Decimal("0.12"), Decimal("0.12")),
        "美容与健康": (Decimal("0.12"), Decimal("0.14"), Decimal("0.18")),
        "专业口腔护理": (Decimal("0.12"), Decimal("0.17"), Decimal("0.17")),
        "外衣": (Decimal("0.10"), Decimal("0.10"), Decimal("0.10")),
        "专业医疗设备": (Decimal("0.12"), Decimal("0.17"), Decimal("0.17")),
    },
    "儿童用品": {
        "儿童纺织品": (Decimal("0.12"), Decimal("0.19"), Decimal("0.19")),
        "儿童运动用品": (Decimal("0.12"), Decimal("0.14"), Decimal("0.14")),
        "儿童电子产品、家具、配件": (Decimal("0.12"), Decimal("0.14"), Decimal("0.20")),
        "玩具": (Decimal("0.12"), Decimal("0.14"), Decimal("0.175")),
        "儿童卫生用品": (Decimal("0.12"), Decimal("0.18"), Decimal("0.18")),
        "婴儿推车和汽车安全座椅": (Decimal("0.12"), Decimal("0.14"), Decimal("0.20")),
    },
    "宠物用品": {
        "宠物饲料与农场用品": (Decimal("0.12"), Decimal("0.13"), Decimal("0.13")),
        "宠物用品": (Decimal("0.12"), Decimal("0.14"), Decimal("0.15")),
        "宠物卫生与护理": (Decimal("0.12"), Decimal("0.13"), Decimal("0.13")),
    },
    "爱好与运动": {
        "运动和休闲用品": (Decimal("0.12"), Decimal("0.19"), Decimal("0.19")),
        "兴趣、创意与文具": (Decimal("0.12"), Decimal("0.14"), Decimal("0.16")),
        "书籍": (Decimal("0.12"), Decimal("0.22"), Decimal("0.22")),
        "蹦床、游泳池和立式桨板": (Decimal("0.12"), Decimal("0.16"), Decimal("0.16")),
        "运动营养": (Decimal("0.12"), Decimal("0.15"), Decimal("0.15")),
        "运动员营养补充剂": (Decimal("0.12"), Decimal("0.18"), Decimal("0.18")),
    },
    "电子产品": {
        "电子产品配饰": (Decimal("0.12"), Decimal("0.20"), Decimal("0.20")),
        "音频和视频设备配件": (Decimal("0.12"), Decimal("0.145"), Decimal("0.145")),
        "家用电器": (Decimal("0.10"), Decimal("0.10"), Decimal("0.10")),
        "电视机": (Decimal("0.09"), Decimal("0.09"), Decimal("0.09")),
        "美容设备": (Decimal("0.12"), Decimal("0.14"), Decimal("0.16")),
        "办公电脑设备、收银及仓储设备": (Decimal("0.12"), Decimal("0.16"), Decimal("0.16")),
        "游戏主机及配件、摄影器材": (Decimal("0.12"), Decimal("0.125"), Decimal("0.125")),
        "电脑外设设备及耗材": (Decimal("0.12"), Decimal("0.145"), Decimal("0.145")),
        "非内置式大型家用电器": (Decimal("0.09"), Decimal("0.09"), Decimal("0.09")),
        "智能手机和平板电脑": (Decimal("0.115"), Decimal("0.115"), Decimal("0.115")),
        "电脑及笔记本配件": (Decimal("0.12"), Decimal("0.125"), Decimal("0.125")),
        "Yandex 智能音箱": (Decimal("0.12"), Decimal("0.145"), Decimal("0.145")),
        "嵌入式大型家用电器": (Decimal("0.09"), Decimal("0.09"), Decimal("0.09")),
        "显示器": (Decimal("0.12"), Decimal("0.125"), Decimal("0.125")),
        "智能手表与健身手环": (Decimal("0.115"), Decimal("0.115"), Decimal("0.115")),
        "电子游戏": (Decimal("0.12"), Decimal("0.145"), Decimal("0.145")),
        "台式电脑": (Decimal("0.09"), Decimal("0.09"), Decimal("0.09")),
        "电脑设备配件": (Decimal("0.12"), Decimal("0.135"), Decimal("0.135")),
        "笔记本电脑": (Decimal("0.08"), Decimal("0.08"), Decimal("0.08")),
        "戴森配件": (Decimal("0.06"), Decimal("0.06"), Decimal("0.06")),
        "索尼耳机": (Decimal("0.08"), Decimal("0.08"), Decimal("0.08")),
        "三星 TWS 耳机": (Decimal("0.08"), Decimal("0.08"), Decimal("0.08")),
        "三星智能手表与健身手环": (Decimal("0.08"), Decimal("0.08"), Decimal("0.08")),
        "三星智能手机和平板电脑": (Decimal("0.08"), Decimal("0.08"), Decimal("0.08")),
        "苹果设备": (Decimal("0.07"), Decimal("0.07"), Decimal("0.07")),
        "戴森设备": (Decimal("0.08"), Decimal("0.08"), Decimal("0.08")),
    },
}


@dataclass(frozen=True)
class OzonProfitResult:
    actual_sale: Decimal
    ozon_price: Decimal
    ozon_old_price: Decimal
    purchase_price: Decimal
    weight_kg: Decimal
    freight: Decimal
    sticker_fee: Decimal
    logistics_commission: Decimal
    commission_rate: Decimal
    commission: Decimal
    advertising_cost: Decimal
    return_rate: Decimal
    return_cost: Decimal
    profit: Decimal
    profit_margin: Decimal
    cost_profit_margin: Decimal


def profit_category_modules() -> tuple[str, ...]:
    return tuple(OZON_RFBS_COMMISSION_TARIFFS)


def profit_product_categories(module_name: str) -> tuple[str, ...]:
    return tuple(OZON_RFBS_COMMISSION_TARIFFS.get(str(module_name or "").strip(), {}))


def _number(value, label: str, *, positive: bool = False) -> Decimal:
    try:
        text = "" if value is None else str(value).strip()
        number = Decimal(text)
    except (InvalidOperation, ValueError) as error:
        raise ValueError(label + "必须是有效数字") from error
    if not number.is_finite():
        raise ValueError(label + "必须是有限数字")
    if positive and number <= 0:
        raise ValueError(label + "必须大于 0")
    if not positive and number < 0:
        raise ValueError(label + "不能小于 0")
    return number


def _round_currency(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _round_up_currency(value: Decimal) -> Decimal:
    return (value / TWO_PLACES).to_integral_value(rounding=ROUND_CEILING) * TWO_PLACES


def ozon_prices_from_actual_sale(
    actual_sale,
    *,
    price_multiplier=OZON_PRICE_MULTIPLIER,
    old_price_multiplier=OZON_OLD_PRICE_MULTIPLIER,
) -> tuple[Decimal, Decimal]:
    actual = _number(actual_sale, "实际成交基准价", positive=True)
    price_factor = _number(price_multiplier, "售价倍数", positive=True)
    old_price_factor = _number(old_price_multiplier, "折扣前价格倍数", positive=True)
    if old_price_factor <= price_factor:
        raise ValueError("折扣前价格倍数必须大于售价倍数")
    return _round_currency(actual * price_factor), _round_currency(actual * old_price_factor)


def _shipping_cost(actual_sale: Decimal, weight_kg: Decimal, dimensions_cm: tuple[Decimal, Decimal, Decimal]) -> Decimal:
    weight = (weight_kg / THREE_PLACES).to_integral_value(rounding=ROUND_CEILING) * THREE_PLACES
    dimension_sum = sum(dimensions_cm, Decimal("0"))
    longest_edge = max(dimensions_cm)
    length_cm, width_cm, height_cm = dimensions_cm
    # The source workbook contains R2*YN2*T2.  YN is unrelated and blank; the
    # intended volumetric-weight formula is length * width * height / 12000.
    volumetric_weight = max(weight, length_cm * width_cm * height_cm / Decimal("12000"))

    if actual_sale <= Decimal("135"):
        if weight <= Decimal("0.55"):
            if dimension_sum >= Decimal("90"):
                raise ValueError("三边合计超长（当前档位必须小于 90 cm）")
            if longest_edge > Decimal("60"):
                raise ValueError("单边超长（当前档位不能超过 60 cm）")
            return _round_up_currency(Decimal("28.1") * weight + Decimal("3.37"))
        if weight > Decimal("30"):
            raise ValueError("重量超过 30 kg 限制")
        if dimension_sum >= Decimal("150"):
            raise ValueError("三边合计超长（当前档位必须小于 150 cm）")
        if longest_edge > Decimal("60"):
            raise ValueError("单边超长（当前档位不能超过 60 cm）")
        return _round_up_currency(Decimal("19.1") * weight + Decimal("25.83"))

    if actual_sale <= Decimal("635"):
        if weight <= Decimal("2.2"):
            if dimension_sum >= Decimal("150"):
                raise ValueError("三边合计超长（当前档位必须小于 150 cm）")
            if longest_edge > Decimal("60"):
                raise ValueError("单边超长（当前档位不能超过 60 cm）")
            return _round_up_currency(Decimal("28.1") * weight + Decimal("17.97"))
        if volumetric_weight > Decimal("30"):
            raise ValueError("体积重或实际重超过 30 kg 限制")
        if dimension_sum >= Decimal("250"):
            raise ValueError("三边合计超长（当前档位必须小于 250 cm）")
        if longest_edge > Decimal("150"):
            raise ValueError("单边超长（当前档位不能超过 150 cm）")
        return _round_up_currency(Decimal("19.1") * volumetric_weight + Decimal("40.44"))

    if weight <= Decimal("5.5"):
        if dimension_sum >= Decimal("250"):
            raise ValueError("三边合计超长（当前档位必须小于 250 cm）")
        if longest_edge > Decimal("150"):
            raise ValueError("单边超长（当前档位不能超过 150 cm）")
        return _round_up_currency(Decimal("28.1") * weight + Decimal("24.71"))
    if volumetric_weight > Decimal("30"):
        raise ValueError("体积重或实际重超过 30 kg 限制")
    if dimension_sum >= Decimal("310"):
        raise ValueError("三边合计超长（当前档位必须小于 310 cm）")
    if longest_edge > Decimal("150"):
        raise ValueError("单边超长（当前档位不能超过 150 cm）")
    return _round_up_currency(Decimal("25.8") * volumetric_weight + Decimal("69.64"))


def calculate_ozon_profit(
    *,
    actual_sale,
    purchase_price,
    length_mm,
    width_mm,
    height_mm,
    weight_g,
    category_module: str,
    product_category: str,
    sticker_fee="2.5",
    advertising_cost="3",
    advertising_rate="0",
    return_rate="10",
    logistics_service_rate="2",
    price_multiplier="3",
    old_price_multiplier="3.7",
) -> OzonProfitResult:
    actual = _number(actual_sale, "实际成交基准价", positive=True)
    ozon_price, ozon_old_price = ozon_prices_from_actual_sale(
        actual,
        price_multiplier=price_multiplier,
        old_price_multiplier=old_price_multiplier,
    )
    purchase = _number(purchase_price, "1688采购到手成本")
    sticker = _number(sticker_fee, "支付及贴单费用")
    fixed_advertising = _number(advertising_cost, "固定广告及运营费用")
    advertising_rate_percent = _number(advertising_rate, "广告份额")
    if advertising_rate_percent > Decimal("100"):
        raise ValueError("广告份额必须在 0% 到 100% 之间")
    advertising = fixed_advertising + actual * advertising_rate_percent / Decimal("100")
    return_rate_percent = _number(return_rate, "退货率")
    if return_rate_percent > Decimal("100"):
        raise ValueError("退货率必须在 0% 到 100% 之间")
    return_rate_decimal = return_rate_percent / Decimal("100")
    weight = _number(weight_g, "包装毛重", positive=True) / Decimal("1000")
    dimensions = tuple(
        _number(value, label, positive=True) / Decimal("10")
        for value, label in (
            (length_mm, "包装长度"),
            (width_mm, "包装宽度"),
            (height_mm, "包装高度"),
        )
    )

    module_name = str(category_module or "").strip()
    category_name = str(product_category or "").strip()
    category_tariffs = OZON_RFBS_COMMISSION_TARIFFS.get(module_name)
    if not category_tariffs:
        raise ValueError("请人工选择利润计算的类目模块")
    rates = category_tariffs.get(category_name)
    if not rates:
        raise ValueError("请从当前类目模块中人工选择利润计算的商品类目")

    # The reference workbook uses RUB 1,500/5,000 bands with a fixed 0.09
    # calculation factor.  This RMB-only implementation stores the equivalent
    # thresholds directly and performs no exchange-rate conversion.
    if actual <= Decimal("135"):
        commission_rate = rates[0]
    elif actual <= Decimal("450"):
        commission_rate = rates[1]
    else:
        commission_rate = rates[2]

    freight = _shipping_cost(actual, weight, dimensions)
    logistics_rate_percent = _number(logistics_service_rate, "物流服务费率")
    if logistics_rate_percent > Decimal("100"):
        raise ValueError("物流服务费率必须在 0% 到 100% 之间")
    logistics_commission = actual * logistics_rate_percent / Decimal("100")
    commission = _round_currency(actual * commission_rate)
    return_cost = (purchase + freight + sticker + advertising) * return_rate_decimal
    profit = actual - purchase - freight - sticker - commission - advertising - return_cost - logistics_commission
    profit_margin = profit / actual
    cost_base = purchase + sticker
    cost_profit_margin = profit / cost_base if cost_base else Decimal("0")
    return OzonProfitResult(
        actual_sale=actual,
        ozon_price=ozon_price,
        ozon_old_price=ozon_old_price,
        purchase_price=purchase,
        weight_kg=weight,
        freight=freight,
        sticker_fee=sticker,
        logistics_commission=logistics_commission,
        commission_rate=commission_rate,
        commission=commission,
        advertising_cost=advertising,
        return_rate=return_rate_decimal,
        return_cost=return_cost,
        profit=profit,
        profit_margin=profit_margin,
        cost_profit_margin=cost_profit_margin,
    )


def calculate_actual_sale_for_target_cost_margin(
    *,
    target_cost_profit_margin,
    purchase_price,
    length_mm,
    width_mm,
    height_mm,
    weight_g,
    category_module: str,
    product_category: str,
    sticker_fee="2.5",
    advertising_cost="3",
    advertising_rate="0",
    return_rate="10",
    logistics_service_rate="2",
    price_multiplier="3",
    old_price_multiplier="3.7",
) -> OzonProfitResult:
    """Return the lowest cent-denominated actual sale meeting the target margin."""
    target_percent = _number(target_cost_profit_margin, "目标成本利润率")
    if target_percent > Decimal("10000"):
        raise ValueError("目标成本利润率不能超过 10000%")
    target_margin = target_percent / Decimal("100")
    purchase = _number(purchase_price, "拿货价")
    sticker = _number(sticker_fee, "贴单费")
    if purchase + sticker <= 0:
        raise ValueError("反算实际售价时，拿货价与贴单费之和必须大于 0")

    shipping_error_prefixes = (
        "三边合计超长", "单边超长", "重量超过", "体积重或实际重超过",
    )

    def result_at(cents: int) -> OzonProfitResult | None:
        try:
            return calculate_ozon_profit(
                actual_sale=Decimal(cents) / Decimal("100"),
                purchase_price=purchase,
                length_mm=length_mm,
                width_mm=width_mm,
                height_mm=height_mm,
                weight_g=weight_g,
                category_module=category_module,
                product_category=product_category,
                sticker_fee=sticker,
                advertising_cost=advertising_cost,
                advertising_rate=advertising_rate,
                return_rate=return_rate,
                logistics_service_rate=logistics_service_rate,
                price_multiplier=price_multiplier,
                old_price_multiplier=old_price_multiplier,
            )
        except ValueError as error:
            if str(error).startswith(shipping_error_prefixes):
                return None
            raise

    def qualifies(result: OzonProfitResult | None) -> bool:
        return result is not None and result.cost_profit_margin >= target_margin

    def first_qualifying_cent(low: int, high: int) -> OzonProfitResult | None:
        high_result = result_at(high)
        if not qualifies(high_result):
            return None
        while low < high:
            middle = (low + high) // 2
            if qualifies(result_at(middle)):
                high = middle
            else:
                low = middle + 1
        return result_at(low)

    # Profit is monotonic inside each combined shipping/commission interval,
    # but can drop at 135, 450, and 635. Search each interval independently.
    candidates = [
        result for result in (
            first_qualifying_cent(1, 13500),
            first_qualifying_cent(13501, 45000),
            first_qualifying_cent(45001, 63500),
        )
        if result is not None
    ]

    high = 63501
    maximum = 1_000_000_000  # 10,000,000.00 in the template calculation currency.
    while high < maximum and not qualifies(result_at(high)):
        high = min(high * 2, maximum)
    if qualifies(result_at(high)):
        high_segment_result = first_qualifying_cent(63501, high)
        if high_segment_result is not None:
            candidates.append(high_segment_result)
    if not candidates:
        raise ValueError("在允许的售价范围内无法达到目标成本利润率，请检查成本、尺寸和类目")
    return min(candidates, key=lambda item: item.actual_sale)
