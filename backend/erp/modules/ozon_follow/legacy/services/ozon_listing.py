from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable

from erp.modules.ozon_follow.legacy.services.ozon_client import (
    OzonApiError,
    OzonImportUncertain,
    OzonSellerClient,
    import_errors,
    import_succeeded,
)
from erp.modules.ozon_follow.legacy.services.sourcing_selector import SourcingCandidate


class OzonListingValidationError(ValueError):
    pass


OZON_HASHTAGS_ATTRIBUTE_ID = 23171
OZON_COLOR_ATTRIBUTE_ID = 10096


@dataclass(slots=True)
class OzonCategoryMatch:
    description_category_id: int
    type_id: int
    display_zh: str
    display_ru: str
    matched_source_label: str


@dataclass(slots=True)
class OzonListingDraft:
    source_sku: str
    offer_id: str
    source_ozon_url: str
    title: str
    exported_category: str
    description_ru: str = ""
    tags_ru: list[str] = field(default_factory=list)
    description_category_id: int = 0
    type_id: int = 0
    category_display_zh: str = ""
    category_display_ru: str = ""
    selected_1688_item_id: str = ""
    selected_1688_sku_id: str = ""
    selected_1688_url: str = ""
    selected_1688_title: str = ""
    selected_1688_sku_name: str = ""
    product_manager_color: str = ""
    product_manager_size: str = ""
    product_manager_remark: str = ""
    selected_1688_landed_cost: str = ""
    purchase_price_cny: str = ""
    domestic_delivery_cny: str = ""
    cross_border_freight_cny: str = ""
    pricing_result: dict[str, Any] = field(default_factory=dict)
    weight_g: float | None = None
    depth_mm: float | None = 300
    width_mm: float | None = 200
    height_mm: float | None = 100
    price: str = ""
    old_price: str = "0"
    currency_code: str = ""
    vat: str = "0"
    primary_image: str = ""
    images: list[str] = field(default_factory=list)
    images_oss_ready: bool = False
    attributes: list[dict[str, Any]] = field(default_factory=list)
    complex_attributes: list[dict[str, Any]] = field(default_factory=list)
    warehouse_id: int | None = None
    stock: int = 0
    status: str = "DRAFT"
    task_id: int | None = None
    blockers: list[str] = field(default_factory=list)
    ai_preparation: dict[str, Any] = field(default_factory=dict)
    product_manager_sync: dict[str, Any] = field(default_factory=dict)
    ozon_repair: dict[str, Any] = field(default_factory=dict)
    promotion_action_id: int | None = None
    promotion_action_title: str = ""
    promotion_result: dict[str, Any] = field(default_factory=dict)
    workflow_stage: str = ""
    workflow_state: str = ""
    workflow_message: str = ""
    workflow_error: str = ""
    workflow_updated_at: str = ""
    # The base id is shared by all store publications and by the ECS source
    # record.  Store-facing offer ids are derived from it with a store prefix.
    base_offer_id: str = ""
    store_publications: dict[str, Any] = field(default_factory=dict)
    store_inventory: dict[str, Any] = field(default_factory=dict)
    store_promotions: dict[str, Any] = field(default_factory=dict)
    store_action_target: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "OzonListingDraft":
        allowed = cls.__dataclass_fields__.keys()
        normalized = {key: value[key] for key in allowed if key in value}
        for key, default in (("depth_mm", 300), ("width_mm", 200), ("height_mm", 100)):
            if normalized.get(key) in (None, ""):
                normalized[key] = default
        draft = cls(**normalized)
        if not draft.base_offer_id:
            draft.base_offer_id = draft.offer_id
        draft.images = list(dict.fromkeys(
            str(url).strip() for url in draft.images
            if str(url).strip().startswith("https://") and str(url).strip() != draft.primary_image
        ))[:14]
        return draft


@dataclass(slots=True)
class OzonListingSubmission:
    task_id: int
    status: str
    import_result: dict[str, Any]
    stock_pending: bool


def prepare_listing_draft(
    product: dict[str, Any],
    *,
    category_match: OzonCategoryMatch | None,
    selected_source: SourcingCandidate | None = None,
    offer_id: str,
) -> OzonListingDraft:
    sku = str(product.get("sku") or "").strip()
    if not sku:
        raise OzonListingValidationError("Ozon导出商品缺少SKU")
    normalized_offer_id = str(offer_id or "").strip()
    if not normalized_offer_id:
        raise OzonListingValidationError("创建上架草稿时没有分配Offer ID")
    image_urls = _product_image_urls(product)
    image_url = image_urls[0] if image_urls else _high_resolution_ozon_image(str(product.get("image_url") or ""))
    selected_cost = ""
    if selected_source and selected_source.landed_cost is not None:
        selected_cost = format(selected_source.landed_cost, "f")
    draft = OzonListingDraft(
        source_sku=sku,
        offer_id=normalized_offer_id,
        base_offer_id=normalized_offer_id,
        source_ozon_url=str(product.get("ozon_url") or "").strip(),
        title=str(product.get("title") or "").strip(),
        exported_category=str(product.get("category") or "").strip(),
        description_category_id=(category_match.description_category_id if category_match else 0),
        type_id=(category_match.type_id if category_match else 0),
        category_display_zh=(category_match.display_zh if category_match else ""),
        category_display_ru=(category_match.display_ru if category_match else ""),
        selected_1688_item_id=(selected_source.item_id if selected_source else ""),
        selected_1688_sku_id=(selected_source.sku_id if selected_source else ""),
        selected_1688_url=(selected_source.offer_url if selected_source else ""),
        selected_1688_landed_cost=selected_cost,
        weight_g=_positive_float_or_none(product.get("weight_grams")),
        primary_image=image_url,
        images=image_urls[1:],
    )
    draft.blockers = validate_listing_draft(draft)
    draft.status = "READY" if not draft.blockers else "DRAFT_BLOCKED"
    return draft


def validate_listing_draft(
    draft: OzonListingDraft,
    attribute_definitions: Iterable[dict[str, Any]] | None = None,
) -> list[str]:
    blockers: list[str] = []
    if not draft.offer_id.strip():
        blockers.append("Offer ID为空")
    if not draft.title.strip():
        blockers.append("商品标题为空")
    elif len(draft.title) > 500:
        blockers.append("商品标题超过500字符")
    if len(draft.description_ru) > 6000:
        blockers.append("商品描述超过6000字符")
    if len(draft.tags_ru) > 30:
        blockers.append("主题标签超过30个")
    for tag in draft.tags_ru:
        if len(str(tag)) > 30 or not re.fullmatch(r"#[0-9A-Za-zА-Яа-яЁё_]+", str(tag)):
            blockers.append("主题标签格式无效（必须#开头，每项最多30字符）")
            break
    if not draft.exported_category.strip():
        blockers.append("Ozon导出类目为空")
    if draft.description_category_id <= 0 or draft.type_id <= 0:
        blockers.append("尚未在导入前确认Ozon实时类目")
    if draft.weight_g is None or draft.weight_g <= 0:
        blockers.append("Ozon导出重量缺失或无效")
    dimensions = (draft.depth_mm, draft.width_mm, draft.height_mm)
    if any(value is None or value <= 0 for value in dimensions):
        blockers.append("缺少包装长、宽、高（毫米）")
    price = _decimal_or_none(draft.price)
    if price is None or price <= 0:
        blockers.append("尚未计算Ozon上架售价")
    old_price = _decimal_or_none(draft.old_price)
    if old_price is None or old_price < 0:
        blockers.append("折扣前价格无效")
    elif price is not None and old_price and old_price <= price:
        blockers.append("折扣前价格必须大于当前售价")
    if not draft.currency_code.strip():
        blockers.append("尚未设置该Ozon店铺的结算币种")
    if not draft.primary_image.startswith("https://"):
        blockers.append("缺少可公开访问的HTTPS主图")
    if not draft.images_oss_ready:
        blockers.append("主图和附图尚未全部上传到OSS")
    blockers.extend(
        _missing_required_attributes(
            draft.attributes,
            draft.complex_attributes,
            list(attribute_definitions or []),
        )
    )
    return list(dict.fromkeys(blockers))


def build_ozon_import_item(
    draft: OzonListingDraft,
    attribute_definitions: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    blockers = validate_listing_draft(draft, attribute_definitions)
    if blockers:
        raise OzonListingValidationError("上架草稿不完整：" + "；".join(blockers))
    all_images = [draft.primary_image, *draft.images]
    all_images = list(dict.fromkeys(url for url in all_images if url.startswith("https://")))[:15]
    return {
        "attributes": _clean_attribute_payload(draft.attributes),
        "barcode": "",
        "description_category_id": int(draft.description_category_id),
        "new_description_category_id": 0,
        "color_image": all_images[0],
        "complex_attributes": _clean_attribute_payload(draft.complex_attributes),
        "currency_code": draft.currency_code.strip().upper(),
        "depth": float(draft.depth_mm),
        "dimension_unit": "mm",
        "height": float(draft.height_mm),
        "images": all_images[1:],
        "name": re.sub(r"\s+", " ", draft.title).strip()[:500],
        "offer_id": draft.offer_id.strip(),
        "old_price": str(draft.old_price or "0").strip(),
        "pdf_list": [],
        "price": str(draft.price).strip(),
        "primary_image": all_images[0],
        "type_id": int(draft.type_id),
        "vat": str(draft.vat),
        "weight": float(draft.weight_g),
        "weight_unit": "g",
        "width": float(draft.width_mm),
    }


class OzonListingWorkflow:
    """Draft-first Ozon workflow; stock remains a separate follow-up step."""

    def __init__(self, client: OzonSellerClient) -> None:
        self.client = client

    def resolve_exported_category(self, exported_category: str) -> OzonCategoryMatch | None:
        zh_tree = self.client.category_tree("ZH_HANS")
        ru_tree = self.client.category_tree("RU")
        return match_exported_category(exported_category, zh_tree, ru_tree)

    def submit(
        self,
        draft: OzonListingDraft,
        *,
        on_task_created: Callable[[OzonListingDraft], None] | None = None,
    ) -> OzonListingSubmission:
        if draft.task_id:
            raise OzonListingValidationError(
                f"草稿已有Ozon导入任务 {draft.task_id}，请查询原任务，不要重复提交"
            )
        if draft.status == "IMPORT_UNCERTAIN":
            raise OzonListingValidationError(
                "上次创建商品的结果不确定，请先按Offer ID在Ozon后台核对，不能重复提交"
            )
        definitions = self.client.category_attributes(
            draft.description_category_id,
            draft.type_id,
            "ZH_HANS",
        )
        item = build_ozon_import_item(draft, definitions)
        try:
            task_id = self.client.import_product(item)
        except OzonImportUncertain as exc:
            draft.status = "IMPORT_UNCERTAIN"
            draft.blockers = [str(exc)]
            if on_task_created:
                on_task_created(draft)
            raise
        draft.task_id = task_id
        draft.status = "IMPORT_SUBMITTED"
        if on_task_created:
            on_task_created(draft)
        result = self.client.wait_import(task_id)
        errors = import_errors(result)
        if errors:
            draft.status = "IMPORT_FAILED"
            draft.blockers = [json.dumps(errors, ensure_ascii=False)]
            raise OzonApiError("Ozon商品导入失败：" + draft.blockers[0])
        if not import_succeeded(result):
            draft.status = "IMPORT_PENDING"
            raise OzonApiError(
                f"Ozon导入任务 {task_id} 尚未明确完成；程序不会重复提交。"
            )
        draft.status = "IMPORT_COMPLETED_STOCK_PENDING"
        draft.blockers = []
        return OzonListingSubmission(
            task_id=task_id,
            status=draft.status,
            import_result=result,
            stock_pending=True,
        )

    def refresh_import(self, draft: OzonListingDraft) -> dict[str, Any]:
        if not draft.task_id:
            raise OzonListingValidationError("草稿没有Ozon导入任务 ID")
        result = self.client.import_info(int(draft.task_id))
        errors = import_errors(result)
        if errors:
            draft.status = "IMPORT_FAILED"
            draft.blockers = [json.dumps(errors, ensure_ascii=False)]
        elif import_succeeded(result):
            draft.status = "IMPORT_COMPLETED_STOCK_PENDING"
            draft.blockers = []
        else:
            draft.status = "IMPORT_PENDING"
        return result

    def publish_stock(self, draft: OzonListingDraft) -> dict[str, Any]:
        if draft.status not in {"IMPORT_COMPLETED_STOCK_PENDING", "PUBLISHED", "STOCK_COMPLETED_PROMOTION_PENDING"}:
            raise OzonListingValidationError("商品尚未成功导入，不能发布库存")
        if not draft.warehouse_id or draft.warehouse_id <= 0:
            raise OzonListingValidationError("请选择Ozon仓库")
        if draft.stock <= 0:
            raise OzonListingValidationError("库存必须大于0")
        self.client.wait_product_available(draft.offer_id)
        result = self.client.update_stock(
            draft.offer_id,
            int(draft.warehouse_id),
            int(draft.stock),
        )
        draft.status = "STOCK_COMPLETED_PROMOTION_PENDING"
        return result


def flatten_ozon_categories(
    tree: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def visit(node: dict[str, Any], path: list[str], inherited_id: int | None = None) -> None:
        if node.get("disabled") or node.get("is_disabled"):
            return
        raw_category_id = node.get("description_category_id") or node.get("category_id") or inherited_id
        category_id = int(raw_category_id) if raw_category_id else None
        category_name = str(node.get("category_name") or "").strip()
        type_id = node.get("type_id")
        type_name = str(node.get("type_name") or "").strip()
        current_path = [*path, *([category_name] if category_name else [])]
        if category_id and type_id:
            display_path = [*current_path, *([type_name] if type_name and type_name != category_name else [])]
            result.append(
                {
                    "description_category_id": category_id,
                    "type_id": int(type_id),
                    "name": type_name or category_name,
                    "path": display_path,
                    "display": " / ".join(display_path),
                }
            )
        for legacy in node.get("type", node.get("types", [])) or []:
            if not isinstance(legacy, dict) or legacy.get("disabled") or legacy.get("is_disabled"):
                continue
            legacy_id = legacy.get("id") or legacy.get("type_id")
            legacy_name = str(legacy.get("name") or legacy.get("type_name") or "").strip()
            if category_id and legacy_id:
                display_path = [*current_path, *([legacy_name] if legacy_name else [])]
                result.append(
                    {
                        "description_category_id": category_id,
                        "type_id": int(legacy_id),
                        "name": legacy_name,
                        "path": display_path,
                        "display": " / ".join(display_path),
                    }
                )
        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                visit(child, current_path, category_id)

    for root in tree:
        if isinstance(root, dict):
            visit(root, [])
    unique = {
        (item["description_category_id"], item["type_id"]): item
        for item in result
    }
    return list(unique.values())


def match_exported_category(
    exported_category: str,
    zh_tree: Iterable[dict[str, Any]],
    ru_tree: Iterable[dict[str, Any]],
) -> OzonCategoryMatch | None:
    source_labels = [
        label.strip()
        for label in re.split(r"[\r\n]+", str(exported_category or ""))
        if label.strip()
    ]
    normalized_sources = {_normalize_category(label): label for label in source_labels}
    if not normalized_sources:
        return None
    zh = {
        (item["description_category_id"], item["type_id"]): item
        for item in flatten_ozon_categories(zh_tree)
    }
    ru = {
        (item["description_category_id"], item["type_id"]): item
        for item in flatten_ozon_categories(ru_tree)
    }
    matches: list[tuple[tuple[int, int], str]] = []
    for key in zh.keys() | ru.keys():
        candidate_labels: list[str] = []
        for localized in (zh.get(key), ru.get(key)):
            if not localized:
                continue
            candidate_labels.extend([str(localized.get("name") or ""), *localized.get("path", [])])
        normalized_candidates = {_normalize_category(label) for label in candidate_labels if label}
        exact = normalized_candidates & normalized_sources.keys()
        if exact:
            matched_normalized = max(exact, key=len)
            matches.append((key, normalized_sources[matched_normalized]))
    unique_keys = {key for key, _ in matches}
    if len(unique_keys) != 1:
        return None
    key = next(iter(unique_keys))
    matched_label = next(label for candidate_key, label in matches if candidate_key == key)
    return OzonCategoryMatch(
        description_category_id=key[0],
        type_id=key[1],
        display_zh=str((zh.get(key) or {}).get("display") or ""),
        display_ru=str((ru.get(key) or {}).get("display") or ""),
        matched_source_label=matched_label,
    )


def required_attribute_skeleton(
    definitions: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    attributes: list[dict[str, Any]] = []
    complex_groups: dict[int, dict[str, Any]] = {}
    for definition in definitions:
        if not definition.get("is_required") or not definition.get("id"):
            continue
        complex_id = int(definition.get("attribute_complex_id") or 0)
        item = {
            "complex_id": complex_id,
            "id": int(definition["id"]),
            "values": [],
            "_name": str(definition.get("name") or definition["id"]),
        }
        if complex_id:
            group = complex_groups.setdefault(
                complex_id,
                {"attributes": [], "_complex_id": complex_id},
            )
            group["attributes"].append(item)
        else:
            attributes.append(item)
    return attributes, list(complex_groups.values())


def _missing_required_attributes(
    attributes: list[dict[str, Any]],
    complex_attributes: list[dict[str, Any]],
    definitions: list[dict[str, Any]],
) -> list[str]:
    if not definitions:
        return []
    all_attributes = list(attributes)
    for group in complex_attributes:
        all_attributes.extend(group.get("attributes", []))
    present = {
        int(item.get("id") or 0)
        for item in all_attributes
        if item.get("values")
        and any(value.get("value") or value.get("dictionary_value_id") for value in item["values"])
    }
    return [
        "缺少Ozon必填属性：" + str(item.get("name") or item.get("id"))
        for item in definitions
        if item.get("is_required") and int(item.get("id") or 0) not in present
    ]


def _clean_attribute_payload(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for item in items:
        value = {key: data for key, data in item.items() if not str(key).startswith("_")}
        if "values" in value:
            value["values"] = [dict(entry) for entry in value.get("values", [])]
        if int(value.get("id") or 0) == OZON_HASHTAGS_ATTRIBUTE_ID:
            for entry in value.get("values", []):
                tags = re.split(r"[,，\s]+", str(entry.get("value") or ""))
                entry["value"] = " ".join(tag for tag in tags if tag)
        if _is_color_attribute(item):
            color_values = value.get("values", [])
            multicolor = next((
                entry for entry in color_values
                if _is_multicolor_value(entry.get("value"))
            ), None)
            value["values"] = [multicolor] if multicolor else color_values[:3]
        if "attributes" in value:
            value["attributes"] = _clean_attribute_payload(value.get("attributes", []))
        cleaned.append(value)
    return cleaned


def _is_color_attribute(item: dict[str, Any]) -> bool:
    if int(item.get("id") or 0) == OZON_COLOR_ATTRIBUTE_ID:
        return True
    name = str(item.get("_name") or item.get("name") or "").casefold()
    return any(token in name for token in ("颜色", "цвет товара", "product color"))


def _is_multicolor_value(value: Any) -> bool:
    normalized = re.sub(r"\s+", " ", str(value or "")).strip().casefold().replace("ё", "е")
    return normalized in {"多色", "彩色", "разноцветный", "многоцветный", "multicolor", "multi-color"}


def _normalize_category(value: str) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9\u3400-\u9fff]+", "", text)


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        text = str(value).strip()
        return Decimal(text) if text else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _positive_float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _high_resolution_ozon_image(url: str) -> str:
    return re.sub(r"/wc\d+/", "/wc1000/", str(url or "").strip())


def _product_image_urls(product: dict[str, Any]) -> list[str]:
    """Read all imported Ozon images and normalize to wc1000; never include AI output."""
    values: list[str] = []
    raw = product.get("image_urls")
    if isinstance(raw, list):
        values.extend(str(item) for item in raw)
    if not values:
        try:
            raw_data = json.loads(str(product.get("raw_json") or "{}"))
            if isinstance(raw_data, dict):
                for key, value in raw_data.items():
                    label = str(key).casefold()
                    if any(token in label for token in ("主图", "附图", "图片", "image", "photo")):
                        if isinstance(value, list):
                            values.extend(str(item) for item in value)
                        else:
                            values.extend(re.split(r"[\r\n,;|\s]+", str(value or "")))
        except (TypeError, json.JSONDecodeError):
            pass
    if not values:
        values = [str(product.get("image_url") or "")]
    result: list[str] = []
    for value in values:
        url = _high_resolution_ozon_image(value)
        if url.startswith("https://") and url not in result:
            result.append(url)
    return result
