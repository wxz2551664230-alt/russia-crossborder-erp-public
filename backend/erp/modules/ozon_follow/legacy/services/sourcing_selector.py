from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Iterable


class MatchVerdict(StrEnum):
    EXACT = "EXACT"
    NOT_EXACT = "NOT_EXACT"
    UNCERTAIN = "UNCERTAIN"


@dataclass(slots=True)
class ExactMatchDecision:
    item_id: str
    sku_id: str
    verdict: MatchVerdict
    confidence: float
    reason: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return self.item_id, self.sku_id


@dataclass(slots=True)
class SourcingCandidate:
    item_id: str
    sku_id: str
    title: str
    sku_name: str
    image_url: str
    company: str
    unit_price: Decimal | None
    delivery_price: Decimal | None
    minimum_order_quantity: Decimal | None
    sales: int | None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return self.item_id, self.sku_id

    @property
    def offer_url(self) -> str:
        if not self.item_id:
            return ""
        return f"https://detail.1688.com/offer/{self.item_id}.html"

    @property
    def landed_cost(self) -> Decimal | None:
        if self.unit_price is None or self.delivery_price is None:
            return None
        return self.unit_price + self.delivery_price

    @classmethod
    def from_newton_row(cls, row: dict[str, Any]) -> "SourcingCandidate":
        newton_data = row.get("newtonData")
        if not isinstance(newton_data, dict):
            newton_data = {}
        return cls(
            item_id=str(row.get("itemId") or "").strip(),
            sku_id=str(row.get("skuId") or "").strip(),
            title=str(row.get("title") or "").strip(),
            sku_name=str(row.get("skuName") or "").strip(),
            image_url=str(row.get("imageUrl") or "").strip(),
            company=str(row.get("company") or "").strip(),
            unit_price=_decimal_or_none(row.get("realtimeSinglePrice")),
            delivery_price=_decimal_or_none(row.get("realtimeDeliveryPrice")),
            minimum_order_quantity=_decimal_or_none(newton_data.get("min_order_qty")),
            sales=_sales_or_none(row.get("salesCountNum", row.get("salesCount"))),
            raw=row,
        )


@dataclass(slots=True)
class SourceSelectionResult:
    status: str
    selected: SourcingCandidate | None
    eligible: list[SourcingCandidate]
    rejected: dict[tuple[str, str], str]
    message: str


def build_manual_review_candidates(
    rows: Iterable[dict[str, Any]],
    match_decisions: Iterable[ExactMatchDecision] = (),
) -> list[dict[str, Any]]:
    """Return one-piece candidates in Newton's original relevance order.

    Price and sales remain visible to the reviewer, but must not replace the
    image-search ranking that Newton produced.
    """
    candidates: list[dict[str, Any]] = []
    for row in rows:
        candidate = SourcingCandidate.from_newton_row(row)
        if not candidate.item_id or not candidate.sku_id:
            continue
        if candidate.minimum_order_quantity is None:
            continue
        if not Decimal("0") < candidate.minimum_order_quantity <= Decimal("1"):
            continue
        candidates.append({
            "itemId": candidate.item_id,
            "skuId": candidate.sku_id,
            "title": candidate.title,
            "skuName": candidate.sku_name,
            "imageUrl": candidate.image_url,
            "offerUrl": candidate.offer_url,
            "confidence": None,
            "verdict": "HUMAN_REVIEW",
            "reason": "程序不做AI同款判断，请人工对照Ozon原图确认商品和变体",
            "unitPrice": format(candidate.unit_price, "f") if candidate.unit_price is not None else "",
            "deliveryPrice": format(candidate.delivery_price, "f") if candidate.delivery_price is not None else "",
            "minimumOrderQuantity": format(candidate.minimum_order_quantity, "f"),
            "sales": candidate.sales,
            "company": candidate.company,
            "supplierCity": str((row.get("digital2Map") or {}).get("addressCity") or "") if isinstance(row.get("digital2Map"), dict) else "",
            "supplierYears": str((row.get("digital2Map") or {}).get("tpServiceYear") or "") if isinstance(row.get("digital2Map"), dict) else "",
            "supplierScore": str(row.get("customerStar") or ""),
            "recommendReasons": row.get("recommendReasonMap") if isinstance(row.get("recommendReasonMap"), dict) else {},
            "servicePerformance": row.get("servicePerformance") if isinstance(row.get("servicePerformance"), list) else [],
            "qualityGuarantee": str(row.get("qualityExpr") or ""),
            "ship24Hours": str((row.get("newtonData") or {}).get("is_24hour_send") or "") if isinstance(row.get("newtonData"), dict) else "",
            "ship48Hours": str((row.get("newtonData") or {}).get("s_48hour_send") or "") if isinstance(row.get("newtonData"), dict) else "",
            "needsDetail": bool((row.get("newtonData") or {}).get("elim_needs_detail")) if isinstance(row.get("newtonData"), dict) else False,
        })
    return candidates


def extract_newton_candidate_rows(tables: Iterable[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table in tables:
        value = _json_or_value(table)
        if isinstance(value, dict):
            result = value.get("result")
            if isinstance(result, list):
                rows.extend(row for row in result if isinstance(row, dict))
        elif isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, dict))
    return rows


def select_best_source(
    rows: Iterable[dict[str, Any]],
    match_decisions: Iterable[ExactMatchDecision],
    *,
    minimum_confidence: float = 0.90,
) -> SourceSelectionResult:
    """Selects conservatively and is allowed to return no automatic match.

    Rules are intentionally lexicographic: exact match, one-piece MOQ, non-zero
    sales when available, landed cost, then sales. Missing facts are never guessed.
    """
    decisions = {decision.key: decision for decision in match_decisions}
    rejected: dict[tuple[str, str], str] = {}
    eligible: list[SourcingCandidate] = []

    for row in rows:
        candidate = SourcingCandidate.from_newton_row(row)
        key = candidate.key
        if not candidate.item_id or not candidate.sku_id:
            rejected[key] = "缺少商品ID或SKU ID"
            continue
        decision = decisions.get(key)
        if decision is None:
            rejected[key] = "尚未完成严格同款校验"
            continue
        if decision.verdict is not MatchVerdict.EXACT:
            rejected[key] = decision.reason or "不是同款或无法确认"
            continue
        if decision.confidence < minimum_confidence:
            rejected[key] = f"同款置信度不足（{decision.confidence:.0%}）"
            continue
        if candidate.minimum_order_quantity is None:
            rejected[key] = "起订量未知"
            continue
        if not Decimal("0") < candidate.minimum_order_quantity <= Decimal("1"):
            rejected[key] = "不支持一件起批"
            continue
        if candidate.unit_price is None:
            rejected[key] = "SKU价格未知"
            continue
        if candidate.delivery_price is None:
            rejected[key] = "运费未知"
            continue
        eligible.append(candidate)

    if not eligible:
        return SourceSelectionResult(
            status="NO_EXACT_MATCH",
            selected=None,
            eligible=[],
            rejected=rejected,
            message="未找到可确认的同款一件起批货源",
        )

    positive_sales = [candidate for candidate in eligible if candidate.sales is not None and candidate.sales > 0]
    if positive_sales:
        ranked_pool = positive_sales
        for candidate in eligible:
            if candidate not in ranked_pool:
                rejected[candidate.key] = "存在有销量的同款候选，零销量或销量未知不参与比价"
    else:
        confirmed_zero_sales = [candidate for candidate in eligible if candidate.sales == 0]
        if not confirmed_zero_sales:
            for candidate in eligible:
                rejected[candidate.key] = "销量未知，不能按零销量推测"
            return SourceSelectionResult(
                status="INSUFFICIENT_DATA",
                selected=None,
                eligible=[],
                rejected=rejected,
                message="同款候选的销量均未知，无法安全自动选择",
            )
        ranked_pool = confirmed_zero_sales
        for candidate in eligible:
            if candidate not in ranked_pool:
                rejected[candidate.key] = "销量未知，不参与全零销量的最低价比较"
    ranked_pool.sort(
        key=lambda candidate: (
            candidate.landed_cost,
            -(candidate.sales or 0),
            candidate.item_id,
            candidate.sku_id,
        )
    )
    selected = ranked_pool[0]
    return SourceSelectionResult(
        status="SELECTED",
        selected=selected,
        eligible=ranked_pool,
        rejected=rejected,
        message="已按到手成本优先、销量次优的规则选出货源",
    )


def build_exact_match_verification_message(
    *,
    reference_title: str,
    reference_image_url: str,
    rows: Iterable[dict[str, Any]],
) -> str:
    candidates = []
    for row in rows:
        item_id = str(row.get("itemId") or "").strip()
        sku_id = str(row.get("skuId") or "").strip()
        image_url = str(row.get("imageUrl") or "").strip()
        if not item_id or not sku_id or not image_url:
            continue
        candidates.append(
            {
                "itemId": item_id,
                "skuId": sku_id,
                "title": str(row.get("title") or ""),
                "skuName": str(row.get("skuName") or ""),
                "imageUrl": image_url,
            }
        )
    payload = json.dumps(candidates, ensure_ascii=False, separators=(",", ":"))
    return (
        "请严格判断候选1688商品及其指定SKU是否与Ozon参考商品为同一款产品。\n"
        "这不是相似品推荐：核心结构、造型、功能部件、接口/按钮位置、附件组成和可见规格必须一致。"
        "颜色、拍摄角度和背景可以不同；型号、结构或套装内容不同必须判为NOT_EXACT。\n"
        "证据不足必须判为UNCERTAIN，禁止猜测。宁可没有结果，也不能把相似款判为同款。\n"
        f"Ozon标题：{reference_title.strip()}\n"
        f"Ozon参考主图：{reference_image_url.strip()}\n"
        f"候选数据：{payload}\n"
        "只返回JSON数组，不要返回其他文字。每项格式："
        '{"itemId":"...","skuId":"...","verdict":"EXACT|NOT_EXACT|UNCERTAIN",'
        '"confidence":0到1之间的数字,"reason":"简短的可核验证据"}'
    )


def parse_exact_match_decisions(value: Any) -> list[ExactMatchDecision]:
    parsed = _json_or_value(value)
    if isinstance(parsed, str):
        decoder = json.JSONDecoder()
        for index, char in enumerate(parsed):
            if char != "[":
                continue
            try:
                candidate, _ = decoder.raw_decode(parsed[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, list):
                parsed = candidate
                break
    if isinstance(parsed, dict):
        parsed = parsed.get("result", parsed.get("decisions"))
    if not isinstance(parsed, list):
        return []
    decisions: list[ExactMatchDecision] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("itemId") or "").strip()
        sku_id = str(item.get("skuId") or "").strip()
        try:
            verdict = MatchVerdict(str(item.get("verdict") or "").strip().upper())
            confidence = float(item.get("confidence"))
        except (ValueError, TypeError):
            continue
        if not item_id or not sku_id or not 0 <= confidence <= 1:
            continue
        decisions.append(
            ExactMatchDecision(
                item_id=item_id,
                sku_id=sku_id,
                verdict=verdict,
                confidence=confidence,
                reason=str(item.get("reason") or "").strip(),
            )
        )
    return decisions


def parse_exact_match_decision_chunks(chunks: Iterable[Any]) -> list[ExactMatchDecision]:
    """Parse streamed Newton output after joining token-sized text chunks."""
    text_parts: list[str] = []
    for chunk in chunks:
        content = getattr(chunk, "content", chunk)
        if isinstance(content, str):
            text_parts.append(content)
    return parse_exact_match_decisions("".join(text_parts))


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _sales_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group())
    if "万" in text:
        number *= 10000
    return int(number)


def _json_or_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value
