from __future__ import annotations

from erp.core.ai_concurrency import post as ai_post

import base64
from difflib import SequenceMatcher
import hashlib
import io
import json
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from PIL import Image, ImageChops, ImageStat

from erp.modules.ozon_follow.legacy.config import AppConfig
from erp.modules.ozon_follow.legacy.paths import DATA_DIR, ensure_runtime_directories
from erp.modules.ozon_follow.legacy.services.ozon_client import OzonSellerClient
from erp.modules.ozon_follow.legacy.services.ozon_listing import OzonListingDraft
from erp.modules.ozon_follow.legacy.services.ozon_gallery import (
    OzonGallery,
    additional_gallery_images,
    scrape_ozon_gallery,
    wait_for_extension_gallery,
)


MAX_IMAGE_PROMPT_CHARS = 4000
STORE_CREATIVE_PROFILES = (
    {
        "profile_id": "functional_editorial",
        "copy_angle": "functional and problem-solving; lead with the product type and its primary everyday use",
        "title_pattern": "product type first, then one factual differentiator; concise noun phrase",
        "description_structure": "use case -> visible construction/features -> care or placement -> factual closing",
        "composition": "clean editorial composition with the product in a front three-quarter view",
        "camera_viewpoint": "eye-level front three-quarter view",
        "background_environment": "bright structured studio-workspace environment with geometric planes",
        "palette": "cool white and soft gray with restrained green and red accents",
        "lighting": "crisp daylight with defined contact shadows",
        "typography_placement": "headline at top-left; compact fact modules along the lower edge",
    },
    {
        "profile_id": "warm_lifestyle",
        "copy_angle": "lifestyle and occasion-led; open with the user situation, then identify the product",
        "title_pattern": "use scenario first, then product type; avoid the primary store's noun order",
        "description_structure": "scene/occasion -> user benefit supported by facts -> visible details -> concise closing",
        "composition": "warm contextual composition with an asymmetrical side or slightly elevated product view",
        "camera_viewpoint": "side view or 25-degree elevated view",
        "background_environment": "realistic category-appropriate home setting with layered surfaces and contextual props",
        "palette": "cream, terracotta and deep teal with a small charcoal accent",
        "lighting": "warm window light with soft directional shadows",
        "typography_placement": "vertical headline zone on the right; fact labels grouped at bottom-left",
    },
    {
        "profile_id": "bold_graphic",
        "copy_angle": "feature-led and energetic; foreground a different verified characteristic than earlier stores",
        "title_pattern": "short benefit-oriented clause followed by the product type",
        "description_structure": "verified differentiator -> product overview -> usage scenarios -> visible details",
        "composition": "bold graphic diagonal composition with a low three-quarter product view",
        "camera_viewpoint": "low front-side three-quarter view",
        "background_environment": "designed color-block set with physical platforms and strong depth separation",
        "palette": "cobalt, coral, off-white and black accents",
        "lighting": "high-contrast studio lighting with a controlled rim light",
        "typography_placement": "large headline across the upper center; small fact strip on the left edge",
    },
    {
        "profile_id": "premium_detail",
        "copy_angle": "calm detail-led presentation; emphasize verified form, finish, or organization",
        "title_pattern": "distinctive verified detail first, product category last",
        "description_structure": "visual detail -> construction -> practical use -> suitable setting",
        "composition": "premium close editorial scene with the full product viewed from a high three-quarter angle",
        "camera_viewpoint": "high three-quarter view while keeping the complete SKU visible",
        "background_environment": "dark neutral architectural set with one tactile category-appropriate surface",
        "palette": "graphite, ivory, muted yellow and cyan accents",
        "lighting": "focused softbox lighting with subtle edge highlights",
        "typography_placement": "compact headline centered near the top; facts stacked vertically at lower-right",
    },
    {
        "profile_id": "travel_documentary",
        "copy_angle": "mobility and everyday readiness, using only visually verified facts",
        "title_pattern": "short situation-led headline followed by the product type",
        "description_structure": "travel moment -> visible utility -> construction -> factual closing",
        "composition": "documentary overhead composition with the product offset from center among sparse travel context",
        "camera_viewpoint": "top-down or steep 60-degree view",
        "background_environment": "clean hotel, luggage or entryway preparation scene with believable personal objects",
        "palette": "navy, sand, white and a restrained orange accent",
        "lighting": "soft morning side light with realistic ambient shadows",
        "typography_placement": "narrow headline band at bottom-left; two facts aligned vertically at upper-right",
    },
    {
        "profile_id": "fresh_domestic",
        "copy_angle": "simple daily organization and comfort, without unsupported benefit claims",
        "title_pattern": "concise product noun plus one visible differentiator",
        "description_structure": "daily setting -> visible form -> organization detail -> suitable placement",
        "composition": "wide breathing composition with product on the left third and a strong environmental foreground",
        "camera_viewpoint": "natural seated eye-level side angle",
        "background_environment": "bright real bathroom, wardrobe or bedroom corner, clearly lived-in but uncluttered",
        "palette": "white, mint, pale blue and warm wood",
        "lighting": "diffused clean daylight without beige cast",
        "typography_placement": "headline in the open upper-right area; factual labels in one slim bottom row",
    },
    {
        "profile_id": "urban_contrast",
        "copy_angle": "direct modern presentation led by a different verified physical detail",
        "title_pattern": "two-part compact headline with a strong line break",
        "description_structure": "visible detail -> practical context -> construction -> concise close",
        "composition": "cropped editorial foreground with the complete product placed low and strongly off-axis",
        "camera_viewpoint": "close wide-angle side view with controlled perspective",
        "background_environment": "modern urban apartment shelf or clean hallway with architectural lines",
        "palette": "charcoal, electric blue, clean white and lime accent",
        "lighting": "directional daylight with graphic but physically plausible shadows",
        "typography_placement": "large two-line headline on the left edge; facts as small unboxed captions on the right",
    },
    {
        "profile_id": "soft_story",
        "copy_angle": "quiet human-scale routine and tactile visual details",
        "title_pattern": "gentle scenario phrase followed by an exact product noun",
        "description_structure": "routine -> visible texture/form -> use placement -> factual close",
        "composition": "layered intimate scene viewed through a soft foreground with product on the right third",
        "camera_viewpoint": "slightly low candid three-quarter angle",
        "background_environment": "clean bedside, dressing or family-home setting with restrained textiles",
        "palette": "dusty rose, burgundy, cream and cool gray",
        "lighting": "soft late-afternoon window light with clean neutral whites",
        "typography_placement": "small headline block at upper-left; facts integrated as clean captions near the base",
    },
)


def store_creative_profile(index: int) -> dict[str, str]:
    """Return a stable profile for a store's position in configuration."""
    return dict(STORE_CREATIVE_PROFILES[index % len(STORE_CREATIVE_PROFILES)])


NO_BRAND_DICTIONARY_ID = 126745801
NO_BRAND_DICTIONARY_VALUE = "Нет бренда"
CHINA_DICTIONARY_ID = 90296
CHINA_DICTIONARY_VALUE = "Китай"
ONE_YEAR_WARRANTY_DICTIONARY_ID = 970716397
ONE_YEAR_WARRANTY_DICTIONARY_VALUE = "1 год"
MAX_HASHTAGS = 30
MAX_HASHTAG_LENGTH = 30
RICH_CONTENT_ATTRIBUTE_ID = 11254
BROAD_INFERENCE_ATTRIBUTE_IDS = {
    8449,   # 为谁/适用对象
    9390,   # 目标受众
    6953,   # 茶、咖啡等宽泛用途
    8448,   # 节日/送礼场景
    4384,   # 配套/套装描述
    11619,  # 印刷名称
}


def _is_brand_attribute(definition: dict[str, Any]) -> bool:
    name = _normalize_text(" ".join((str(definition.get("name") or ""), str(definition.get("name_ru") or ""))))
    return int(definition.get("id") or 0) == 85 or "品牌" in name or "бренд" in name


def _is_model_name_attribute(definition: dict[str, Any]) -> bool:
    name = _normalize_text(" ".join((str(definition.get("name") or ""), str(definition.get("name_ru") or ""))))
    return "型号名称" in name or "названиемодели" in name


def _is_origin_country_attribute(definition: dict[str, Any]) -> bool:
    name = _normalize_text(" ".join((str(definition.get("name") or ""), str(definition.get("name_ru") or ""))))
    return int(definition.get("id") or 0) == 4389 or "原产国" in name or "странапроисхождения" in name


def normalize_hashtags(values: Any) -> list[str]:
    raw_values = values if isinstance(values, list) else re.split(r"[,，\r\n\s]+", str(values or ""))
    result: list[str] = []
    for raw in raw_values:
        value = re.sub(r"\s+", "_", str(raw or "").strip().lstrip("#"))
        value = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_]", "", value).strip("_")
        tag = f"#{value}" if value else ""
        if tag and len(tag) <= MAX_HASHTAG_LENGTH and tag.casefold() not in {item.casefold() for item in result}:
            result.append(tag)
        if len(result) >= MAX_HASHTAGS:
            break
    return result


def format_hashtags(values: Any) -> str:
    return " ".join(normalize_hashtags(values))


def _clean_marketing_copy(value: str, brand: str = "") -> str:
    text = str(value or "")
    blocked = [brand, "нет бренда", "без бренда", "no brand"]
    blocked.extend([
        "скидка", "распродажа", "акция", "подарок", "бесплатно", "хит продаж",
        "лучшая цена", "супер цена", "успей купить", "ограниченное предложение",
    ])
    for phrase in blocked:
        phrase = str(phrase or "").strip()
        if phrase:
            text = re.sub(re.escape(phrase), "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"(?:,\s*){2,}", ", ", text)
    return text.strip(" ,;:-\n\t")


def enforce_system_attributes(draft: OzonListingDraft, definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    for definition in definitions:
        if _is_brand_attribute(definition):
            _set_attribute(draft, definition, NO_BRAND_DICTIONARY_VALUE, NO_BRAND_DICTIONARY_ID, {
                "confidence": 1.0, "evidence": "系统锁定：无品牌",
            })
            applied.append({"attribute_id": int(definition["id"]), "value": NO_BRAND_DICTIONARY_VALUE})
        elif _is_model_name_attribute(definition) and not definition.get("dictionary_id"):
            _set_attribute(draft, definition, draft.offer_id, 0, {
                "confidence": 1.0, "evidence": "系统默认：与Offer ID一致",
            })
            applied.append({"attribute_id": int(definition["id"]), "value": draft.offer_id})
        elif _is_origin_country_attribute(definition):
            _set_attribute(draft, definition, CHINA_DICTIONARY_VALUE, CHINA_DICTIONARY_ID, {
                "confidence": 1.0, "evidence": "系统默认：原产国中国",
            })
            applied.append({"attribute_id": int(definition["id"]), "value": CHINA_DICTIONARY_VALUE})
        elif int(definition.get("id") or 0) == 9070:
            _set_attribute(draft, definition, "false", 0, {
                "confidence": 1.0, "evidence": "系统默认：非18+商品",
            })
            applied.append({"attribute_id": 9070, "value": "false"})
        elif int(definition.get("id") or 0) == 10400:
            _set_attribute(
                draft, definition,
                ONE_YEAR_WARRANTY_DICTIONARY_VALUE,
                ONE_YEAR_WARRANTY_DICTIONARY_ID,
                {"confidence": 1.0, "evidence": "系统默认：保证1年"},
            )
            applied.append({"attribute_id": 10400, "value": ONE_YEAR_WARRANTY_DICTIONARY_VALUE})
    return applied


def enforce_known_fact_attributes(
    draft: OzonListingDraft,
    definitions: list[dict[str, Any]],
    product: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fill category fields that follow directly from imported or system facts."""
    definitions_by_id = {int(item.get("id") or 0): item for item in definitions}
    existing_ids = {
        int(item.get("id") or 0)
        for item in [*draft.attributes, *(attribute for group in draft.complex_attributes for attribute in group.get("attributes", []))]
        if item.get("values")
    }
    applied: list[dict[str, Any]] = []

    def set_value(attribute_id: int, value: str, dictionary_value_id: int = 0, *, append: bool = False) -> None:
        definition = definitions_by_id.get(attribute_id)
        if not definition or (attribute_id in existing_ids and not append):
            return
        evidence = "系统依据Ozon导出、已确认货源或明确类目事实补全"
        _set_attribute(
            draft, definition, value, dictionary_value_id,
            {"confidence": 1.0, "evidence": evidence}, append=append,
        )
        existing_ids.add(attribute_id)
        applied.append({
            "attribute_id": attribute_id,
            "name": definition.get("name"),
            "value": value,
            "dictionary_value_id": dictionary_value_id,
            "confidence": 1.0,
            "evidence": evidence,
        })

    set_value(9024, draft.offer_id)
    set_value(11650, "1")
    # The selected Ozon type is already fixed by the original publication task.
    # Use only an exact match from the live category definition, never a guessed value.
    type_definition = definitions_by_id.get(8229)
    if type_definition:
        selected_type = next(
            (
                option for option in type_definition.get("allowed_values") or []
                if int(option.get("id") or 0) == int(draft.type_id or 0)
            ),
            None,
        )
        if selected_type and selected_type.get("value"):
            set_value(8229, str(selected_type["value"]), int(selected_type["id"]))
    try:
        weight = float(product.get("weight_grams") or 0)
    except (TypeError, ValueError):
        weight = 0
    if weight:
        normalized_weight = format(weight, "g")
        set_value(4383, normalized_weight)
        set_value(4497, normalized_weight)

    return applied


def normalize_existing_draft(draft: OzonListingDraft, product: dict[str, Any]) -> bool:
    """Migrate an old saved draft to current content and system-attribute rules."""
    changed = False
    brand = str(product.get("brand") or "").strip()
    title = _clean_marketing_copy(draft.title, brand)[:500]
    description = _clean_marketing_copy(draft.description_ru, brand)[:6000]
    normalized_brand = re.sub(r"[^0-9a-zа-я]+", "", brand.casefold().replace("ё", "е"))
    tags = [
        tag for tag in normalize_hashtags(draft.tags_ru)
        if not normalized_brand or normalized_brand not in re.sub(
            r"[^0-9a-zа-я]+", "", tag.casefold().replace("ё", "е")
        )
    ]
    if (title, description, tags) != (draft.title, draft.description_ru, draft.tags_ru):
        draft.title, draft.description_ru, draft.tags_ru = title, description, tags
        changed = True

    original_main = str(product.get("image_url") or "").strip()
    original_main = re.sub(r"/wc\d+/", "/wc1000/", original_main)
    cleaned_images = [
        url for url in draft.images
        if re.sub(r"/wc\d+/", "/wc1000/", str(url).strip()) != original_main
    ]
    if cleaned_images != draft.images:
        draft.images = cleaned_images
        changed = True

    filtered_attributes = [
        item for item in draft.attributes
        if int(item.get("id") or 0) != RICH_CONTENT_ATTRIBUTE_ID
    ]
    if filtered_attributes != draft.attributes:
        draft.attributes = filtered_attributes
        changed = True
    for group in draft.complex_attributes:
        filtered = [
            item for item in group.get("attributes", [])
            if int(item.get("id") or 0) != RICH_CONTENT_ATTRIBUTE_ID
        ]
        if filtered != group.get("attributes", []):
            group["attributes"] = filtered
            changed = True

    all_items = list(draft.attributes)
    for group in draft.complex_attributes:
        all_items.extend(group.get("attributes", []))
    for item in all_items:
        definition = {"id": item.get("id"), "name": item.get("_name")}
        if _is_brand_attribute(definition):
            expected = [{"value": NO_BRAND_DICTIONARY_VALUE, "dictionary_value_id": NO_BRAND_DICTIONARY_ID}]
            if item.get("values") != expected:
                item["values"] = expected
                item["_source"] = "系统锁定：无品牌"
                item["_confidence"] = 1.0
                item["_evidence"] = "系统锁定：无品牌"
                changed = True
        elif _is_model_name_attribute(definition):
            expected = [{"value": draft.offer_id}]
            if item.get("values") != expected:
                item["values"] = expected
                item["_source"] = "系统默认：与Offer ID一致"
                item["_confidence"] = 1.0
                item["_evidence"] = "系统默认：与Offer ID一致"
                changed = True
    return changed


def normalize_chat_url(value: str) -> str:
    return _normalize_endpoint(value, "/v1/chat/completions")


def normalize_image_url(value: str) -> str:
    return _normalize_endpoint(value, "/v1/images/edits")


def _normalize_endpoint(value: str, default_path: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw
    if not parsed.scheme or not parsed.netloc:
        return raw
    path = parsed.path.rstrip("/")
    if path in {"", "/v1"}:
        path = default_path
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _extract_json(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and any(
        key in payload for key in ("title_ru", "attributes", "description_ru")
    ):
        return payload
    if not isinstance(payload, dict):
        raise RuntimeError("AI接口返回格式不是JSON对象")
    if payload.get("error"):
        error = payload["error"]
        if isinstance(error, dict):
            error = error.get("message") or error.get("type") or error
        raise RuntimeError("AI接口错误：" + str(error))
    content: Any = ""
    choices = payload.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        content = message.get("content") or message.get("reasoning_content") or choices[0].get("text")
    content = content or payload.get("output_text") or ""
    if isinstance(content, list):
        content = "".join(
            str(item.get("text") or item.get("content") or "") if isinstance(item, dict) else str(item)
            for item in content
        )
    if isinstance(content, dict):
        return content
    text = re.sub(r"```(?:json)?", "", str(content), flags=re.I).replace("```", "").strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError("AI响应中没有完整JSON对象")


def _model_candidates(primary: str, candidates: tuple[str, ...] = ()) -> list[str]:
    return list(dict.fromkeys(
        model for model in (str(primary or "").strip(), *(str(item).strip() for item in candidates)) if model
    ))


def _retryable_model_response(response: requests.Response) -> bool:
    text = response.text.casefold()
    return response.status_code == 429 or response.status_code >= 500 or any(
        marker in text for marker in ("model_not_found", "no available channel")
    )


class ChatJsonClient:
    def __init__(self, api_url: str, api_key: str, model: str, timeout: int = 240, models: tuple[str, ...] = ()) -> None:
        self.api_url = normalize_chat_url(api_url)
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.models = _model_candidates(self.model, models)
        self.timeout = timeout

    def complete(self, system: str, prompt: str) -> dict[str, Any]:
        if not self.api_url or not self.api_key or not self.model:
            raise ValueError("请先配置AI文案地址、密钥和模型")
        response = None
        for index, model in enumerate(self.models):
            response = ai_post("text", requests.post,
                self.api_url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                "model": model,
                "temperature": 0.1,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            },
                timeout=self.timeout,
            )
            if response.ok or not _retryable_model_response(response) or index == len(self.models) - 1:
                break
        assert response is not None
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(f"AI接口返回HTTP {response.status_code}：{response.text[:1200]}") from exc
        try:
            return _extract_json(response.json())
        except ValueError as exc:
            raise RuntimeError("AI接口未返回JSON") from exc


class ListingContentService:
    def __init__(self, client: ChatJsonClient) -> None:
        self.client = client

    def generate(
        self,
        product: dict[str, Any],
        draft: OzonListingDraft,
        definitions: list[dict[str, Any]],
        sourcing: dict[str, Any] | None = None,
        visual_analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        candidates = [
            {
                "id": int(item.get("id") or 0),
                "name": str(item.get("name") or ""),
                "description": str(item.get("description") or "")[:500],
                "dictionary": bool(item.get("dictionary_id")),
                "collection": bool(item.get("is_collection")),
                "complex_id": int(item.get("attribute_complex_id") or 0),
                "allowed_values": item.get("_ai_allowed_values") or [],
                "allowed_value_count": int(item.get("_ai_allowed_value_count") or 0),
            }
            for item in definitions if item.get("id") and not _is_brand_attribute(item)
        ]
        candidates.sort(key=lambda item: (not next(
            (bool(raw.get("is_required")) for raw in definitions if int(raw.get("id") or 0) == item["id"]), False
        ), item["id"]))
        candidates = candidates[:60]
        facts = {
            "ozon_title": product.get("title"),
            "ozon_category": product.get("category"),
            "ozon_weight_grams": product.get("weight_grams"),
            "brand_policy": "The listing is unbranded. Never output any brand word.",
            "ozon_raw_export": _json_value(product.get("raw_json")),
            "selected_1688": (sourcing or {}).get("selected") or {},
            "conservative_visual_analysis": visual_analysis or {},
            "fillable_ozon_attributes": candidates,
            "content_variant_label": (draft.ai_preparation or {}).get("content_variant_label") or "",
            "must_differ_from_existing_copy": (draft.ai_preparation or {}).get("must_differ_from_existing_copy") or {},
            "store_creative_profile": (draft.ai_preparation or {}).get("store_creative_profile") or {},
        }
        prompt = (
            "只依据给出的Ozon导出事实、已确认1688货源事实和保守视觉分析，准备Ozon俄文上架资料。"
            "品牌固定为无品牌；标题、描述、标签中禁止出现来源品牌词、促销词、折扣、赠品、最低价等营销承诺。"
            "禁止编造重量、精确尺寸、材质、成分、认证、保修、保质期、包装数量、配件、兼容性或性能。"
            "但对适用对象、目标受众、使用场景、茶/咖啡等一般用途、节日送礼、风格主题和配套描述等宽泛营销属性，"
            "允许依据商品类型、图片语义和普通消费常识进行合理推断，不要求来源数据逐字证明；应主动尽量填写。"
            "除这些宽泛营销属性外，没有可靠事实的技术属性必须留空并说明原因。"
            "返回严格JSON对象，字段为：title_ru字符串，description_ru字符串，"
            "tags_ru以30个不同且相关的俄文主题标签为目标，应尽量写满30个，不能只给少量标签；每项必须以#开头，多词用下划线连接，每项含#最多30字符；不得靠无关词、重复变体或虚假信息凑数，确实不足30个时在tags_shortfall_reason说明事实限制；"
            "主题标签使用俄罗斯买家自然的俄语检索表达，优先简洁常见词组，按图案、风格、家居布置和真实场景展开；不做中文逐字翻译、不造词、不堆叠近义重复词，不宣称未经检索数据证实的热搜。标题、描述、标签和海报短句均不得出现商标品牌及促销词；主题标签不写品牌、参数、货号、型号、促销词或商品名称。poster_text_lines_ru为1到4条短俄文文案数组，"
            "creative_direction_en为英文海报艺术指导，attributes为数组。"
            "同时根据当前商品类型、用途和主图事实动态选择适合的场景、构图和展示方式；不要套用与商品无关的固定模板。"
            "返回可选的 differentiation_plan 对象，包含 scene、composition、camera_viewpoint、background_environment、"
            "lighting、typography_placement，用于本店铺与其他店铺生成明显不同但商品事实一致的主图。"
            "海报文案只能使用已证实事实；creative_direction_en必须描述适合商品类目的3:4商业海报，"
            "包括前景/承托面、主体层、背景层、光线、景深、克制的多色调色板、上方标题区和下方事实徽章区，"
            "不能只写简单纯色背景。attributes每项必须是 "
            "{attribute_id,value,confidence,evidence}；confidence为0到1。"
            "字典属性的value只是用于Ozon实时搜索的查询词，不能虚构ID。"
            "如果属性提供allowed_values，value必须严格选其中一个或多个原值；多选用分号分隔。"
            "标题不超过500字符；描述信息充分但不超过6000字符，均不得包含品牌词或促销词。"
            "store_creative_profile是当前店铺必须遵守的独立创意档案：严格执行其文案角度、标题句法、描述段落顺序及全部视觉字段。"
            "店铺创意档案只规定差异化方向，不得覆盖商品事实；如果档案与商品类型不适配，必须按商品类型调整为合理方案。"
            "如果事实中提供must_differ_from_existing_copy，必须在不改变事实的前提下重新组织标题、描述、海报短句和创意方向，不得复用原文。"
            "尽量填写有可靠文字或视觉证据的必填和非必填类目属性，没有证据则留空。事实如下：\n"
            + json.dumps(facts, ensure_ascii=False)
        )
        system = "You create factual Russian marketplace listings and art-directed product posters. Return strict JSON and never invent missing facts."

        def normalize_copy(text: Any) -> str:
            return re.sub(r"[^0-9a-zа-яё]+", " ", str(text or "").casefold()).strip()

        references: list[tuple[str, str]] = []
        different_from = facts["must_differ_from_existing_copy"]
        if isinstance(different_from, dict):
            comparison_items = [different_from.get("primary"), *(different_from.get("other_store_variants") or [])]
            for item in comparison_items:
                if isinstance(item, dict):
                    reference_title = normalize_copy(item.get("title_ru") or item.get("title"))
                    reference_description = normalize_copy(item.get("description_ru"))
                    if reference_title or reference_description:
                        references.append((reference_title, reference_description))

        value: dict[str, Any] = {}
        similarity_failures: list[str] = []
        for attempt in range(2):
            retry_instruction = ""
            if attempt:
                retry_instruction = (
                    "\n上一版因与其他店铺文案过于相似而被拒绝：" + "；".join(similarity_failures)
                    + "。必须重新构思，改变标题句法和词序，并严格改用创意档案规定的段落顺序；不得只替换同义词。"
                )
            value = self.client.complete(system, prompt + retry_instruction)
            candidate_title = normalize_copy(value.get("title_ru"))
            candidate_description = normalize_copy(value.get("description_ru"))
            similarity_failures = []
            for reference_index, (reference_title, reference_description) in enumerate(references, start=1):
                if candidate_title and reference_title:
                    ratio = SequenceMatcher(None, candidate_title, reference_title).ratio()
                    if ratio >= 0.82:
                        similarity_failures.append(f"标题与版本{reference_index}相似度{ratio:.0%}，必须低于82%")
                if candidate_description and reference_description:
                    ratio = SequenceMatcher(None, candidate_description, reference_description).ratio()
                    if ratio >= 0.70:
                        similarity_failures.append(f"描述与版本{reference_index}相似度{ratio:.0%}，必须低于70%")
            if not similarity_failures:
                break
        if similarity_failures:
            raise RuntimeError("店铺独立文案连续两次未达到差异要求：" + "；".join(similarity_failures))
        brand = str(product.get("brand") or "").strip()
        title = _clean_marketing_copy(re.sub(r"\s+", " ", str(value.get("title_ru") or "")), brand)[:500]
        description = _clean_marketing_copy(str(value.get("description_ru") or ""), brand)[:6000]
        if not title or not description:
            raise RuntimeError("AI没有返回可用的俄文标题或描述")
        tags = normalize_hashtags(value.get("tags_ru"))
        poster_lines = [line for line in _string_list(value.get("poster_text_lines_ru"), 4) if len(line) <= 68]
        attributes = []
        allowed = {item["id"] for item in candidates}
        for raw in value.get("attributes") or []:
            if not isinstance(raw, dict):
                continue
            try:
                attribute_id = int(raw.get("attribute_id"))
                confidence = float(raw.get("confidence") or 0)
            except (TypeError, ValueError):
                continue
            text = re.sub(r"\s+", " ", str(raw.get("value") or "")).strip()
            minimum_confidence = 0.65 if attribute_id in BROAD_INFERENCE_ATTRIBUTE_IDS else 0.80
            if attribute_id in allowed and text and confidence >= minimum_confidence:
                attributes.append({
                    "attribute_id": attribute_id,
                    "value": text,
                    "confidence": min(1.0, confidence),
                    "evidence": re.sub(r"\s+", " ", str(raw.get("evidence") or "")).strip()[:500],
                })
        return {
            "title_ru": title,
            "description_ru": description,
            "tags_ru": tags,
            "tags_shortfall_reason": str(value.get("tags_shortfall_reason") or "").strip()[:500],
            "poster_text_lines_ru": poster_lines or [title[:68]],
            "creative_direction_en": re.sub(
                r"\s+", " ", str(value.get("creative_direction_en") or "")
            ).strip()[:1600],
            "differentiation_plan": value.get("differentiation_plan") if isinstance(value.get("differentiation_plan"), dict) else {},
            "store_creative_profile": dict(facts["store_creative_profile"]),
            "attributes": attributes,
        }


class VisionAnalysisService:
    def __init__(self, api_url: str, api_key: str, model: str, timeout: int = 240, models: tuple[str, ...] = ()) -> None:
        self.api_url = normalize_chat_url(api_url)
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.models = _model_candidates(self.model, models)
        self.timeout = timeout

    def analyze(
        self, reference_paths: Path | list[Path], product: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        paths = reference_paths if isinstance(reference_paths, list) else [reference_paths]
        paths = paths[:6]
        prompt = (
            "Analyze the first supplied image as the authoritative current SKU. Any later images are weak, "
            "reference-only context and may show another color, size, or bundle variant; never use them to override "
            "facts from the first image. Return strict JSON with: "
            "product_type_ru, colors_ru array, visible_components_ru array, visible_material_cues_ru array, "
            "unit_count integer, unit_count_confidence number 0..1, "
            "living_subject object {type: human_model|dog|cat|other_pet|none,count,relationship_en,preserve_subject}, "
            "poster_text_lines_ru array with one Russian headline and up to three short factual callouts, "
            "product_bbox_norm array [left,top,right,bottom] using 0..1 coordinates around the complete sellable product, "
            "source_design_to_avoid object {composition_en,background_en,palette_en,typography_en,props_en}, "
            "art_direction_en, and notes_ru. source_design_to_avoid must describe the first image's poster design only, "
            "not the product, so a new design can explicitly avoid copying it. art_direction_en must describe a professionally art-directed edge-to-edge "
            "3:4 Russian marketplace poster with a category-appropriate physical scene, foreground/base, product plane, "
            "designed background, deliberate lighting and depth, controlled multi-color palette, an upper headline zone "
            "and compact lower factual badges. Avoid a plain catalog background. Choose one coherent SKU presentation, "
            "not a collage. Count complete sellable units visible together in the clearest full-set image, not product "
            "components. Preserve a visible pet or human when it materially demonstrates wearing or use. "
            "Describe only directly visible facts or supplied Ozon facts. Never infer hidden materials, functions, "
            "dimensions, brand, compatibility, package contents outside the frame, or performance. Ozon facts:\n"
            + json.dumps(product or {}, ensure_ascii=False)[:12000]
        )
        return self._vision_json(prompt, paths)
    def verify_generated(
        self,
        reference_paths: Path | list[Path],
        generated_path: Path,
        *,
        expected_unit_count: int = 1,
        living_subject: dict[str, Any] | None = None,
        expected_text_lines: list[str] | None = None,
        raise_on_failure: bool = True,
    ) -> dict[str, Any]:
        paths = reference_paths if isinstance(reference_paths, list) else [reference_paths]
        subject = living_subject if isinstance(living_subject, dict) else {}
        text_lines = [str(line).strip() for line in expected_text_lines or [] if str(line).strip()]
        prompt = (
            f"The first {len(paths)} images are authoritative original views of one SKU and the final image is an "
            "AI-generated Ozon main image. "
            "Return strict JSON: matches_sku boolean, text_matches boolean, confidence number 0..1, reason_ru string. "
            "matches_sku can be true only when product type, unit count, color distribution, silhouette, construction, "
            "visible components, accessories and surface pattern are preserved. Background, lighting, camera angle and "
            "Russian poster graphics may differ. Any missing, extra, recolored or redesigned product part means false. "
            f"The generated image must show exactly {max(1, expected_unit_count)} complete sellable unit(s). "
            f"Required living-subject context: {json.dumps(subject, ensure_ascii=False)}. "
            f"Exact visible Russian text whitelist: {json.dumps(text_lines, ensure_ascii=False)}. "
            "text_matches is true only if every whitelisted line appears once with exactly the supplied Cyrillic spelling "
            "and there is no extra, omitted, malformed or pseudo-text. When the whitelist is nonempty, matches_sku must "
            "also be false if text_matches is false."
        )
        result = self._vision_json(prompt, [*paths, generated_path])
        try:
            confidence = float(result.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0
        claimed = result.get("matches_sku") is True or str(result.get("matches_sku")).casefold() == "true"
        text_claimed = result.get("text_matches") is True or str(result.get("text_matches")).casefold() == "true"
        result["confidence"] = max(0.0, min(1.0, confidence))
        result["text_matches"] = bool(text_claimed) if text_lines else True
        result["matches_sku"] = bool(claimed and result["text_matches"] and confidence >= 0.80)
        if not result["matches_sku"] and raise_on_failure:
            raise RuntimeError(
                "AI主图未通过商品一致性校验：" + str(result.get("reason_ru") or "置信度不足")
            )
        return result

    def _vision_json(self, prompt: str, paths: list[Path]) -> dict[str, Any]:
        if not self.api_url or not self.api_key or not self.model:
            raise ValueError("请先配置视觉AI地址、密钥和模型")
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        # Vision relays enforce a total image-patch budget.  Keep the generated
        # candidate (last path) plus at most two references and resize each
        # before encoding; the source files themselves remain untouched.
        selected_paths = paths if len(paths) <= 3 else [paths[0], paths[1], paths[-1]]
        for path in selected_paths:
            content.append({
                "type": "image_url",
                "image_url": {"url": _image_data_url(path, maximum=768), "detail": "low"},
            })
        response = None
        for index, model in enumerate(self.models):
            response = ai_post("text", requests.post,
                self.api_url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                "model": model,
                "temperature": 0,
                "stream": False,
                "messages": [
                    {"role": "system", "content": "You are a conservative product vision verifier. Return strict JSON."},
                    {"role": "user", "content": content},
                ],
            },
                timeout=self.timeout,
            )
            if response.ok or not _retryable_model_response(response) or index == len(self.models) - 1:
                break
        assert response is not None
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(f"视觉AI返回HTTP {response.status_code}：{response.text[:1200]}") from exc
        return _extract_json(response.json())


def build_product_master(reference_path: Path, visual_analysis: dict[str, Any], work_dir: Path) -> Path:
    """Create a reusable product-focused reference that excludes most source-poster design."""
    raw_bbox = visual_analysis.get("product_bbox_norm")
    if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
        return reference_path
    try:
        left, top, right, bottom = [max(0.0, min(1.0, float(value))) for value in raw_bbox]
    except (TypeError, ValueError):
        return reference_path
    if right - left < 0.12 or bottom - top < 0.12:
        return reference_path
    with Image.open(reference_path) as source:
        source = source.convert("RGB")
        pad_x = (right - left) * 0.06
        pad_y = (bottom - top) * 0.06
        box = (
            int(max(0.0, left - pad_x) * source.width),
            int(max(0.0, top - pad_y) * source.height),
            int(min(1.0, right + pad_x) * source.width),
            int(min(1.0, bottom + pad_y) * source.height),
        )
        if box[2] <= box[0] or box[3] <= box[1]:
            return reference_path
        master = source.crop(box)
        target = work_dir / "product_master.jpg"
        master.save(target, "JPEG", quality=95)
    return target


def image_difference_score(first: Path, second: Path) -> float:
    """Return 0 for an identical poster and approach 1 as the full design changes."""
    try:
        with Image.open(first) as a_source, Image.open(second) as b_source:
            a = a_source.convert("RGB").resize((96, 128))
            b = b_source.convert("RGB").resize((96, 128))
            means = ImageStat.Stat(ImageChops.difference(a, b)).mean
            return max(0.0, min(1.0, sum(means) / (3 * 255.0)))
    except (OSError, ValueError):
        return 1.0


def apply_ai_attributes(
    draft: OzonListingDraft,
    definitions: list[dict[str, Any]],
    suggestions: list[dict[str, Any]],
    client: OzonSellerClient,
) -> dict[str, Any]:
    definition_by_id = {int(item.get("id") or 0): item for item in definitions}
    applied: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for suggestion in suggestions:
        attribute_id = int(suggestion["attribute_id"])
        definition = definition_by_id.get(attribute_id)
        if not definition:
            continue
        if attribute_id == RICH_CONTENT_ATTRIBUTE_ID:
            continue
        raw_text = str(suggestion["value"]).strip()
        candidate_values = [raw_text]
        if definition.get("is_collection"):
            candidate_values = [
                value.strip() for value in re.split(r"[;；|\r\n]+", raw_text)
                if value.strip()
            ]
            maximum = int(definition.get("max_value_count") or 0)
            if maximum:
                candidate_values = candidate_values[:maximum]
        if _is_color_definition(definition):
            multicolor = next((value for value in candidate_values if _is_multicolor(value)), None)
            candidate_values = [multicolor] if multicolor else candidate_values[:3]
        wrote_value = False
        for candidate in candidate_values:
            text = candidate
            dictionary_id = 0
            if definition.get("dictionary_id"):
                match = None
                for query in _dictionary_search_queries(candidate):
                    options = client.search_category_attribute_values(
                        draft.description_category_id,
                        draft.type_id,
                        attribute_id,
                        query,
                        language="RU",
                    )
                    match = _unique_dictionary_match(query, options)
                    if match:
                        break
                if not match:
                    pending.append({
                        **suggestion,
                        "value": candidate,
                        "reason": "Ozon字典值无法唯一确认",
                    })
                    continue
                dictionary_id = int(match["id"])
                text = str(match.get("value") or match.get("name") or candidate)
            _set_attribute(
                draft, definition, text, dictionary_id, suggestion,
                append=wrote_value and bool(definition.get("is_collection")),
            )
            wrote_value = True
            applied.append({
                "attribute_id": attribute_id,
                "name": definition.get("name"),
                "value": text,
                "dictionary_value_id": dictionary_id,
                "confidence": suggestion.get("confidence"),
                "evidence": suggestion.get("evidence"),
            })
    return {"applied": applied, "pending": pending}


def _is_color_definition(definition: dict[str, Any]) -> bool:
    if int(definition.get("id") or 0) == 10096:
        return True
    name = str(definition.get("name") or "").casefold()
    return any(token in name for token in ("颜色", "цвет товара", "product color"))


def _is_multicolor(value: Any) -> bool:
    normalized = re.sub(r"\s+", " ", str(value or "")).strip().casefold().replace("ё", "е")
    return normalized in {"多色", "彩色", "разноцветный", "многоцветный", "multicolor", "multi-color"}


def repair_ozon_rejected_attributes(
    *,
    config: AppConfig,
    product: dict[str, Any],
    draft: OzonListingDraft,
    definitions: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    ozon_client: OzonSellerClient,
) -> dict[str, Any]:
    """Repair only attributes explicitly rejected by Ozon before one controlled retry."""
    messages: list[dict[str, Any]] = []
    attribute_ids: set[int] = set()
    for item in errors:
        for error in item.get("errors") or []:
            if not isinstance(error, dict):
                continue
            try:
                attribute_id = int(error.get("attribute_id") or 0)
            except (TypeError, ValueError):
                attribute_id = 0
            messages.append({
                "attribute_id": attribute_id,
                "attribute_name": str(error.get("attribute_name") or ""),
                "code": str(error.get("code") or ""),
                "message": str(error.get("message") or error.get("description") or "")[:1000],
            })
            if attribute_id:
                attribute_ids.add(attribute_id)
    if not attribute_ids:
        raise RuntimeError("Ozon没有返回可自动修复的属性ID")

    target_definitions: list[dict[str, Any]] = []
    for definition in definitions:
        attribute_id = int(definition.get("id") or 0)
        if attribute_id not in attribute_ids:
            continue
        enriched = dict(definition)
        if definition.get("dictionary_id"):
            values = ozon_client.category_attribute_values(
                draft.description_category_id, draft.type_id, attribute_id, language="RU"
            )
            enriched["allowed_values"] = [
                str(value.get("value") or value.get("name") or "").strip()
                for value in values[:200]
                if str(value.get("value") or value.get("name") or "").strip()
            ]
        target_definitions.append(enriched)

    enforce_system_attributes(draft, definitions)
    system_fixed = {9070}
    ai_targets = [item for item in target_definitions if int(item.get("id") or 0) not in system_fixed]
    suggestions: list[dict[str, Any]] = []
    if ai_targets:
        prompt = {
            "ozon_errors": messages,
            "rejected_attribute_definitions": ai_targets,
            "product_facts": {
                "title": product.get("title"), "category": product.get("category"),
                "raw_export": _json_value(product.get("raw_json")),
                "selected_1688_title": draft.selected_1688_title,
                "selected_1688_sku_name": draft.selected_1688_sku_name,
            },
            "rules": [
                "Return only rejected attributes that can be corrected from facts.",
                "For dictionary attributes choose exact allowed_values text.",
                "For collection values separate entries with semicolons.",
                "Color has at most three values; if multicolor use only Разноцветный.",
                "Use action=remove for an optional attribute that cannot be supported.",
            ],
        }
        response = ChatJsonClient(config.ai_api_url, config.ai_api_key, config.ai_model).complete(
            "You repair rejected Ozon listing attributes. Return strict JSON: attributes is an array of {attribute_id,value,action,evidence}.",
            json.dumps(prompt, ensure_ascii=False),
        )
        for raw in response.get("attributes") or []:
            if not isinstance(raw, dict):
                continue
            try:
                attribute_id = int(raw.get("attribute_id") or 0)
            except (TypeError, ValueError):
                continue
            if attribute_id not in attribute_ids:
                continue
            if str(raw.get("action") or "").casefold() == "remove":
                _remove_attribute(draft, attribute_id)
                continue
            value = str(raw.get("value") or "").strip()
            if value:
                suggestions.append({
                    "attribute_id": attribute_id, "value": value, "confidence": 1.0,
                    "evidence": str(raw.get("evidence") or "Ozon拒绝后AI纠错")[:500],
                })
    applied = apply_ai_attributes(draft, definitions, suggestions, ozon_client)
    enforce_system_attributes(draft, definitions)
    return {"errors": messages, "target_attribute_ids": sorted(attribute_ids), **applied}


def _remove_attribute(draft: OzonListingDraft, attribute_id: int) -> None:
    draft.attributes = [item for item in draft.attributes if int(item.get("id") or 0) != attribute_id]
    for group in draft.complex_attributes:
        group["attributes"] = [
            item for item in group.get("attributes", [])
            if int(item.get("id") or 0) != attribute_id
        ]


def _set_attribute(
    draft: OzonListingDraft,
    definition: dict[str, Any],
    value: str,
    dictionary_value_id: int,
    source: dict[str, Any],
    *,
    append: bool = False,
) -> None:
    attribute_id = int(definition["id"])
    complex_id = int(definition.get("attribute_complex_id") or 0)
    all_items = list(draft.attributes)
    for group in draft.complex_attributes:
        all_items.extend(group.get("attributes", []))
    item = next((entry for entry in all_items if int(entry.get("id") or 0) == attribute_id), None)
    if item is None:
        item = {"complex_id": complex_id, "id": attribute_id, "values": []}
        if complex_id:
            group = next((g for g in draft.complex_attributes if int(g.get("_complex_id") or 0) == complex_id), None)
            if group is None:
                group = {"attributes": [], "_complex_id": complex_id}
                draft.complex_attributes.append(group)
            group["attributes"].append(item)
        else:
            draft.attributes.append(item)
    payload: dict[str, Any] = {"value": value}
    if dictionary_value_id:
        payload["dictionary_value_id"] = dictionary_value_id
    if append:
        identity = (payload.get("dictionary_value_id", 0), payload.get("value", "").casefold())
        present = {
            (entry.get("dictionary_value_id", 0), str(entry.get("value") or "").casefold())
            for entry in item.get("values", [])
        }
        if identity not in present:
            item.setdefault("values", []).append(payload)
    else:
        item["values"] = [payload]
    item["_name"] = str(definition.get("name") or attribute_id)
    item["_source"] = "AI"
    item["_confidence"] = source.get("confidence")
    item["_evidence"] = source.get("evidence")


def build_main_image_prompt(content: dict[str, Any]) -> str:
    lines = [re.sub(r"\s+", " ", str(line)).strip() for line in content.get("poster_text_lines_ru") or []]
    lines = [line for line in lines if line and not re.search(r"[\u3400-\u9fff]", line)][:4]
    whitelist = "\n".join(f'{index}. "{line}"' for index, line in enumerate(lines, 1))
    direction = re.sub(r"\s+", " ", str(content.get("creative_direction_en") or "")).strip()
    profile = content.get("store_creative_profile") if isinstance(content.get("store_creative_profile"), dict) else {}
    visual_profile_keys = (
        "composition", "camera_viewpoint", "background_environment",
        "palette", "lighting", "typography_placement",
    )
    profile_lines = "\n".join(
        f"- {key.replace('_', ' ').upper()}: {re.sub(r'\\s+', ' ', str(profile.get(key))).strip()}"
        for key in visual_profile_keys if str(profile.get(key) or "").strip()
    )
    visual = content.get("visual_analysis") if isinstance(content.get("visual_analysis"), dict) else {}
    source_design = visual.get("source_design_to_avoid") if isinstance(visual.get("source_design_to_avoid"), dict) else {}
    source_design_lines = "\n".join(
        f"- DO NOT COPY SOURCE {key.replace('_en', '').replace('_', ' ').upper()}: "
        f"{re.sub(r'\s+', ' ', str(value)).strip()}"
        for key, value in source_design.items() if str(value or "").strip()
    )
    colors = [re.sub(r"\s+", " ", str(value)).strip() for value in visual.get("colors_ru") or [] if str(value).strip()][:5]
    color_lock = ", ".join(colors) or "the exact product color visible in the reference image"
    try:
        count_confidence = float(visual.get("unit_count_confidence") or 0)
        unit_count = max(1, min(20, int(visual.get("unit_count") or 1)))
    except (TypeError, ValueError):
        count_confidence, unit_count = 0, 1
    if unit_count > 1 and count_confidence >= 0.80:
        unit_rule = (
            f"- The sellable SKU is one set containing exactly {unit_count} complete units. Show exactly {unit_count} "
            "complete units together as one coherent grouped set; never replace a missing unit with text, a badge, crop, reflection, or partial object."
        )
        collage_rule = f"Do not add duplicates beyond the required {unit_count}-unit set."
    else:
        unit_count = 1
        unit_rule = "- Show exactly one complete dominant product unit in one physically possible view."
        collage_rule = "Do not repeat or duplicate the product."
    living = visual.get("living_subject") if isinstance(visual.get("living_subject"), dict) else {}
    subject_type = str(living.get("type") or "").strip().casefold()
    preserve_subject = living.get("preserve_subject") is True or str(living.get("preserve_subject")).casefold() == "true"
    if subject_type in {"human_model", "dog", "cat", "other_pet"} and preserve_subject:
        relationship = re.sub(r"\s+", " ", str(living.get("relationship_en") or "")).strip()[:240]
        living_rule = (
            f"Preserve the visible {subject_type.replace('_', ' ')} and its natural relationship to the product"
            + (f": {relationship}." if relationship else ".")
            + " Keep realistic anatomy, contact, pose and occlusion while the product remains fully recognizable."
        )
    else:
        living_rule = "Do not invent a person, hand, dog, cat, or other pet when no required living subject was confirmed."
    visual_direction = re.sub(r"\s+", " ", str(visual.get("art_direction_en") or "")).strip()
    direction = " ".join(value for value in (visual_direction, direction) if value)[:1600]
    if len(lines) == 1:
        text_layout = "Use line 1 as one large primary headline; create no unsupported callout cards."
    else:
        text_layout = (
            f"Render exactly {len(lines)} text elements: line 1 as the large headline and the remaining lines as compact factual badges."
        )
    return f"""Create one finished commercial Russian-language Ozon product advertising poster on a true edge-to-edge vertical 3:4 canvas.

SKU LOCK — HIGHEST PRIORITY:
- The uploaded image is the sole authority for the sellable SKU.
{unit_rule}
- PRESERVE PRODUCT COLOR EXACTLY: {color_lock}. Background, lighting, and decorative accents must not recolor, tint, darken, lighten, or shift the product.
- Preserve exact silhouette, proportions, construction, component count, surface pattern, hardware, accessories, and product-native branding.
- Never add, remove, recolor, merge, redesign, or duplicate product parts.
- {living_rule}

POSTER DESIGN:
- Make a professionally art-directed marketplace advertising poster, not a plain catalog or generic lifestyle photo.
- Product occupies about 50-72% of the useful frame, with clear edges and an intentional text zone.
- Build three readable layers: a category-appropriate foreground/base, the dominant product plane, and a designed background.
- Add neutral, color-accurate lighting, natural depth, contact shadows, strong product separation, and restrained graphic accents. Use color variation only in the background and decoration, never on the product.
- Use bold modern Cyrillic typography readable on a phone, with a clear upper headline zone and compact lower fact modules.
- Avoid empty beige catalog layouts, random podiums, decorative blur, excess empty space, and weak hierarchy.
- No collage, split screen, multiple views, fake claims, QR codes, UI, new logos, watermarks, seller typography, or pseudo-text. {collage_rule}
- Never invent specifications, functions, materials, compatibility, accessories, or benefits.

STORE VARIANT ART DIRECTION — MANDATORY AND VISIBLY DISTINCT:
{profile_lines or '- Follow the assigned store profile and make the scene visibly distinct from other store variants.'}
- The composition, camera viewpoint, background environment, palette, lighting, and typography placement above are requirements, not suggestions.
- Do not fall back to the composition or visual treatment of another store variant. These scene rules cannot override SKU LOCK.

SOURCE POSTER DESIGN EXCLUSION — KEEP THE PRODUCT, DISCARD THE OLD ADVERTISING DESIGN:
{source_design_lines or '- Do not copy the source background, camera framing, props, palette, typography positions, badges, or negative-space layout.'}
- Treat the uploaded crop as product identity evidence only. Build a new environment and a new poster grid from scratch.

VISIBLE RUSSIAN TEXT — EXACT WHITELIST:
{whitelist or '(no visible text)'}
{text_layout if lines else 'Add no visible text.'}
Reproduce supplied Cyrillic exactly. Do not translate, paraphrase, duplicate, misspell, omit, or add text.

CREATIVE DIRECTION — SCENE/PRESENTATION ONLY; IT CANNOT OVERRIDE SKU LOCK:
{direction or 'Create a polished category-appropriate marketplace scene with designed lighting, natural depth, strong product separation and a controlled palette.'}"""[:MAX_IMAGE_PROMPT_CHARS]


class ImageEditService:
    def __init__(self, api_url: str, api_key: str, model: str, timeout: int = 600, attempts: int = 3) -> None:
        self.api_url = normalize_image_url(api_url)
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.timeout = timeout
        self.attempts = max(1, min(5, int(attempts or 1)))

    def generate(self, reference_path: str, prompt: str, output_dir: Path) -> Path:
        from erp.core.ai_reuse import fingerprint, result_cache, read_json, write_json
        key = fingerprint({"version":1,"endpoint":self.api_url,"account":fingerprint(self.api_key),"model":self.model,"prompt":str(prompt or "")[:MAX_IMAGE_PROMPT_CHARS],"source":hashlib.sha256(Path(reference_path).read_bytes()).hexdigest(),"output":str(output_dir.resolve())})
        with result_cache("image-receipts",key,timeout=self.timeout+60) as receipt:
            payload=read_json(receipt)
            if payload is None:
                payload=self._generate_payload(reference_path,prompt,output_dir)
                write_json(receipt,payload)
            # Download/decode failures retain the paid response for the next
            # attempt. Explicit regeneration after success gets a fresh image.
            target=self._save_payload(payload,output_dir)
            receipt.unlink(missing_ok=True)
            return target

    def _generate_payload(self, reference_path: str, prompt: str, output_dir: Path) -> dict:
        if not self.api_url or not self.api_key or not self.model:
            raise ValueError("请先配置生图API地址、密钥和模型")
        prompt = str(prompt or "")[:MAX_IMAGE_PROMPT_CHARS]
        output_dir.mkdir(parents=True, exist_ok=True)
        source = Path(reference_path)
        response = None
        last_error: requests.RequestException | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                with source.open("rb") as handle:
                    response = ai_post("image", requests.post,
                        self.api_url,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        files={"image": (source.name, handle, mimetypes.guess_type(source.name)[0] or "image/jpeg")},
                        data={
                            "model": self.model,
                            "prompt": prompt,
                            "n": 1,
                            "size": "1792x2400",
                            # GPT Image 2.5 currently accepts only `low` on the
                            # configured compatible provider. Older image models
                            # retain their previous high-quality setting.
                            "quality": "low" if "gpt-image-2.5" in self.model.casefold() else "high",
                            "response_format": "b64_json",
                        },
                        timeout=(30, self.timeout),
                    )
                break
            except (requests.ConnectTimeout, requests.ConnectionError, requests.ReadTimeout) as exc:
                last_error = exc
                if attempt < self.attempts:
                    time.sleep(min(8, 2 ** attempt))
        if response is None:
            raise RuntimeError(
                f"生图接口连续{self.attempts}次连接失败，请检查生图API地址或网络后点击AI生成重试：{last_error}"
            ) from last_error
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(f"生图接口返回HTTP {response.status_code}：{response.text[:1200]}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("生图接口没有返回有效JSON") from exc
        if payload.get("error"):
            raise RuntimeError("生图接口错误：" + str(payload["error"]))
        items = payload.get("data") or payload.get("images") or []
        if not items:
            raise RuntimeError("生图接口没有返回图片")
        return payload

    def _save_payload(self, payload: dict, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        items = payload.get("data") or payload.get("images") or []
        item = items[0]
        if item.get("b64_json"):
            raw = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            download = requests.get(item["url"], timeout=180)
            download.raise_for_status()
            raw = download.content
        else:
            raise RuntimeError("生图结果缺少b64_json或url")
        target = output_dir / f"ai_main_{time.time_ns()}.jpg"
        with Image.open(io.BytesIO(raw)) as image:
            image.convert("RGB").save(target, "JPEG", quality=95, optimize=True)
        return target


def download_reference_image(
    url: str,
    target_dir: Path,
    filename: str = "reference.jpg",
    *,
    attempts: int = 4,
) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    requested = Path(filename)
    url_fingerprint = hashlib.sha256(str(url).strip().encode("utf-8")).hexdigest()[:12]
    suffix = requested.suffix or ".jpg"
    target = target_dir / f"{requested.stem}_{url_fingerprint}{suffix}"
    if target.is_file():
        try:
            with Image.open(target) as cached:
                cached.verify()
            return target
        except OSError:
            pass

    request_attempts = max(1, min(6, int(attempts or 1)))
    last_error: Exception | None = None
    converted: Image.Image | None = None
    for attempt in range(1, request_attempts + 1):
        try:
            response = requests.get(
                str(url),
                timeout=(15, 120),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/140.0.0.0 Safari/537.36"
                    ),
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    "Referer": "https://www.ozon.ru/",
                },
            )
            response.raise_for_status()
            with Image.open(io.BytesIO(response.content)) as image:
                image.load()
                converted = image.convert("RGB")
            break
        except (requests.RequestException, OSError) as exc:
            last_error = exc
            if attempt < request_attempts:
                time.sleep(min(8, 2 ** attempt))
    if converted is None:
        raise RuntimeError(
            f"Ozon图片连续{request_attempts}次下载失败：{url}；{last_error}"
        ) from last_error

    temp_target = target.with_name(target.name + ".tmp")
    converted.save(temp_target, "JPEG", quality=95)
    temp_target.replace(target)
    return target


class OssUploader:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    @staticmethod
    def object_key(
        object_prefix: str,
        offer_id: str,
        filename: str,
        *,
        month: str | None = None,
        day: str | None = None,
    ) -> str:
        safe_offer = re.sub(r"[^A-Za-z0-9._#-]+", "_", str(offer_id)).strip("._-") or "offer"
        if safe_offer != str(offer_id):
            safe_offer += "-" + hashlib.sha256(str(offer_id).encode()).hexdigest()[:8]
        safe_filename = re.sub(r"[\\/:*?\"<>|]+", "_", str(filename or "图片.jpg")).strip(" .") or "图片.jpg"
        prefix = str(object_prefix or "ozon-auto-listing").strip("/")
        return f"{prefix}/{month or time.strftime('%m')}/{day or time.strftime('%d')}/{safe_offer}/{safe_filename}"

    def upload(self, path: Path, offer_id: str, filename: str = "主图.jpg") -> str:
        try:
            import alibabacloud_oss_v2 as oss
        except ImportError as exc:
            raise RuntimeError("缺少阿里云OSS SDK") from exc
        required = {
            "region": self.config.oss_region,
            "endpoint": self.config.oss_endpoint,
            "bucket": self.config.oss_bucket,
            "access_key_id": self.config.oss_access_key_id,
            "access_key_secret": self.config.oss_access_key_secret,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError("OSS配置缺少：" + "、".join(missing))
        os.environ["OSS_ACCESS_KEY_ID"] = self.config.oss_access_key_id
        os.environ["OSS_ACCESS_KEY_SECRET"] = self.config.oss_access_key_secret
        client_config = oss.config.load_default()
        client_config.credentials_provider = oss.credentials.EnvironmentVariableCredentialsProvider()
        client_config.region = self.config.oss_region
        client_config.endpoint = self.config.oss_endpoint
        client = oss.Client(client_config)
        key = self.object_key(
            self.config.oss_object_prefix,
            offer_id,
            filename,
        )
        with path.open("rb") as body:
            client.put_object(oss.PutObjectRequest(
                bucket=self.config.oss_bucket,
                key=key,
                body=body,
                content_type="image/jpeg",
            ))
        base = str(
            self.config.oss_public_base_url
            or f"https://{self.config.oss_bucket}.{self.config.oss_endpoint}"
        ).rstrip("/")
        return f"{base}/{quote(key, safe='/')}"


def _oss_upload_scope(draft: OzonListingDraft) -> str:
    """Keep objects isolated when separate seller accounts reuse an Offer ID."""
    return str((draft.ai_preparation or {}).get("oss_upload_scope") or draft.offer_id)


def refresh_additional_images(
    *,
    config: AppConfig,
    product: dict[str, Any],
    draft: OzonListingDraft,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Fetch the original Ozon gallery and replace additional images with OSS URLs."""
    progress = on_progress or (lambda _message: None)
    source_url = str(draft.source_ozon_url or product.get("ozon_url") or "").strip()
    original_main = str(product.get("image_url") or "").strip()
    if not original_main:
        raise ValueError("来源商品没有Ozon主图，无法判断并排除图库第一张")
    try:
        gallery = scrape_ozon_gallery(
            source_url,
            timeout=15,
            browser_fallback=False,
            on_progress=progress,
        )
    except RuntimeError:
        gallery = wait_for_extension_gallery(
            source_url,
            draft.source_sku,
            timeout=180,
            on_progress=progress,
        )
    return upload_captured_gallery_images(
        config=config,
        product=product,
        draft=draft,
        gallery=gallery,
        on_progress=progress,
    )


def upload_captured_gallery_images(
    *,
    config: AppConfig,
    product: dict[str, Any],
    draft: OzonListingDraft,
    gallery: Any,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Download captured Ozon originals and persist only their OSS URLs."""
    progress = on_progress or (lambda _message: None)
    source_url = str(draft.source_ozon_url or product.get("ozon_url") or "").strip()
    original_main = str(product.get("image_url") or "").strip()
    if not original_main:
        raise ValueError("来源商品没有Ozon主图，无法判断并排除图库第一张")
    originals = additional_gallery_images(gallery.images, original_main)
    work_dir = DATA_DIR / "generated" / draft.source_sku
    uploader = OssUploader(config)
    uploaded: list[str] = []
    for index, original_url in enumerate(originals, start=1):
        progress(f"正在下载并上传Ozon原附图（{index}/{len(originals)}）")
        local_path = download_reference_image(
            original_url, work_dir, f"ozon_additional_{index:02d}.jpg"
        )
        uploaded.append(uploader.upload(local_path, _oss_upload_scope(draft), f"附图{index:02d}.jpg"))
    draft.images = uploaded
    oss_base = str(
        config.oss_public_base_url
        or f"https://{config.oss_bucket}.{config.oss_endpoint}"
    ).rstrip("/")
    draft.images_oss_ready = bool(
        draft.primary_image.startswith(oss_base + "/")
        and len(uploaded) == len(originals)
    )
    metadata = {
        "source": gallery.source,
        "source_url": source_url,
        "original_gallery_count": len(gallery.images),
        "additional_image_count": len(uploaded),
        "uploaded_additional_image_urls": uploaded,
        "gallery_warning": "" if originals else "该商品图库只有主图，无需附图",
        "refreshed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    draft.ai_preparation = {
        **draft.ai_preparation,
        "gallery": metadata,
        "gallery_status": "READY",
        "gallery_source": gallery.source,
        "gallery_image_count": len(gallery.images),
        "gallery_warning": metadata["gallery_warning"],
    }
    return metadata


def prepare_sourcing_image(
    *,
    config: AppConfig,
    product: dict[str, Any],
    draft: OzonListingDraft,
) -> str:
    """Mirror the original Ozon main image to OSS before Newton reads it."""
    local_path = prepare_sourcing_image_file(product=product, draft=draft)
    return OssUploader(config).upload(local_path, _oss_upload_scope(draft), "找货源主图.jpg")


def prepare_sourcing_image_file(
    *,
    product: dict[str, Any],
    draft: OzonListingDraft,
) -> Path:
    """Download and retain the original Ozon main image for sourcing APIs."""
    original_url = re.sub(r"/wc\d+/", "/wc2000/", str(product.get("image_url") or "").strip())
    if not original_url.startswith("https://"):
        raise ValueError("来源商品没有有效的Ozon主图")
    work_dir = DATA_DIR / "generated" / draft.source_sku
    return download_reference_image(original_url, work_dir, "sourcing_reference.jpg")


def prepare_listing_with_ai(
    *,
    config: AppConfig,
    product: dict[str, Any],
    draft: OzonListingDraft,
    definitions: list[dict[str, Any]],
    ozon_client: OzonSellerClient,
    sourcing: dict[str, Any] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_content_checkpoint: Callable[[], None] | None = None,
    generate_image: bool = True,
) -> dict[str, Any]:
    progress = on_progress or (lambda _message: None)
    draft.images_oss_ready = False
    ensure_runtime_directories()
    work_dir = DATA_DIR / "generated" / draft.source_sku
    gallery_warning = ""
    original_main = str(product.get("image_url") or "").strip()
    if not draft.images and draft.source_ozon_url:
        try:
            progress("正在读取Ozon完整图库")
            cached_images = [
                str(value).strip() for value in (product.get("image_urls") or [])
                if str(value).strip().startswith("http")
            ]
            if cached_images:
                gallery = OzonGallery(
                    url=draft.source_ozon_url, title=str(product.get("title") or ""),
                    images=cached_images, source="imported-catalog",
                )
                progress(f"复用已采集的Ozon图库（{len(cached_images)}张），不重复打开商品页")
            else:
                try:
                    gallery = scrape_ozon_gallery(
                        draft.source_ozon_url, timeout=15, browser_fallback=False, on_progress=progress,
                    )
                except RuntimeError:
                    gallery = wait_for_extension_gallery(
                        draft.source_ozon_url, draft.source_sku, timeout=180, on_progress=progress,
                    )
            gallery_result = upload_captured_gallery_images(
                config=config,
                product=product,
                draft=draft,
                gallery=gallery,
                on_progress=progress,
            )
            draft.ai_preparation = {
                **draft.ai_preparation,
                "gallery_status": "READY",
                "gallery_source": gallery.source,
                "gallery_image_count": len(gallery.images),
            }
            progress(
                "已补全Ozon图库并转存OSS："
                f"主图参考1张，附图{gallery_result['additional_image_count']}张"
            )
        except Exception as exc:
            gallery_warning = str(exc)
            draft.ai_preparation = {
                **draft.ai_preparation,
                "gallery_warning": gallery_warning,
                "gallery_status": "FAILED",
            }
            if on_content_checkpoint:
                on_content_checkpoint()
            progress(f"Ozon附图抓取失败，已暂停后续生图：{gallery_warning}")
            raise RuntimeError(
                "Ozon附图抓取失败，未继续生成不完整商品；请检查正常Edge中的图库桥接扩展后重试"
            ) from exc
    reference_urls = []
    for raw_url in [product.get("image_url"), *draft.images]:
        url = str(raw_url or "").strip()
        if url.startswith("http") and url not in reference_urls:
            reference_urls.append(url)
    if not reference_urls:
        raise ValueError("商品没有可下载的Ozon参考图")
    progress(f"正在下载Ozon原商品参考图（共{min(6, len(reference_urls))}张）")
    references = [
        download_reference_image(url, work_dir, f"reference_{index}.jpg")
        for index, url in enumerate(reference_urls[:6], start=1)
    ]
    reference = references[0]
    vision = VisionAnalysisService(
        config.vision_api_url, config.vision_api_key, config.vision_model, models=config.vision_models
    )
    # Only the Ozon primary image is authoritative for SKU facts. Additional
    # gallery images remain available for optional gallery upload, but may be
    # another variant and must not drive generation or attribute inference.
    fact_references = references[:1]
    progress("视觉AI正在以Ozon主图为唯一事实依据分析商品")
    visual_analysis = vision.analyze(fact_references, product)
    product_master = build_product_master(reference, visual_analysis, work_dir)
    progress("AI正在生成俄文标题、描述和属性建议")
    ai_definitions: list[dict[str, Any]] = []
    for definition in definitions:
        enriched = dict(definition)
        if definition.get("dictionary_id") and not _is_brand_attribute(definition) and not _is_origin_country_attribute(definition):
            try:
                values = ozon_client.category_attribute_values(
                    draft.description_category_id, draft.type_id,
                    int(definition.get("id") or 0), language="RU",
                )
            except (OSError, RuntimeError):
                values = []
            enriched["_ai_allowed_value_count"] = len(values)
            # Always expose an exact, bounded slice of the cached dictionary to AI.
            # Large dictionaries stay local in full and are searched/validated after AI responds.
            visible_limit = 160 if definition.get("is_required") else 30
            enriched["_ai_allowed_values"] = [
                str(value.get("value") or value.get("name") or "").strip()
                for value in values[:visible_limit]
                if str(value.get("value") or value.get("name") or "").strip()
            ]
        ai_definitions.append(enriched)
    content = ListingContentService(ChatJsonClient(
        config.ai_api_url, config.ai_api_key, config.ai_model, models=config.ai_models
    )).generate(product, draft, ai_definitions, sourcing, visual_analysis)
    content["visual_analysis"] = visual_analysis
    poster_text_lines = [
        str(line).strip() for line in content.get("poster_text_lines_ru") or []
        if str(line).strip()
    ][:4]
    draft.title = content["title_ru"]
    draft.description_ru = content["description_ru"]
    draft.tags_ru = content["tags_ru"]
    progress("正在用Ozon实时字典校准属性")
    attribute_result = apply_ai_attributes(
        draft, definitions, content.get("attributes", []), ozon_client
    )
    content_attributes = apply_content_attributes(draft, definitions, content)
    attribute_result["applied"].extend(content_attributes)
    attribute_result["applied"].extend(enforce_system_attributes(draft, definitions))
    attribute_result["applied"].extend(enforce_known_fact_attributes(draft, definitions, product))
    original_images = list(dict.fromkeys(
        str(raw_url or "").strip() for raw_url in draft.images
        if str(raw_url or "").strip().startswith("https://")
    ))[:14]
    # Persist the expensive text/attribute result before image generation.
    # A transient image-provider failure can then resume from this checkpoint.
    draft.ai_preparation = {
        **draft.ai_preparation,
        "content": content,
        "attributes": attribute_result,
        "original_additional_image_urls": original_images,
        "reference_image_urls": reference_urls,
        "fact_reference_image_urls": reference_urls[:1],
        "gallery_warning": gallery_warning,
        "visual_analysis": visual_analysis,
        "product_master_path": str(product_master),
        "content_prepared_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "image_stage_status": "PENDING",
    }
    if on_content_checkpoint:
        on_content_checkpoint()
    if not generate_image:
        progress("AI文案和Ozon属性已保存，等待单独生成主图")
        return draft.ai_preparation
    image_service = ImageEditService(
        config.image_api_url, config.image_api_key, config.image_model
    )
    base_prompt = build_main_image_prompt(content)
    try:
        expected_unit_count = max(1, min(20, int(visual_analysis.get("unit_count") or 1)))
        if float(visual_analysis.get("unit_count_confidence") or 0) < 0.80:
            expected_unit_count = 1
    except (TypeError, ValueError):
        expected_unit_count = 1
    living_subject = (
        visual_analysis.get("living_subject")
        if isinstance(visual_analysis.get("living_subject"), dict) else {}
    )
    generation_attempts: list[dict[str, Any]] = []
    progress("AI正在生成3:4俄文营销主图（生成后只校验一次；不合格将转人工核验）")
    generated = image_service.generate(product_master, base_prompt[:MAX_IMAGE_PROMPT_CHARS], work_dir)
    # Main-image verification is intentionally manual for Ozon follow listings.
    # Do not call the vision verifier here: an unavailable verifier must not
    # block a generated candidate from reaching the human review queue.
    image_verification = {
        "status": "MANUAL_REVIEW_REQUIRED",
        "matches_sku": None,
        "text_matches": None,
        "confidence": None,
        "reason_ru": "已跳过AI主图核验，等待人工核验",
    }
    generation_attempts.append({
        "attempt": 1,
        "path": str(generated),
        "verification": image_verification,
    })
    result = {
        "content": content,
        "attributes": attribute_result,
        "generated_image_path": str(generated),
        "generated_image_url": "",
        "uploaded_additional_image_urls": [],
        "original_additional_image_urls": original_images,
        "reference_image_urls": reference_urls,
        "fact_reference_image_urls": reference_urls[:1],
        "gallery_warning": gallery_warning,
        "visual_analysis": visual_analysis,
        "product_master_path": str(product_master),
        "image_verification": image_verification,
        "generation_attempts": generation_attempts,
        "image_review_status": "PENDING",
        "image_review_source": "",
        "image_review_requested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "content_prepared_at": draft.ai_preparation.get("content_prepared_at"),
        "image_stage_status": "DONE",
    }
    draft.ai_preparation = result
    progress("主图已生成，已直接进入人工核验")
    return result


def prepare_main_image_from_checkpoint(
    *,
    config: AppConfig,
    draft: OzonListingDraft,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Generate only the main image using saved content/attribute work."""
    progress = on_progress or (lambda _message: None)
    preparation = draft.ai_preparation
    content = preparation.get("content")
    visual_analysis = preparation.get("visual_analysis")
    reference_urls = preparation.get("reference_image_urls")
    if not isinstance(content, dict) or not isinstance(visual_analysis, dict) or not isinstance(reference_urls, list):
        raise ValueError("缺少已保存的AI文案属性检查点")
    work_dir = DATA_DIR / "generated" / draft.source_sku
    progress("正在复用已保存的AI文案、属性和视觉分析")
    references = [
        download_reference_image(str(url), work_dir, f"reference_{index}.jpg")
        for index, url in enumerate(reference_urls[:6], start=1)
        if str(url).strip().startswith("http")
    ]
    if not references:
        raise ValueError("已保存的AI检查点没有可用参考图")
    poster_text_lines = [
        str(line).strip() for line in content.get("poster_text_lines_ru") or []
        if str(line).strip()
    ][:4]
    try:
        expected_unit_count = max(1, min(20, int(visual_analysis.get("unit_count") or 1)))
        if float(visual_analysis.get("unit_count_confidence") or 0) < 0.80:
            expected_unit_count = 1
    except (TypeError, ValueError):
        expected_unit_count = 1
    living_subject = visual_analysis.get("living_subject") if isinstance(visual_analysis.get("living_subject"), dict) else {}
    progress("AI正在单独重试生成3:4俄文营销主图")
    product_master = Path(str(preparation.get("product_master_path") or ""))
    if not product_master.is_file():
        product_master = build_product_master(references[0], visual_analysis, work_dir)
        preparation["product_master_path"] = str(product_master)
    generated = ImageEditService(
        config.image_api_url, config.image_api_key, config.image_model
    ).generate(str(product_master), build_main_image_prompt(content)[:MAX_IMAGE_PROMPT_CHARS], work_dir)
    verification = {
        "status": "MANUAL_REVIEW_REQUIRED",
        "matches_sku": None,
        "text_matches": None,
        "confidence": None,
        "reason_ru": "已跳过AI主图核验，等待人工核验",
    }
    attempts = list(preparation.get("generation_attempts") or [])
    attempts.append({"attempt": len(attempts) + 1, "path": str(generated), "verification": verification})
    preparation.update({
        "generated_image_path": str(generated),
        "generated_image_url": "",
        "image_verification": verification,
        "generation_attempts": attempts,
        "image_review_status": "PENDING",
        "image_review_source": "",
        "image_review_requested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "image_stage_status": "DONE",
    })
    progress("重新生成主图已直接进入人工核验")
    return preparation


def pending_main_image_path(draft: OzonListingDraft) -> Path:
    """Return the persisted review candidate, restricted to this SKU's generated directory."""
    raw_path = str(draft.ai_preparation.get("generated_image_path") or "").strip()
    if not raw_path:
        raise ValueError("该商品没有待核验主图")
    candidate = Path(raw_path).resolve()
    allowed = (DATA_DIR / "generated" / draft.source_sku).resolve()
    if candidate.parent != allowed or not candidate.is_file():
        raise ValueError("待核验主图文件不存在或路径无效")
    return candidate


def approve_pending_main_image(
    *,
    config: AppConfig,
    draft: OzonListingDraft,
    on_progress: Callable[[str], None] | None = None,
    reviewer: str = "HUMAN",
) -> dict[str, Any]:
    """Approve a generated candidate and complete the deferred OSS image upload."""
    progress = on_progress or (lambda _message: None)
    generated = pending_main_image_path(draft)
    preparation = draft.ai_preparation
    # The gallery stage normally uploads additional images before AI work.
    # Treat the current draft images as authoritative so approving/retrying the
    # generated main image does not download and upload those OSS objects again.
    additional_images = list(dict.fromkeys(
        str(url or "").strip()
        for url in (
            draft.images
            or preparation.get("original_additional_image_urls")
            or []
        )
        if str(url or "").strip().startswith("https://")
    ))[:14]
    uploader = OssUploader(config)
    progress("正在按月/日/产品编号上传已核验主图到OSS")
    public_url = str(preparation.get('codex_preserve_primary_url') or '') or uploader.upload(generated, _oss_upload_scope(draft), "主图.jpg")
    work_dir = (DATA_DIR / "generated" / draft.source_sku).resolve()
    oss_base = str(
        config.oss_public_base_url
        or f"https://{config.oss_bucket}.{config.oss_endpoint}"
    ).rstrip("/")
    uploaded_additional: list[str] = []
    for index, image_url in enumerate(additional_images, start=1):
        if image_url.startswith(oss_base + "/"):
            uploaded_additional.append(image_url)
            continue
        progress(f"正在转存Ozon原附图到OSS（{index}/{len(additional_images)}）")
        local_path = download_reference_image(
            image_url, work_dir, f"ozon_additional_{index:02d}.jpg"
        )
        uploaded_additional.append(
            uploader.upload(local_path, _oss_upload_scope(draft), f"附图{index:02d}.jpg")
        )
    draft.primary_image = public_url
    draft.images = uploaded_additional
    draft.images_oss_ready = bool(
        public_url.startswith(oss_base + "/")
        and all(url.startswith(oss_base + "/") for url in uploaded_additional)
    )
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    preparation.update({
        "generated_image_url": public_url,
        "uploaded_additional_image_urls": uploaded_additional,
        "image_review_status": "APPROVED",
        "image_review_source": reviewer,
        "image_reviewed_at": now,
        "prepared_at": now,
    })
    return preparation


def regenerate_pending_main_image(
    *,
    config: AppConfig,
    draft: OzonListingDraft,
    mode: str = "redesign",
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Generate one replacement candidate and leave any failure for human review."""
    progress = on_progress or (lambda _message: None)
    preparation = draft.ai_preparation
    content = preparation.get("content")
    visual = preparation.get("visual_analysis")
    reference_urls = preparation.get("reference_image_urls")
    if not isinstance(content, dict) or not isinstance(visual, dict) or not isinstance(reference_urls, list):
        raise ValueError("缺少上次AI准备数据，请重新执行AI生成内容和主图")
    work_dir = DATA_DIR / "generated" / draft.source_sku
    urls = [str(url).strip() for url in reference_urls[:6] if str(url).strip().startswith("http")]
    if not urls:
        raise ValueError("缺少Ozon参考图，无法重新生成主图")
    progress(f"正在读取Ozon原商品参考图（共{len(urls)}张）")
    references = [
        download_reference_image(url, work_dir, f"reference_{index}.jpg")
        for index, url in enumerate(urls, start=1)
    ]
    if not isinstance(visual.get("product_bbox_norm"), list) or not isinstance(visual.get("source_design_to_avoid"), dict):
        progress("正在为旧候选图补充商品主体区域和原海报排除分析")
        refreshed_visual = VisionAnalysisService(
            config.vision_api_url,
            config.vision_api_key,
            config.vision_model,
            models=config.vision_models,
        ).analyze(references[:1], {"title": draft.title, "offer_id": draft.offer_id})
        visual.update(refreshed_visual)
        preparation["visual_analysis"] = visual
        content["visual_analysis"] = visual
    try:
        expected_unit_count = max(1, min(20, int(visual.get("unit_count") or 1)))
        if float(visual.get("unit_count_confidence") or 0) < 0.80:
            expected_unit_count = 1
    except (TypeError, ValueError):
        expected_unit_count = 1
    living_subject = visual.get("living_subject") if isinstance(visual.get("living_subject"), dict) else {}
    text_lines = [
        str(line).strip() for line in content.get("poster_text_lines_ru") or []
        if str(line).strip()
    ][:4]
    previous_reason = re.sub(
        r"\s+", " ", str(preparation.get("image_verification", {}).get("reason_ru") or "")
    ).strip()
    correction = (
        "\n\nMANDATORY CORRECTION FROM THE PREVIOUS REVIEW:\n"
        f"The previous candidate failed because: {previous_reason[:500]}\n"
        f"Show exactly {expected_unit_count} complete unit(s). Restore every changed color, shape, component, "
        "pattern and accessory. Render every Russian whitelist line exactly once and remove all other text."
        if previous_reason else ""
    )
    attempts = preparation.setdefault("generation_attempts", [])
    attempt_no = len(attempts) + 1
    creative_directions = (
        "a bright lived-in morning scene near a window, airy neutral palette, product in natural use",
        "a warm evening home scene with soft practical lighting, deep clean colors and an intimate viewpoint",
        "a fresh everyday travel or getting-ready scene, dynamic three-quarter camera angle and clear depth",
        "a tidy modern bathroom or dressing-area scene, cool daylight palette and asymmetrical editorial layout",
        "a comfortable bedroom or wardrobe scene, tactile fabrics, warm daylight and a human-scale viewpoint",
        "a clean real-life tabletop scene with contextual objects, bold color contrast and diagonal composition",
    )
    direction = creative_directions[(attempt_no - 1) % len(creative_directions)]
    mode = str(mode or "redesign").strip().casefold()
    if mode not in {"repair", "redesign", "full_redesign"}:
        raise ValueError("不支持的重新生图方式")
    mode_rule = {
        "repair": "Keep the current candidate art direction and only repair factual, cleanliness, anatomy or typography problems.",
        "redesign": "Replace the scene, camera angle, placement, palette, lighting, negative space, typography hierarchy and labels.",
        "full_redesign": "Ignore every advertising-design choice from the source and previous candidate; start a new campaign from product identity only.",
    }[mode]
    redesign = (
        "\n\nMANDATORY VISUAL REDESIGN FOR THIS REGENERATION:\n"
        f"This is regeneration attempt {attempt_no}. Use {direction}. "
        f"{mode_rule} "
        "Do not reuse the previous beige studio template, centered catalog composition, identical rounded text pills, or the same props. "
        "The setting must feel clean, believable and lived-in, never dirty or cluttered. "
        "Keep the exact product identity, construction, color, pattern, accessories and unit count unchanged."
    )
    progress(f"AI正在按新场景与新版式重新生成3:4俄文营销主图（第 {attempt_no} 版）")
    previous_candidate = Path(str(preparation.get("generated_image_path") or ""))
    product_master = Path(str(preparation.get("product_master_path") or ""))
    if not product_master.is_file():
        product_master = build_product_master(references[0], visual, work_dir)
        preparation["product_master_path"] = str(product_master)
    generation_reference = previous_candidate if mode == "repair" and previous_candidate.is_file() else product_master
    image_service = ImageEditService(
        config.image_api_url, config.image_api_key, config.image_model
    )
    prompt = (redesign + correction + "\n\n" + build_main_image_prompt(content))[:MAX_IMAGE_PROMPT_CHARS]
    generated = image_service.generate(str(generation_reference), prompt, work_dir)
    similarity_score = None
    if mode != "repair" and previous_candidate.is_file():
        similarity_score = image_difference_score(previous_candidate, generated)
        if similarity_score < 0.085:
            progress("新候选图与上一张过于相似，正在自动换构图补生一次")
            stronger = (
                "MANDATORY ANTI-DUPLICATION RETRY: use the opposite camera elevation and side placement, a new room type, "
                "contrasting palette, different prop category, and typography on the opposite side. Preserve only product identity.\n\n"
            )
            generated = image_service.generate(str(product_master), (stronger + prompt)[:MAX_IMAGE_PROMPT_CHARS], work_dir)
            similarity_score = image_difference_score(previous_candidate, generated)
    verification = {
        "status": "MANUAL_REVIEW_REQUIRED",
        "matches_sku": None,
        "text_matches": None,
        "confidence": None,
        "reason_ru": "已跳过AI主图核验，等待人工核验",
    }
    attempts.append({
        "attempt": attempt_no,
        "path": str(generated),
        "verification": verification,
        "creative_direction": direction,
        "regeneration_mode": mode,
        "difference_score_from_previous": similarity_score,
    })
    preparation.update({
        "generated_image_path": str(generated),
        "generated_image_url": "",
        "image_verification": verification,
        "image_review_status": "PENDING",
        "image_review_source": "",
        "image_review_requested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "image_reviewed_at": "",
        "prepared_at": "",
    })
    draft.primary_image = ""
    draft.images_oss_ready = False
    progress("重新生成主图已直接进入人工核验")
    return preparation


def apply_content_attributes(
    draft: OzonListingDraft,
    definitions: list[dict[str, Any]],
    content: dict[str, Any],
) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    for definition in definitions:
        if int(definition.get("id") or 0) == RICH_CONTENT_ATTRIBUTE_ID:
            continue
        names = [
            _normalize_text(str(definition.get(key) or ""))
            for key in ("name", "name_ru")
        ]
        value = ""
        kind = ""
        if any(name in {"名称", "название"} or any(
            token in name for token in ("названиетовара", "наименованиетовара", "商品名称", "商品标题")
        ) for name in names):
            value, kind = content.get("title_ru", ""), "AI俄文标题"
        elif any(any(token in name for token in (
            "описаниетовара", "описание", "商品描述", "产品描述", "简介", "аннотация"
        )) for name in names):
            value, kind = content.get("description_ru", ""), "AI俄文描述"
        elif any(any(token in name for token in ("хештег", "主题标签")) for name in names):
            tags = content.get("tags_ru") or []
            value, kind = format_hashtags(tags), "AI主题标签"
        if not value:
            continue
        suggestion = {"confidence": 1.0, "evidence": kind}
        _set_attribute(draft, definition, str(value), 0, suggestion)
        applied.append({
            "attribute_id": int(definition["id"]),
            "name": definition.get("name"),
            "value": str(value),
            "dictionary_value_id": 0,
            "confidence": 1.0,
            "evidence": kind,
        })
    return applied


def _unique_dictionary_match(query: str, options: list[dict[str, Any]]) -> dict[str, Any] | None:
    valid = [
        item for item in options
        if item.get("id") and str(item.get("value") or item.get("name") or "").strip()
    ]
    # The Ozon search endpoint is already query-scoped. With a localized API
    # response, one Russian query can legitimately return one Chinese label.
    if len(valid) == 1:
        return valid[0]
    normalized_query = _normalize_text(query)
    exact = [item for item in valid if _normalize_text(item.get("value") or item.get("name")) == normalized_query]
    if len(exact) == 1:
        return exact[0]
    contains = [
        item for item in valid
        if normalized_query and normalized_query in _normalize_text(item.get("value") or item.get("name"))
    ]
    return contains[0] if len(contains) == 1 else None


def _dictionary_search_queries(value: str) -> list[str]:
    """Try the exact phrase, then conservative word stems for Russian inflections."""
    phrase = re.sub(r"\s+", " ", str(value or "")).strip()
    result = [phrase] if len(phrase) >= 2 else []
    words = [word for word in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", phrase) if len(word) >= 3]
    for word in sorted(words, key=len, reverse=True):
        for length in range(len(word), max(2, len(word) - 4), -1):
            query = word[:length]
            if query not in result:
                result.append(query)
            if len(result) >= 10:
                return result
    return result


def _normalize_text(value: Any) -> str:
    return re.sub(r"[^a-zа-я0-9\u3400-\u9fff]+", "", str(value or "").casefold().replace("ё", "е"))


def _string_list(value: Any, maximum: int) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,;\n]+", value)
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        if text and text.casefold() not in {existing.casefold() for existing in result}:
            result.append(text)
        if len(result) >= maximum:
            break
    return result


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _image_data_url(path: Path, maximum: int = 1400) -> str:
    with Image.open(path) as opened:
        image = opened.convert("RGB")
        image.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=84, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
