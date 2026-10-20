from __future__ import annotations
from erp.services.publication_options import pricing_profile, publication_images

import hashlib
import itertools
import re
import shutil
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from erp.core.config import get_settings
from erp.core.errors import AppError, ConflictError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.core.security import vault
from erp.integrations.ozon import OzonImportUncertain, OzonSellerAdapter, import_failed, import_items, import_succeeded
from erp.models import (
    Asset,
    CatalogProduct,
    CatalogProductAsset,
    CatalogVariant,
    IntegrationConnection,
    Publication,
    PublicationStore,
    Store,
    SystemSetting,
)
from erp.modules.ozon_follow.legacy.config import AppConfig
from erp.modules.ozon_follow.legacy.services.ai_listing import (
    ImageEditService,
    OssUploader,
    VisionAnalysisService,
    apply_ai_attributes,
    approve_pending_main_image,
    download_reference_image,
    enforce_known_fact_attributes,
    prepare_main_image_from_checkpoint,
    prepare_listing_with_ai,
    regenerate_pending_main_image,
    store_creative_profile,
)
from erp.modules.ozon_follow.legacy.paths import DATA_DIR as OZON_WORK_DATA_DIR
from erp.modules.ozon_follow.legacy.services.ozon_client import OzonSellerClient
from erp.modules.ozon_follow.legacy.storage import ProductRepository
from erp.modules.ozon_follow.legacy.services.ozon_listing import OzonListingDraft, build_ozon_import_item, validate_listing_draft
from erp.modules.ozon_follow.legacy.services.pricing import calculate_actual_sale_for_target_cost_margin
from erp.services.catalog import get_catalog_product, link_catalog_asset
from erp.services.config_resolution import build_job_snapshot, resolve_job_snapshot
from erp.services.jobs import JobActionRequired, JobContext, JobPaused, JobCancelled, job_handler


PUBLICATION_STEPS = [
    ("validate", "校验产品精确资料"),
    ("ai_content", "生成店铺独立俄文文案"),
    ("ai_images", "按策略处理图片"),
    ("attributes", "读取并校验 OZON 实时属性"),
    ("oss_upload", "确认发布图片可访问"),
    ("submit", "提交 OZON 商品"),
    ("stock", "发布库存"),
    ("promotion", "加入促销"),
]


def publication_query():
    return select(Publication).options(
        selectinload(Publication.product).selectinload(CatalogProduct.variants),
        selectinload(Publication.product).selectinload(CatalogProduct.asset_links).selectinload(CatalogProductAsset.asset),
        selectinload(Publication.variant),
        selectinload(Publication.stores).selectinload(PublicationStore.store),
    )


def publication_status(stores: list[PublicationStore]) -> str:
    states={row.status for row in stores}
    if states and states <= {"completed", "ready_for_export"}: return "completed"
    if states == {"ready_for_submit"}: return "ready_for_submit"
    if states == {"stock_pending"}: return "stock_pending"
    if "promotion_pending" in states and states <= {"promotion_pending", "completed"}: return "promotion_pending"
    completed=sum(1 for row in stores if row.status in {"completed", "ready_for_export"})
    if any(row.status in {"stock_pending", "promotion_pending", "completed", "ready_for_export"} for row in stores): return "in_progress"
    return "partial" if completed else "blocked"


def get_publication(session: Session, publication_id: str) -> Publication:
    item = session.scalar(publication_query().where(Publication.id == publication_id))
    if not item: raise NotFoundError("发布记录不存在")
    return item


def _connection_snapshot(session: Session, provider: str, name: str = "") -> dict[str, Any]:
    query = select(IntegrationConnection).where(IntegrationConnection.provider == provider, IntegrationConnection.enabled.is_(True))
    if name: query = query.where(IntegrationConnection.name == name)
    item = session.scalar(query.order_by(IntegrationConnection.created_at))
    if not item: return {}
    return {"id":item.id,"base_url":item.base_url,"config":loads(item.config_json,{}),"secrets":vault.decrypt(item.secrets_encrypted) if item.secrets_encrypted else {}}


def build_publication_snapshot(session: Session, store_id: str, overrides: dict[str, Any]) -> str:
    base = resolve_job_snapshot(build_job_snapshot(session, store_id=store_id, overrides=overrides))
    pricing_setting = session.get(SystemSetting, "pricing")
    payload = {
        **base,
        "pricing_profile": loads(pricing_setting.value_json, {}) if pricing_setting else {},
        "services": {
            "ai_chat": _connection_snapshot(session, "ai_chat"),
            "ai_image": _connection_snapshot(session, "ai_image"),
            "oss": _connection_snapshot(session, "oss", "OZON 自动上架 OSS") or _connection_snapshot(session, "oss"),
        },
    }
    secrets = payload.pop("secrets", {})
    service_secrets = {name: service.pop("secrets", {}) for name, service in payload["services"].items()}
    return dumps({"public":payload,"secrets_encrypted":vault.encrypt({"store":secrets,"services":service_secrets})})


def resolve_publication_snapshot(value: str) -> dict[str, Any]:
    raw = loads(value, {}); public = dict(raw.get("public") or {})
    secrets = vault.decrypt(str(raw.get("secrets_encrypted") or "")) if raw.get("secrets_encrypted") else {}
    public["secrets"] = secrets.get("store") or {}
    for name, service in (public.get("services") or {}).items():
        service["secrets"] = (secrets.get("services") or {}).get(name) or {}
    return public


def product_snapshot(product: CatalogProduct, variant: CatalogVariant | None) -> dict[str, Any]:
    if product.source_type == "pod_amazon":
        raise ConflictError("Amazon 美国 POD 产品不能混用 OZON 上架与人民币定价")
    length_mm=variant.package_length_mm if variant and variant.package_length_mm else product.package_length_mm
    width_mm=variant.package_width_mm if variant and variant.package_width_mm else product.package_width_mm
    height_mm=variant.package_height_mm if variant and variant.package_height_mm else product.package_height_mm
    dimensions_source="product_library"
    if product.source_type == "follow":
        # Ozon follow products intentionally use one operational virtual package
        # for publication. Do not write these values back as measured catalog facts.
        length_mm,width_mm,height_mm=300.0,200.0,100.0
        dimensions_source="virtual_follow_30x20x10cm"
    return {
        "product_id":product.id,"variant_id":variant.id if variant else None,"source_type":product.source_type,
        "group_code":product.group_code,"sku":variant.sku if variant else product.sku,"name":product.name,
        "category":product.category,"source_url":product.source_url,"supplier":product.supplier,
        "supplier_product_id":variant.supplier_product_id if variant and variant.supplier_product_id else product.supplier_product_id,
        "supplier_sku_id":variant.supplier_sku_id if variant else "","specification":variant.specification if variant else "",
        "color":variant.color if variant else "","size":variant.size if variant else "",
        "purchase_price_cny":variant.purchase_price_cny if variant and variant.purchase_price_cny is not None else product.purchase_price_cny,
        "domestic_delivery_cny":variant.domestic_delivery_cny if variant and variant.domestic_delivery_cny is not None else product.domestic_delivery_cny,
        "currency":product.currency,
        "package_length_mm":length_mm,"package_width_mm":width_mm,"package_height_mm":height_mm,
        "package_dimensions_source":dimensions_source,
        "package_weight_g":variant.weight_grams if variant and variant.weight_grams else product.package_weight_g,
        "description_category_id":product.description_category_id,"type_id":product.type_id,"category_display":product.category_display,
        "notes":product.notes,"production":loads(product.production_json,{}),"source_snapshot":loads(product.source_snapshot_json,{}),
        "assets":[{"asset_id":link.asset_id,"name":link.asset.name or link.asset.oss_key,"role":link.role,"position":link.position,"local_path":link.asset.local_path,"oss_url":link.asset.oss_url,"sha256":link.asset.sha256} for link in sorted(product.asset_links,key=lambda row:(row.role!="main",row.position)) if link.is_current and (not link.variant_id or not variant or link.variant_id==variant.id)],
    }


def _price(snapshot: dict[str, Any], facts: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]:
    requested = requirements.get("price")
    if requested is not None:
        price = Decimal(str(requested)); old = Decimal(str(requirements.get("old_price") or price * Decimal("1.25")))
        return {"price":str(price),"old_price":str(old),"action_price":requirements.get("action_price"),"currency":"CNY","source":"manual_requirement"}
    purchase = facts.get("purchase_price_cny")
    if purchase is None: raise AppError("缺少精确拿货价，无法定价",code="publication_price_missing")
    profile=pricing_profile(snapshot,requirements)
    result=calculate_actual_sale_for_target_cost_margin(target_cost_profit_margin=profile.get("target_cost_profit_margin",30),purchase_price=purchase,length_mm=facts.get("package_length_mm"),width_mm=facts.get("package_width_mm"),height_mm=facts.get("package_height_mm"),weight_g=facts.get("package_weight_g"),category_module=profile.get("pricing_category_module",""),product_category=profile.get("pricing_product_category",""),sticker_fee=profile.get("pricing_sticker_fee",2.5),advertising_cost=profile.get("pricing_operating_cost",3),advertising_rate=profile.get("pricing_advertising_rate",0),return_rate=profile.get("pricing_return_rate",10),logistics_service_rate=profile.get("pricing_logistics_service_rate",2),price_multiplier=profile.get("pricing_price_multiplier",3),old_price_multiplier=profile.get("pricing_old_price_multiplier",3.7))
    return {"price":str(result.ozon_price),"old_price":str(result.ozon_old_price),"action_price":str(result.actual_sale),"currency":"CNY","source":"batch_pricing" if requirements.get("pricing_overrides") else "configured_pricing","calculation":{key:str(value) for key,value in asdict(result).items()}}


def validate_product_facts(facts: dict[str, Any], requirements: dict[str, Any]) -> list[str]:
    missing=[]
    if not facts.get("name"): missing.append("中文名称")
    if not facts.get("sku"): missing.append("SKU")
    if facts.get("purchase_price_cny") is None and requirements.get("price") is None: missing.append("拿货价或本次指定售价")
    for key,label in (("package_length_mm","包装长度"),("package_width_mm","包装宽度"),("package_height_mm","包装高度"),("package_weight_g","包装重量")):
        if float(facts.get(key) or 0)<=0: missing.append(label)
    if not int(requirements.get("description_category_id") or facts.get("description_category_id") or 0): missing.append("OZON 实时类目")
    if not int(requirements.get("type_id") or facts.get("type_id") or 0): missing.append("OZON 商品类型")
    images=publication_images(facts,requirements)
    if not images: missing.append("产品库当前图片的 HTTPS OSS 链接")
    return missing


def _normalized_attribute_text(value: Any) -> str:
    return re.sub(r"[^0-9a-zа-яё\u4e00-\u9fff]+", "", str(value or "").casefold())


def _dictionary_value(value: dict[str, Any]) -> tuple[int, str]:
    return int(value.get("id") or value.get("value_id") or 0), str(value.get("value") or value.get("name") or "").strip()


def _is_color_variant(definition: dict[str, Any]) -> bool:
    name = str(definition.get("name") or "").casefold()
    return int(definition.get("id") or 0) == 10096 or "颜色" in name or "цвет" in name


def _is_size_variant(definition: dict[str, Any]) -> bool:
    name = str(definition.get("name") or "").casefold()
    return any(token in name for token in ("尺寸", "尺码", "размер", "габарит"))


def _factual_variant_value(facts: dict[str, Any], definition: dict[str, Any]) -> Any:
    if _is_color_variant(definition):
        return facts.get("color")
    if _is_size_variant(definition):
        return facts.get("size")
    # Other Ozon aspect fields may be selected as a last-resort virtual axis,
    # but must never borrow or invent a product fact under a misleading name.
    return ""


def build_ozon_merge_plan(facts_list: list[dict[str, Any]], definitions: list[dict[str, Any]], dictionary_values: dict[int, list[dict[str, Any]]], *, model_name: str, variant_attribute_ids: list[int]) -> dict[str, dict[str, Any]]:
    """Build deterministic values; only the preferred virtual axis may be fabricated."""
    by_id = {int(item.get("id") or 0): item for item in definitions}
    selected = [by_id[value] for value in variant_attribute_ids if value in by_id]
    if len(selected) != len(set(variant_attribute_ids)):
        raise AppError("选择的变体特征不属于当前 Ozon 类目", code="ozon_merge_attribute_invalid")
    ordinary_attributes = [str(item.get("name") or item.get("id")) for item in selected if item.get("is_aspect") is not True]
    if ordinary_attributes:
        raise AppError("只能选择 Ozon 模板标记的变体特征，不能使用普通商品属性："+"、".join(ordinary_attributes), code="ozon_merge_attribute_not_aspect")
    empty_dictionaries = [str(item.get("name") or item.get("id")) for item in selected if not dictionary_values.get(int(item["id"]))]
    if empty_dictionaries:
        raise AppError("以下变体特征没有可用的 Ozon 字典值："+"、".join(empty_dictionaries), code="ozon_merge_dictionary_empty")
    # Color is the only preferred virtual attribute. Another field becomes virtual
    # only for categories that do not expose color as a selected variant aspect.
    virtual_index = next((index for index,item in enumerate(selected) if _is_color_variant(item)), 0)
    plan: dict[str, dict[str, Any]] = {}; used: set[tuple[tuple[int, tuple[int, ...]], ...]] = set()
    for index, facts in enumerate(facts_list):
        assigned=[]; signature_parts=[]
        for definition in selected:
            attribute_id=int(definition["id"]); options=[pair for pair in (_dictionary_value(row) for row in dictionary_values.get(attribute_id,[])) if pair[0] and pair[1]]
            name=str(definition.get("name") or "")
            factual=_factual_variant_value(facts,definition)
            tokens=[part.strip() for part in re.split(r"[;,，、/]+",str(factual or "")) if part.strip()]; matched=[]
            for token in tokens:
                normalized=_normalized_attribute_text(token)
                candidate=next((item for item in options if normalized and (normalized==_normalized_attribute_text(item[1]) or normalized in _normalized_attribute_text(item[1]) or _normalized_attribute_text(item[1]) in normalized)),None)
                if candidate and candidate not in matched: matched.append(candidate)
            maximum=max(1,int(definition.get("max_value_count") or (2 if definition.get("is_collection") else 1)));matched=matched[:maximum]
            if not matched and options and len(assigned)==virtual_index:
                matched=[options[index%len(options)]]
            source="catalog" if tokens and matched else "ozon_fallback" if matched else "missing_real_value"
            assigned.append({"attribute_id":attribute_id,"name":name,"values":[{"dictionary_value_id":item[0],"value":item[1]} for item in matched],"source":source})
            signature_parts.append((attribute_id,tuple(item[0] for item in matched)))
        signature=tuple(signature_parts)
        if signature in used and selected:
            target=selected[virtual_index];attribute_id=int(target["id"]);options=[pair for pair in (_dictionary_value(row) for row in dictionary_values.get(attribute_id,[])) if pair[0] and pair[1]]
            maximum=max(1,int(target.get("max_value_count") or (2 if target.get("is_collection") else 1)));real_ids=set(signature_parts[virtual_index][1]);candidates=[]
            if maximum>1:
                # Keep the product's real color in the combination whenever a second value is needed for uniqueness.
                candidates.extend([list(items) for items in itertools.combinations(options,2) if real_ids.intersection(item[0] for item in items)])
            candidates.extend([[item] for item in options])
            if maximum>1:candidates.extend([list(items) for items in itertools.combinations(options,2) if not real_ids.intersection(item[0] for item in items)])
            for candidate in candidates:
                revised=list(signature_parts);revised[virtual_index]=(attribute_id,tuple(item[0] for item in candidate))
                if tuple(revised) not in used:
                    assigned[virtual_index]["values"]=[{"dictionary_value_id":item[0],"value":item[1]} for item in candidate];assigned[virtual_index]["source"]="ozon_unique_fallback";signature=tuple(revised);break
        if signature in used: raise AppError("所选变体特征的 Ozon 字典组合不足，无法让合并组内商品保持唯一",code="ozon_merge_values_exhausted")
        used.add(signature);plan[str(facts["sku"])]={"mode":"custom_group","model_name":model_name.strip(),"attributes":assigned}
    return plan


def apply_ozon_merge_plan(draft: OzonListingDraft, definitions: list[dict[str, Any]], merge_plan: dict[str, Any]) -> None:
    if merge_plan.get("mode")!="custom_group": return
    model_definition=next((item for item in definitions if any(token in str(item.get("name") or "").casefold() for token in ("型号名称","название модели","model name"))),None)
    if not model_definition: raise AppError("当前 Ozon 类目未返回“型号名称”属性，无法创建合并组",code="ozon_merge_model_attribute_missing")
    overrides=[{"definition":model_definition,"values":[{"value":str(merge_plan.get("model_name") or "")}]}];by_id={int(item.get("id") or 0):item for item in definitions}
    overrides.extend({"definition":by_id[int(item["attribute_id"])],"values":item.get("values") or []} for item in merge_plan.get("attributes") or [] if int(item.get("attribute_id") or 0) in by_id)
    all_items=list(draft.attributes)
    for group in draft.complex_attributes: all_items.extend(group.get("attributes") or [])
    for override in overrides:
        definition=override["definition"];attribute_id=int(definition["id"]);complex_id=int(definition.get("attribute_complex_id") or 0);target=next((item for item in all_items if int(item.get("id") or 0)==attribute_id),None)
        if target is None:
            target={"complex_id":complex_id,"id":attribute_id,"values":[]}
            if complex_id:
                group=next((item for item in draft.complex_attributes if int(item.get("_complex_id") or 0)==complex_id),None)
                if group is None: group={"attributes":[],"_complex_id":complex_id};draft.complex_attributes.append(group)
                group["attributes"].append(target)
            else: draft.attributes.append(target)
            all_items.append(target)
        target["values"]=list(override["values"]);target["_name"]=str(definition.get("name") or attribute_id);target["_source"]="ERP_MERGE_PLAN"


def normalize_ozon_dictionary_attributes(draft: OzonListingDraft, definitions: list[dict[str, Any]], client: OzonSellerClient) -> list[str]:
    """Normalize every dictionary field against cached Ozon values before import."""
    by_id={int(item.get("id") or 0):item for item in definitions};all_items=list(draft.attributes)
    for group in draft.complex_attributes:all_items.extend(group.get("attributes") or [])
    removed=[]
    for item in all_items:
        attribute_id=int(item.get("id") or 0);definition=by_id.get(attribute_id)
        if not definition or not definition.get("dictionary_id") or not item.get("values"):continue
        allowed=client.category_attribute_values(draft.description_category_id,draft.type_id,attribute_id,language="RU")
        allowed_by_id={int(value.get("id") or 0):value for value in allowed if value.get("id")}
        allowed_by_text={_normalized_attribute_text(value.get("value") or value.get("name")):value for value in allowed if _normalized_attribute_text(value.get("value") or value.get("name"))}
        valid=[]
        for raw in item.get("values") or []:
            match=allowed_by_id.get(int(raw.get("dictionary_value_id") or 0)) or allowed_by_text.get(_normalized_attribute_text(raw.get("value")))
            if not match:continue
            valid.append({"dictionary_value_id":int(match["id"]),"value":str(match.get("value") or match.get("name") or "")})
        if not valid:removed.append(str(definition.get("name") or attribute_id))
        item["values"]=valid
    return removed


def _app_config(snapshot: dict[str, Any]) -> AppConfig:
    services=snapshot.get("services") or {}; chat=services.get("ai_chat") or {}; image=services.get("ai_image") or {}; oss=services.get("oss") or {}
    chat_cfg=chat.get("config") or {}; image_cfg=image.get("config") or {}; oss_cfg=oss.get("config") or {}
    return AppConfig(ozon_client_id=str(snapshot.get("external_account_id") or snapshot.get("secrets",{}).get("client_id") or ""),ozon_api_key=str(snapshot.get("secrets",{}).get("api_key") or ""),ozon_api_base=str(snapshot.get("api_base") or "https://api-seller.ozon.ru"),ai_api_url=str(chat.get("base_url") or ""),ai_api_key=str(chat.get("secrets",{}).get("api_key") or ""),ai_model=str(chat_cfg.get("default_model") or "gpt-5.6-terra"),vision_api_url=str(chat.get("base_url") or ""),vision_api_key=str(chat.get("secrets",{}).get("api_key") or ""),vision_model=str(chat_cfg.get("default_model") or "gpt-5.6-terra"),image_api_url=str(image.get("base_url") or ""),image_api_key=str(image.get("secrets",{}).get("api_key") or ""),image_model=str(image_cfg.get("default_model") or "gpt-image-2"),oss_region=str(oss_cfg.get("region") or ""),oss_endpoint=str(oss.get("base_url") or oss_cfg.get("endpoint") or ""),oss_bucket=str(oss_cfg.get("bucket") or ""),oss_public_base_url=str(oss_cfg.get("public_base_url") or ""),oss_object_prefix=str(oss_cfg.get("prefix") or "ozon-publications"),oss_access_key_id=str(oss.get("secrets",{}).get("access_key_id") or ""),oss_access_key_secret=str(oss.get("secrets",{}).get("access_key_secret") or ""))


def _additional_generation_requested(policy: dict[str, Any]) -> bool:
    return policy.get("mode") in {"regenerate_additional","regenerate_all"} or (policy.get("mode")=="custom" and "additional" in (policy.get("roles") or []))


def _main_generation_requested(policy: dict[str, Any]) -> bool:
    return policy.get("mode") in {"regenerate_main","regenerate_all"} or (policy.get("mode")=="custom" and "main" in (policy.get("roles") or []))


def _review_draft(publication: Publication, child: PublicationStore) -> OzonListingDraft:
    facts=loads(publication.product_snapshot_json,{});state=loads(child.assets_json,{});content=loads(child.content_json,{})
    preparation=dict(state.get("ai_preparation") or {});generated_path=str(preparation.get("generated_image_path") or "");source_sku=Path(generated_path).parent.name if generated_path else str(facts["sku"])
    return OzonListingDraft(source_sku=source_sku,offer_id=child.offer_id,source_ozon_url="",title=str(content.get("title") or facts.get("name") or ""),exported_category=str(facts.get("category") or ""),description_ru=str(content.get("description_ru") or ""),tags_ru=list(content.get("tags") or []),primary_image=str(state.get("primary_image") or ""),images=list(state.get("images") or []),attributes=loads(child.attributes_json,[]),complex_attributes=loads(child.complex_attributes_json,[]),ai_preparation=preparation)


def review_publication_image(session: Session, publication: Publication, child: PublicationStore, action: str) -> dict[str, Any]:
    if child.status != "image_review": raise AppError("只有待核验图片可以执行此操作",code="publication_image_not_pending")
    draft=_review_draft(publication,child);config=_app_config(resolve_publication_snapshot(child.settings_snapshot_json))
    if action=="approve": approve_pending_main_image(config=config,draft=draft,reviewer="HUMAN")
    elif action in {"regenerate","redesign","full_redesign","repair"}:
        regenerate_pending_main_image(
            config=config,
            draft=draft,
            mode="redesign" if action=="regenerate" else action,
        )
    else: raise AppError("不支持的图片审核操作",code="unsupported_image_action")
    state=loads(child.assets_json,{})
    facts=loads(publication.product_snapshot_json,{})
    snapshot_images=list(facts.get("assets") or [])
    original_main=next((str(row.get("oss_url") or "") for row in snapshot_images if row.get("role")=="main"),"")
    if not original_main and snapshot_images:
        original_main=str(snapshot_images[0].get("oss_url") or "")
    state.update({
        "original_primary_image":str(state.get("original_primary_image") or original_main),
        "primary_image":draft.primary_image,
        "images":draft.images,
        "ai_preparation":draft.ai_preparation,
    });child.assets_json=dumps(state)
    child.status="queued" if action=="approve" else "image_review";child.last_error="";session.commit();return state


@job_handler("catalog_publication_image_action")
def publication_image_action_job(context: JobContext) -> None:
    publication_id = str(context.payload.get("publication_id") or "")
    store_id = str(context.payload.get("store_id") or "")
    action = str(context.payload.get("action") or "")
    publication = get_publication(context.session, publication_id)
    child = next((row for row in publication.stores if row.store_id == store_id), None)
    if not child:
        raise AppError("店铺发布记录不存在", code="publication_store_not_found")
    step_key = f"{store_id}:image_{action}"
    context.start_step(step_key)
    context.event(f"开始执行主图操作：{action}")
    review_publication_image(context.session, publication, child, action)
    context.complete_step(step_key, details={"publication_id": publication_id, "store_id": store_id, "action": action})


def _cleanup_publication_cache(publication: Publication) -> None:
    if not publication.stores or not all(loads(row.assets_json,{}).get("package_ready") for row in publication.stores): return
    facts=loads(publication.product_snapshot_json,{});root=(OZON_WORK_DATA_DIR/"generated").resolve();sku=str(facts.get("sku") or "")
    for target in root.iterdir() if root.is_dir() else []:
        resolved=target.resolve()
        if resolved.parent==root and resolved.is_dir() and (resolved.name==sku or resolved.name.startswith(sku+"-")): shutil.rmtree(resolved)
    for row in publication.stores:
        state=loads(row.assets_json,{});prep=dict(state.get("ai_preparation") or {});prep["generated_image_path"]="";prep["local_cleanup_status"]="COMPLETED";state["ai_preparation"]=prep;row.assets_json=dumps(state)


def _save_ai_checkpoint(session: Session, child: PublicationStore, draft: OzonListingDraft, main: str, additional: list[str]) -> None:
    child.content_json=dumps({"title":draft.title,"description_ru":draft.description_ru,"tags":draft.tags_ru,"generated_fresh":True})
    child.attributes_json=dumps(draft.attributes);child.complex_attributes_json=dumps(draft.complex_attributes)
    previous=loads(child.assets_json,{})
    previous.setdefault("original_primary_image",main)
    previous.update({"primary_image":draft.primary_image or main,"images":draft.images or additional,"ai_preparation":draft.ai_preparation,"package_ready":False});child.assets_json=dumps(previous);session.commit()


def prepare_store_package(context: JobContext, publication: Publication, child: PublicationStore, snapshot: dict[str, Any]) -> dict[str, Any]:
    if child.status in {"import_uncertain", "import_submitting"} and not child.external_task_id:
        raise AppError("上次提交结果待核对，请先绑定原 task_id", code="ozon_import_recovery_required")
    saved_assets=loads(child.assets_json,{})
    if saved_assets.get("package_ready"):
        return {"item":saved_assets["import_item"],"definitions":saved_assets.get("attribute_definitions",[])}
    facts=loads(publication.product_snapshot_json,{}); requirements=loads(publication.requirements_json,{}); policy=loads(publication.image_policy_json,{})
    missing=validate_product_facts(facts,requirements)
    if missing: raise AppError("产品库资料不完整，任务已暂停",code="publication_facts_missing",details=missing)
    category_id=int(requirements.get("description_category_id") or facts["description_category_id"]); type_id=int(requirements.get("type_id") or facts["type_id"])
    config=_app_config(snapshot); legacy_client=OzonSellerClient(config.ozon_client_id,config.ozon_api_key,config.ozon_api_base,repository=ProductRepository())
    definitions=legacy_client.category_attributes(category_id,type_id,"RU")
    images=publication_images(facts,requirements)
    main=next((row["oss_url"] for row in images if row.get("role")=="main"),images[0]["oss_url"])
    additional=[row["oss_url"] for row in images if row["oss_url"]!=main][:14]
    pricing=_price(snapshot,facts,requirements)
    other_versions=[loads(other.content_json,{}) for other in publication.stores if other.id!=child.id and loads(other.content_json,{})]
    # A new single-store listing must also differ from earlier listings of the
    # same product, not only siblings in this particular multi-store batch.
    previous_versions=context.session.scalars(select(PublicationStore.content_json).join(Publication,Publication.id==PublicationStore.publication_id).where(Publication.product_id==publication.product_id,Publication.id!=publication.id,PublicationStore.content_json!="{}").order_by(PublicationStore.updated_at.desc()).limit(10)).all()
    other_versions.extend(value for raw in previous_versions if (value:=loads(raw,{})))
    index=next((idx for idx,row in enumerate(publication.stores) if row.id==child.id),0)
    # Personal-computer transport carries selected facts and difference-check
    # copy only; it never mirrors the business database onto the client.
    if hasattr(publication, 'personal_other_versions'):
        other_versions = publication.personal_other_versions
        index = publication.personal_store_index
    draft=OzonListingDraft(source_sku=f"{facts['sku']}-{child.store_id[:6]}",offer_id=child.offer_id,base_offer_id=facts["sku"],source_ozon_url="",title=facts["name"],exported_category=str(requirements.get("category_display") or facts.get("category_display") or facts.get("category") or "已选择实时类目"),description_category_id=category_id,type_id=type_id,category_display_zh=str(requirements.get("category_display") or facts.get("category_display") or ""),selected_1688_item_id=str(facts.get("supplier_product_id") or ""),selected_1688_sku_id=str(facts.get("supplier_sku_id") or ""),selected_1688_url=str(facts.get("source_url") or ""),selected_1688_title=str(facts.get("name") or ""),selected_1688_sku_name=str(facts.get("specification") or ""),purchase_price_cny=str(facts.get("purchase_price_cny") or ""),domestic_delivery_cny=str(facts.get("domestic_delivery_cny") or ""),depth_mm=float(facts["package_length_mm"]),width_mm=float(facts["package_width_mm"]),height_mm=float(facts["package_height_mm"]),weight_g=float(facts["package_weight_g"]),price=str(pricing["price"]),old_price=str(pricing["old_price"]),currency_code="CNY",primary_image=main,images=additional,images_oss_ready=True,ai_preparation={"content_variant_label":child.store.name,"store_creative_profile":store_creative_profile(index),"must_differ_from_existing_copy":{"other_store_variants":other_versions},"publication_requirements":requirements,"oss_upload_scope":f"{child.store_id}-{child.offer_id}"})
    # All store variants of one SKU share the same downloaded reference cache.
    # Store-specific OSS paths remain isolated through oss_upload_scope.
    draft.source_sku=str(facts["sku"])
    from erp.core.ai_reuse import fingerprint
    content_signature=fingerprint({"facts":facts,"requirements":requirements,"policy":policy,"definitions":definitions,"store":child.store_id,"profile":store_creative_profile(index),"api":config.ai_api_url,"model":config.ai_model,"vision_model":config.vision_model})
    draft.ai_preparation["erp_content_signature"]=content_signature
    context.start_step(f"{child.store_id}:ai_content")
    saved_preparation=saved_assets.get("ai_preparation") if isinstance(saved_assets,dict) else {}
    review_approved=isinstance(saved_preparation,dict) and saved_preparation.get("image_review_status")=="APPROVED"
    content_reusable = not _main_generation_requested(policy) and isinstance(saved_preparation,dict) and saved_preparation.get("erp_content_signature")==content_signature and isinstance(saved_preparation.get("content"),dict) and bool(loads(child.content_json,{}).get("description_ru"))
    if review_approved or content_reusable:
        saved_content=loads(child.content_json,{});draft.title=str(saved_content.get("title") or draft.title);draft.description_ru=str(saved_content.get("description_ru") or "");draft.tags_ru=list(saved_content.get("tags") or []);draft.attributes=loads(child.attributes_json,[]);draft.complex_attributes=loads(child.complex_attributes_json,[]);draft.primary_image=str(saved_assets.get("primary_image") or main);draft.images=list(saved_assets.get("images") or additional);draft.images_oss_ready=True;draft.ai_preparation=dict(saved_preparation)
    else:
        checkpoint_ready=isinstance(saved_preparation,dict) and saved_preparation.get("image_stage_status")=="PENDING" and isinstance(saved_preparation.get("content"),dict) and isinstance(saved_preparation.get("visual_analysis"),dict)
        if checkpoint_ready and _main_generation_requested(policy):
            saved_content=loads(child.content_json,{})
            content=saved_preparation['content']
            draft.title=str(saved_content.get('title') or content.get('title_ru') or draft.title)
            draft.description_ru=str(saved_content.get('description_ru') or content.get('description_ru') or '')
            draft.tags_ru=list(saved_content.get('tags') or content.get('tags_ru') or [])
            draft.attributes=loads(child.attributes_json,[])
            draft.complex_attributes=loads(child.complex_attributes_json,[])
            draft.ai_preparation=dict(saved_preparation);prepare_main_image_from_checkpoint(config=config,draft=draft,on_progress=context.event)
        else:
            prepare_listing_with_ai(config=config,product={"sku":facts["sku"],"title":facts["name"],"category":facts.get("category"),"weight_grams":facts["package_weight_g"],"image_url":main,"raw_json":{**facts,"publication_requirements":requirements},"brand":""},draft=draft,definitions=definitions,ozon_client=legacy_client,sourcing={"selected":facts},on_progress=context.event,on_content_checkpoint=lambda:_save_ai_checkpoint(context.session,child,draft,main,additional),generate_image=_main_generation_requested(policy))
    context.complete_step(f"{child.store_id}:ai_content",details={"fresh_for_store":True})
    if _main_generation_requested(policy):
        context.start_step(f"{child.store_id}:ai_images")
        if draft.ai_preparation.get("image_review_status") != "APPROVED":
            _save_ai_checkpoint(context.session,child,draft,main,additional);child.status="image_review";context.session.commit()
            raise AppError("新主图等待人工核验",code="publication_image_review_required")
        context.complete_step(f"{child.store_id}:ai_images",details={"main":"regenerated"})
    else:
        draft.primary_image=main; draft.images=additional; draft.images_oss_ready=True
        context.complete_step(f"{child.store_id}:ai_images",details={"main":"reused"})
    codex_additional_ready=draft.ai_preparation.get('producer')=='codex-desktop' and len(draft.ai_preparation.get('generated_additional_urls') or [])==max(1,min(6,int(requirements.get('additional_image_count') or 3)))
    if _additional_generation_requested(policy) and not codex_additional_ready:
        work_dir=get_settings().data_dir/"publication-generated"/publication.id/child.store_id; reference=download_reference_image(draft.primary_image,work_dir,"reference.jpg")
        image_service=ImageEditService(config.image_api_url,config.image_api_key,config.image_model); vision=VisionAnalysisService(config.vision_api_url,config.vision_api_key,config.vision_model); uploader=OssUploader(config)
        generated_urls=list(draft.ai_preparation.get("generated_additional_urls") or []); count=max(1,min(6,int(requirements.get("additional_image_count") or 3)))
        scenes=["展示商品结构和细节","展示真实使用场景","展示规格和使用要点","展示包装内容","展示材质细节","展示适用人群"]
        for idx in range(count):
            if idx < len(generated_urls) and str(generated_urls[idx]).startswith("https://"):
                context.event(f"附图 {idx+1} 已完成，本次断点续跑跳过");continue
            generated=image_service.generate(str(reference),f"Create a factual 3:4 Ozon additional product image. Preserve the exact SKU, colors, parts and quantity. {scenes[idx]}. No unsupported claims, no logos, no pseudo-text. User requirements: {requirements}",work_dir)
            verified=vision.verify_generated([reference],generated,expected_unit_count=1,expected_text_lines=[],raise_on_failure=False)
            if not verified.get("matches_sku"): raise AppError(f"第 {idx+1} 张新附图未通过商品一致性校验",code="publication_additional_image_invalid",details=verified)
            generated_urls.append(uploader.upload(generated,f"{child.store_id}-{child.offer_id}",f"附图{idx+1:02d}.jpg"))
            draft.ai_preparation["generated_additional_urls"]=generated_urls;draft.images=generated_urls;_save_ai_checkpoint(context.session,child,draft,main,additional)
        draft.images=generated_urls; draft.images_oss_ready=True
    context.start_step(f"{child.store_id}:attributes")
    apply_ozon_merge_plan(draft,definitions,requirements.get("ozon_merge") or {})
    if requirements.get("generation_provider") == "codex":
        # A saved Codex copy can resume without rerunning attribute preparation.
        # Reapply only facts confirmed by this task and the current category dictionary.
        enforce_known_fact_attributes(
            draft, definitions, {"weight_grams": facts["package_weight_g"]}
        )
        if not any(int(item.get("id") or 0) == 8229 and item.get("values") for item in draft.attributes):
            type_options = legacy_client.category_attribute_values(category_id, type_id, 8229, language="RU")
            selected_type = next(
                (item for item in type_options if int(item.get("id") or 0) == type_id), None
            )
            if selected_type and selected_type.get("value"):
                apply_ai_attributes(
                    draft, definitions,
                    [{"attribute_id": 8229, "value": str(selected_type["value"])}],
                    legacy_client,
                )
    removed_dictionary_values=normalize_ozon_dictionary_attributes(draft,definitions,legacy_client)
    if removed_dictionary_values:context.event("已移除不属于当前 Ozon 字典的属性值："+"、".join(removed_dictionary_values))
    blockers=validate_listing_draft(draft,definitions)
    if blockers:
        raise AppError(
            "OZON 实时属性或必填资料未通过校验：" + "；".join(blockers),
            code="publication_validation_blocked",
            details=blockers,
        )
    item=build_ozon_import_item(draft,definitions)
    child.content_json=dumps({"title":draft.title,"description_ru":draft.description_ru,"tags":draft.tags_ru,"generated_fresh":True})
    child.attributes_json=dumps(draft.attributes); child.complex_attributes_json=dumps(draft.complex_attributes); child.pricing_json=dumps(pricing)
    child.package_json=dumps({"depth_mm":draft.depth_mm,"width_mm":draft.width_mm,"height_mm":draft.height_mm,"weight_g":draft.weight_g})
    child.assets_json=dumps({"primary_image":draft.primary_image,"images":draft.images,"ai_preparation":draft.ai_preparation,"package_ready":True,"import_item":item,"attribute_definitions":definitions})
    child.status="ready"; context.session.commit(); context.complete_step(f"{child.store_id}:attributes",details={"attribute_count":len(draft.attributes)}); context.complete_step(f"{child.store_id}:oss_upload",details={"image_count":1+len(draft.images)});_cleanup_publication_cache(publication);context.session.commit()
    return {"item":item,"definitions":definitions}


def save_store_images_to_catalog(session: Session, publication: Publication, child: PublicationStore) -> None:
    product=get_catalog_product(session,publication.product_id); state=loads(child.assets_json,{})
    for link in product.asset_links:
        if link.is_current and link.role in {"main","additional","mockup"}: link.is_current=False
    urls=[("main",state.get("primary_image")),*(("additional",url) for url in state.get("images") or [])]
    for position,(role,url) in enumerate(urls):
        if not str(url or "").startswith("https://"): continue
        legacy_id=f"{publication.id}:{child.store_id}:{role}:{position}"; asset=session.scalar(select(Asset).where(Asset.legacy_source=="publication",Asset.legacy_id==legacy_id))
        if not asset:
            asset=Asset(kind="image",name=f"{publication.id}-{role}-{position}.jpg",source="publication",oss_url=str(url),read_only=True,mirror_status="mirrored",mime_type="image/*",legacy_source="publication",legacy_id=legacy_id);session.add(asset);session.flush()
        link_catalog_asset(session,product=product,asset=asset,role=role,position=position if role=="additional" else 0,variant_id=publication.variant_id,source_kind="publication",set_current=True,metadata={"publication_id":publication.id,"store_id":child.store_id})


@job_handler("catalog_ozon_publication")
@job_handler("catalog_ozon_prepare")
def catalog_ozon_publication_job(context: JobContext) -> None:
    publication=context.session.scalar(publication_query().where(Publication.id==str(context.payload.get("publication_id") or "")))
    if not publication: raise NotFoundError("发布记录不存在")
    requested_store_ids={str(value) for value in (context.payload.get("store_ids") or [])}
    targets=[child for child in publication.stores if not requested_store_ids or child.store_id in requested_store_ids]
    if not targets: raise NotFoundError("未找到需要执行的店铺发布记录")
    if context.job.type == "catalog_ozon_prepare" and (context.payload.get("submit_ready") or any(child.external_task_id for child in targets)):
        raise ConflictError("AI 准备队列不允许执行平台提交或导入回查，请使用 Ozon 发布队列")
    successes=0; failures=[]; pause_required=False; publication.status="running"; context.session.commit()
    for child in targets:
        try:
            context.start_step(f"{child.store_id}:validate")
            facts=loads(publication.product_snapshot_json,{}); missing=validate_product_facts(facts,loads(publication.requirements_json,{}))
            if missing: raise AppError("产品资料不完整",code="publication_facts_missing",details=missing)
            context.complete_step(f"{child.store_id}:validate")
            snapshot=resolve_publication_snapshot(child.settings_snapshot_json)
            prepared=prepare_store_package(context,publication,child,snapshot)
            if not publication.submit_via_api:
                for suffix in ("submit","stock","promotion"): context.complete_step(f"{child.store_id}:{suffix}",details={"skipped":"export_only"})
                if publication.save_generated_to_catalog: save_store_images_to_catalog(context.session,publication,child)
                child.status="ready_for_export";successes+=1;context.session.commit();continue
            if not context.payload.get("submit_ready") and not child.external_task_id:
                child.status="ready_for_submit";child.last_error="";successes+=1;context.session.commit();context.event(f"{child.store.name} 图片和发布包已就绪，等待人工确认提交 Ozon");continue
            context.start_step(f"{child.store_id}:submit"); adapter=OzonSellerAdapter(snapshot)
            if not child.external_task_id:
                if child.status=="import_uncertain": raise AppError("创建结果不确定，必须先绑定原 task_id，禁止重发",code="ozon_import_recovery_required")
                child.status="import_uncertain";child.last_error="提交意图已保存，等待 Ozon 回执";context.session.commit()
                try: child.external_task_id=str(adapter.import_product(prepared["item"]))
                except OzonImportUncertain as error: child.status="import_uncertain";child.last_error=str(error);pause_required=True;context.session.commit();failures.append(str(error));continue
                child.status="import_submitted";context.session.commit()
            result=adapter.wait_import(int(child.external_task_id))
            if import_failed(result): raise AppError("OZON 商品导入失败",code="ozon_import_failed",details=result)
            if not import_succeeded(result): child.status="import_pending";context.session.commit();failures.append("导入任务尚未完成");continue
            rows=import_items(result);child.external_product_id=str((rows[0].get("product_id") or rows[0].get("id") or "") if rows else "");context.complete_step(f"{child.store_id}:submit",details={"task_id":child.external_task_id})
            # Ozon only accepts stock after the imported product becomes available.
            # Keep later writes as explicit, restartable phases instead of racing them
            # in the product-import job.
            child.stock_status="pending";child.promotion_status="pending";child.status="stock_pending";child.last_error=""
            context.event(f"{child.store.name} 建品成功，等待添加库存和促销")
            if publication.save_generated_to_catalog: save_store_images_to_catalog(context.session,publication,child)
            successes+=1;context.session.commit()
        except (JobPaused, JobCancelled):
            raise
        except Exception as error:
            if child.status == "import_uncertain" or getattr(error, "code", "") == "ozon_import_recovery_required":
                child.status="import_uncertain";child.last_error=str(error);pause_required=True;failures.append(str(error));context.session.commit();continue
            child.status="image_review" if getattr(error,"code","")=="publication_image_review_required" else ("blocked" if getattr(error,"code","").startswith("publication_") else "failed");child.last_error=str(error)[:4000];failures.append(f"{child.store.name}: {error}");context.session.commit();context.event(f"{child.store.name} 发布未完成：{error}",level="error")
            active = next((step for step in context.job.steps if step.key.startswith(f"{child.store_id}:") and step.status == "running"), None)
            if active:
                active.status = "failed"; active.error_message = str(error)[:4000]
            if getattr(error, "code", "").startswith("publication_"):
                pause_required=True
    publication.status=publication_status(publication.stores)
    context.session.commit()
    if pause_required:
        context.job.pause_requested=True;context.session.commit();raise JobPaused()
    if failures: raise AppError("部分店铺发布未完成",code="publication_store_failures",details=failures)
    if any(row.status in {"ready_for_submit", "stock_pending", "promotion_pending"} for row in publication.stores):
        raise JobActionRequired("自动准备阶段已完成，等待提交 Ozon 或处理后续库存促销")
