from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from erp.core.errors import AppError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.core.security import vault
from erp.models import Asset, IntegrationConnection, Listing, ResearchProduct, SourcingCandidate, StoreListing
from erp.services.assets import register_local_asset
from erp.services.jobs import JobContext, job_handler
from erp.modules.ozon_follow.legacy.config import AppConfig
from erp.modules.ozon_follow.legacy.services.ai_listing import (
    approve_pending_main_image, prepare_listing_with_ai, prepare_main_image_from_checkpoint,
    regenerate_pending_main_image,
)
from erp.modules.ozon_follow.legacy.services.ozon_client import OzonSellerClient
from erp.modules.ozon_follow.legacy.services.ozon_listing import OzonListingDraft
from erp.modules.ozon_follow.legacy.paths import DATA_DIR


def _connection(context: JobContext, provider: str, name: str = "") -> tuple[dict, dict]:
    query = select(IntegrationConnection).where(IntegrationConnection.provider == provider, IntegrationConnection.enabled.is_(True))
    if name: query = query.where(IntegrationConnection.name == name)
    item = context.session.scalar(query.order_by(IntegrationConnection.created_at))
    if not item: raise AppError(f"尚未配置 {provider} 接口", code="connection_missing")
    return {"base_url": item.base_url, **loads(item.config_json, {})}, vault.decrypt(item.secrets_encrypted)


def _config(context: JobContext, child: StoreListing) -> AppConfig:
    text, text_secret = _connection(context, "ai_chat")
    image, image_secret = _connection(context, "ai_image")
    oss, oss_secret = _connection(context, "oss", "OZON 自动上架 OSS")
    store_secret = vault.decrypt(child.store.secrets_encrypted)
    configured_models = tuple(
        str(model).strip() for model in (text.get("models") or []) if str(model).strip()
    )
    return AppConfig(
        ozon_client_id=str(store_secret.get("client_id") or child.store.external_account_id or ""),
        ozon_api_key=str(store_secret.get("api_key") or ""), ozon_api_base=str(loads(child.store.config_json, {}).get("api_base") or "https://api-seller.ozon.ru"),
        ai_api_url=str(text.get("base_url") or ""), ai_api_key=str(text_secret.get("api_key") or ""), ai_model=str(text.get("default_model") or "gpt-5.6-terra"), ai_models=configured_models,
        vision_api_url=str(text.get("base_url") or ""), vision_api_key=str(text_secret.get("api_key") or ""), vision_model=str(text.get("default_model") or "gpt-5.6-terra"), vision_models=configured_models,
        image_api_url=str(image.get("base_url") or ""), image_api_key=str(image_secret.get("api_key") or ""), image_model=str(image.get("default_model") or "gpt-image-2"),
        oss_region=str(oss.get("region") or ""), oss_endpoint=str(oss.get("base_url") or ""), oss_bucket=str(oss.get("bucket") or ""),
        oss_public_base_url=str(oss.get("public_base_url") or ""), oss_object_prefix=str(oss.get("prefix") or "ozon-auto-listing"),
        oss_access_key_id=str(oss_secret.get("access_key_id") or ""), oss_access_key_secret=str(oss_secret.get("access_key_secret") or ""),
    )


def _draft(listing: Listing, child: StoreListing) -> OzonListingDraft:
    source = listing.research_product; package = loads(listing.package_json, {}); pricing = loads(child.pricing_json, {}) or loads(listing.pricing_json, {})
    dimensions_are_facts = package.get("dimensions_source") != "operational_default"
    assets = loads(child.assets_json, {}); preparation = assets.get("ai_preparation") if isinstance(assets, dict) else {}
    preparation = {**(preparation or {}), "oss_upload_scope": f"{child.store_id}-{child.offer_id}"}
    urls = [asset.oss_url for asset in listing._asset_records] if hasattr(listing, "_asset_records") else []
    selected = next((item for item in source.sourcing_candidates if item.selected), None) if source else None
    return OzonListingDraft(
        source_sku=source.source_sku, offer_id=child.offer_id, base_offer_id=listing.base_offer_id, source_ozon_url=source.ozon_url,
        title=str(loads(child.content_json, {}).get("title") or listing.title), exported_category=source.category,
        description_ru=str(loads(child.content_json, {}).get("description_ru") or listing.description_ru), tags_ru=loads(listing.tags_json, []),
        description_category_id=int(source.description_category_id or 0), type_id=int(source.type_id or 0), category_display_zh=source.category_display,
        selected_1688_item_id=selected.external_product_id if selected else "", selected_1688_sku_id=selected.external_sku_id if selected else "",
        selected_1688_url=selected.source_url if selected else "", selected_1688_title=selected.title if selected else "", selected_1688_sku_name=selected.sku_name if selected else "",
        purchase_price_cny=str(selected.unit_price_cny or "") if selected else "", domestic_delivery_cny=str(selected.domestic_delivery_cny or "") if selected else "",
        selected_1688_landed_cost=str(selected.landed_cost_cny or "") if selected else "",
        depth_mm=package.get("depth_mm") if dimensions_are_facts else None,
        width_mm=package.get("width_mm") if dimensions_are_facts else None,
        height_mm=package.get("height_mm") if dimensions_are_facts else None,
        weight_g=package.get("weight_g"),
        price=str(pricing.get("price") or ""), old_price=str(pricing.get("old_price") or "0"), currency_code=str(pricing.get("currency") or "CNY"),
        primary_image=str(assets.get("primary_image") or (urls[0] if urls else "")), images=urls[1:], images_oss_ready=bool(urls and all(url.startswith("https://") for url in urls)),
        attributes=loads(child.attributes_json, []) or loads(listing.attributes_json, []),
        complex_attributes=loads(child.complex_attributes_json, []) or loads(listing.complex_attributes_json, []), ai_preparation=preparation or {},
    )


def _save(context: JobContext, listing: Listing, child: StoreListing, draft: OzonListingDraft) -> None:
    child.content_json = dumps({"title": draft.title, "description_ru": draft.description_ru, "tags": draft.tags_ru})
    child.assets_json = dumps({"primary_image": draft.primary_image, "images": draft.images, "ai_preparation": draft.ai_preparation})
    child.attributes_json, child.complex_attributes_json = dumps(draft.attributes), dumps(draft.complex_attributes)
    if draft.title: listing.title = draft.title
    if draft.description_ru: listing.description_ru = draft.description_ru
    paths = [draft.ai_preparation.get("generated_image_path")]
    for path in paths:
        if path:
            try: register_local_asset(context.session, path, kind="ai_candidate", source="ozon_follow", read_only=False, calculate_hash=True)
            except (OSError, ValueError): pass
    context.session.commit()


def _cleanup_uploaded_product_files(context: JobContext, listing: Listing) -> dict[str, int] | None:
    """Remove shared SKU work files only after every store package is OSS-complete."""
    enabled_children = [child for child in listing.store_listings if child.store.enabled]
    if not listing.research_product or not enabled_children:
        return None
    states: list[tuple[StoreListing, dict, dict]] = []
    for child in enabled_children:
        assets = loads(child.assets_json, {})
        preparation = assets.get("ai_preparation") if isinstance(assets, dict) else {}
        urls = [assets.get("primary_image"), *(assets.get("images") or [])]
        if (
            not isinstance(preparation, dict)
            or preparation.get("image_review_status") != "APPROVED"
            or not urls
            or not all(str(url or "").startswith("https://") for url in urls)
        ):
            return None
        states.append((child, assets, preparation))
    root = (DATA_DIR / "generated").resolve()
    target = (root / listing.research_product.source_sku).resolve()
    if target.parent != root or not target.is_dir():
        return None
    removed = 0
    released = 0
    for path in target.iterdir():
        if path.is_file():
            released += path.stat().st_size
            path.unlink()
            removed += 1
    try:
        target.rmdir()
    except OSError:
        pass
    cleaned_at = datetime.now(timezone.utc).isoformat()
    for child, assets, preparation in states:
        preparation.update({
            "local_cleanup_status": "COMPLETED",
            "local_cleanup_at": cleaned_at,
            "local_cleanup_files": removed,
            "local_cleanup_bytes": released,
            "generated_image_path": "",
        })
        assets["ai_preparation"] = preparation
        child.assets_json = dumps(assets)
    context.session.commit()
    context.event(f"所有店铺图片已上传 OSS，自动清理本地临时文件 {removed} 个，释放 {released / 1024 / 1024:.2f} MB")
    return {"files": removed, "bytes": released}


def _load(context: JobContext) -> tuple[Listing, list[StoreListing]]:
    listing = context.session.scalar(select(Listing).where(Listing.id == context.payload.get("listing_id")).options(
        selectinload(Listing.research_product).selectinload(ResearchProduct.sourcing_candidates), selectinload(Listing.store_listings).selectinload(StoreListing.store)))
    if not listing or not listing.research_product: raise NotFoundError("上架商品不存在")
    ids=loads(listing.asset_ids_json,[]); listing._asset_records=context.session.scalars(select(Asset).where(Asset.id.in_(ids))).all() if ids else []
    selected=set(context.payload.get("store_ids") or []); children=[item for item in listing.store_listings if item.store.enabled and (not selected or item.store_id in selected)]
    if not children: raise AppError("没有可执行的目标店铺",code="no_target_store")
    return listing,children


@job_handler("ozon_ai_prepare")
def ai_prepare(context: JobContext) -> None:
    listing, children = _load(context)
    for child in children:
        key=f"{child.store_id}:ai_prepare"
        step = next((item for item in context.job.steps if item.key == key), None)
        if step and step.status == "completed":
            context.event(f"{child.store.name} 的 AI 准备已经完成，本次重试跳过")
            continue
        context.start_step(key); config=_config(context,child)
        client=OzonSellerClient(config.ozon_client_id,config.ozon_api_key,config.ozon_api_base)
        definitions=client.category_attributes(listing.research_product.description_category_id,listing.research_product.type_id,"RU")
        draft=_draft(listing,child)
        preparation = draft.ai_preparation if isinstance(draft.ai_preparation, dict) else {}
        if (
            preparation.get("image_stage_status") == "PENDING"
            and isinstance(preparation.get("content"), dict)
            and isinstance(preparation.get("visual_analysis"), dict)
            and isinstance(preparation.get("reference_image_urls"), list)
        ):
            context.event(f"{child.store.name} 正在复用已保存的文案和属性，仅重试生图")
            prepare_main_image_from_checkpoint(
                config=config, draft=draft, on_progress=context.event,
            )
        else:
            prepare_listing_with_ai(
                config=config,
                product={"sku":listing.research_product.source_sku,"title":listing.research_product.title,"brand":listing.research_product.brand,"image_url":listing.research_product.image_url,"image_urls":loads(listing.research_product.image_urls_json, []),"ozon_url":listing.research_product.ozon_url,"weight_grams":listing.research_product.weight_grams},
                draft=draft,
                definitions=definitions,
                ozon_client=client,
                on_progress=context.event,
                on_content_checkpoint=lambda: _save(context, listing, child, draft),
            )
        _save(context,listing,child,draft); child.status="image_review" if draft.ai_preparation.get("image_review_status")=="PENDING" else "ready"; context.session.commit();context.complete_step(key)


@job_handler("ozon_image_review_action")
def image_review_action(context: JobContext) -> None:
    listing, children = _load(context); action=str(context.payload.get("action") or "")
    for child in children:
        key=f"{child.store_id}:image_{action}";context.start_step(key)
        try:
            draft=_draft(listing,child);config=_config(context,child)
            if action=="approve": approve_pending_main_image(config=config,draft=draft,on_progress=context.event,reviewer="HUMAN")
            elif action=="regenerate": regenerate_pending_main_image(config=config,draft=draft,on_progress=context.event)
            else: raise AppError("不支持的主图操作",code="unsupported_image_action")
            _save(context,listing,child,draft);child.status="ready" if draft.ai_preparation.get("image_review_status")=="APPROVED" else "image_review";child.last_error="";context.session.commit()
        except Exception as error:
            child.status = "image_review"
            child.last_error = str(error)[:4000]
            context.session.commit()
            raise
        cleanup = _cleanup_uploaded_product_files(context, listing) if action == "approve" else None
        context.complete_step(key, details={"local_cleanup": cleanup or {"deferred": True}})
