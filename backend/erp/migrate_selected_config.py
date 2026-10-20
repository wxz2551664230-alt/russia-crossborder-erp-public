from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import select

from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps
from erp.core.security import vault
from erp.models import Asset, IntegrationConnection, Listing, ResearchProduct, SourcingCandidate, Store, StoreListing, SystemSetting
from erp.services.legacy_migration import _legacy_dpapi


def _connection(session, provider: str, name: str, url: str, config: dict, secrets: dict, enabled: bool = True):
    item = session.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == provider, IntegrationConnection.name == name))
    if not item:
        item = IntegrationConnection(provider=provider, name=name)
        session.add(item)
    item.base_url, item.config_json = url or "", dumps(config)
    present = {k: v for k, v in secrets.items() if v}
    item.secrets_encrypted, item.enabled, item.status = vault.encrypt(present), enabled, "configured" if present else "incomplete"
    return item


def _setting(session, key: str, value: dict):
    item = session.get(SystemSetting, key) or SystemSetting(key=key)
    session.add(item); item.value_json = dumps(value)


def _migrate_follow_work(session) -> int:
    source_path = get_settings().legacy_ozon_listing_root / "data/products.db"
    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True); source.row_factory = sqlite3.Row
    stores = {item.name.casefold(): item for item in session.scalars(select(Store).where(Store.platform == "ozon")).all()}
    primary = stores.get("wxz") or next(iter(stores.values()), None)
    count = 0
    for row in source.execute("SELECT * FROM ozon_listing_drafts ORDER BY updated_at"):
        payload = json.loads(row["payload_json"] or "{}"); sku = str(row["source_sku"])
        product = session.scalar(select(ResearchProduct).where(ResearchProduct.source_sku == sku))
        if not product: continue
        product.description_category_id = payload.get("description_category_id") or product.description_category_id
        product.type_id = payload.get("type_id") or product.type_id
        product.category_display = payload.get("category_display_zh") or payload.get("category_display_ru") or product.category_display
        legacy_status = str(row["status"] or payload.get("status") or "DRAFT_BLOCKED")
        status = {"COMPLETED":"completed","PUBLISHED":"promotion_pending","DRAFT_BLOCKED":"blocked","STORE_PUBLICATIONS_FAILED":"failed"}.get(legacy_status, legacy_status.casefold())
        base_offer = str(payload.get("base_offer_id") or payload.get("offer_id") or f"LEGACY-{sku}")
        listing = session.scalar(select(Listing).where(Listing.legacy_source == "ozon_listing", Listing.legacy_id == sku))
        if not listing:
            listing = Listing(research_product_id=product.id, base_offer_id=base_offer, legacy_source="ozon_listing", legacy_id=sku); session.add(listing); session.flush()
        listing.title = str(payload.get("title") or product.title); listing.description_ru = str(payload.get("description_ru") or "")
        listing.tags_json = dumps(payload.get("tags_ru") or []); listing.attributes_json = dumps(payload.get("attributes") or [])
        listing.complex_attributes_json = dumps(payload.get("complex_attributes") or [])
        listing.pricing_json = dumps({"price":payload.get("price"),"old_price":payload.get("old_price"),"currency":payload.get("currency_code") or "RUB","result":payload.get("pricing_result") or {}})
        listing.package_json = dumps({"depth_mm":payload.get("depth_mm"),"width_mm":payload.get("width_mm"),"height_mm":payload.get("height_mm"),"weight_g":payload.get("weight_g")})
        listing.status = status; listing.blockers_json = dumps(payload.get("blockers") or ([payload.get("workflow_error")] if payload.get("workflow_error") else []))
        asset_ids=[]
        for index,url in enumerate([payload.get("primary_image"), *(payload.get("images") or [])]):
            if not url: continue
            legacy_id=f"{sku}:image:{index}"; asset=session.scalar(select(Asset).where(Asset.legacy_source=="ozon_listing",Asset.legacy_id==legacy_id))
            if not asset: asset=Asset(name=f"{sku}-{index}.jpg",legacy_source="ozon_listing",legacy_id=legacy_id);session.add(asset)
            asset.kind="listing_image";asset.source="legacy_oss";asset.oss_url=str(url);asset.read_only=True;asset.mirror_status="mirrored";session.flush();asset_ids.append(asset.id)
        listing.asset_ids_json=dumps(asset_ids)
        publications=payload.get("store_publications") or {"primary":{"store_name":primary.name if primary else "","status":legacy_status,"offer_id":payload.get("offer_id"),"task_id":row["task_id"]}}
        for pub_key,pub in publications.items():
            if not isinstance(pub,dict): continue
            store=stores.get(str(pub.get("store_name") or "").casefold()) or (primary if pub_key=="primary" else None)
            if not store: continue
            child=session.scalar(select(StoreListing).where(StoreListing.listing_id==listing.id,StoreListing.store_id==store.id))
            if not child: child=StoreListing(listing_id=listing.id,store_id=store.id,idempotency_key=f"legacy:{sku}:{store.id}");session.add(child)
            child.offer_id=str(pub.get("offer_id") or payload.get("offer_id") or base_offer); child.external_task_id=str(pub.get("task_id") or row["task_id"] or "")
            pub_status=str(pub.get("status") or legacy_status); child.status={"COMPLETED":"completed","PUBLISHED":"promotion_pending","IMPORT_COMPLETED_STOCK_PENDING":"stock_pending","STOCK_COMPLETED_PROMOTION_PENDING":"promotion_pending"}.get(pub_status,"failed" if "FAILED" in pub_status else pub_status.casefold())
            child.stock_status="completed" if child.status in {"promotion_pending","completed"} else "pending"
            child.promotion_status="completed" if child.status=="completed" else "pending"; child.last_error=str(pub.get("error") or payload.get("workflow_error") or "")
            child.content_json=dumps({"title":pub.get("title") or listing.title,"description_ru":pub.get("description_ru") or listing.description_ru});child.assets_json=dumps({"primary_image":pub.get("primary_image") or payload.get("primary_image")})
            child.pricing_json=listing.pricing_json
        product.status = status; count += 1
        selected_item = str(payload.get("selected_1688_item_id") or "")
        selected_sku = str(payload.get("selected_1688_sku_id") or "")
        if selected_item:
            candidate = session.scalar(select(SourcingCandidate).where(
                SourcingCandidate.research_product_id == product.id,
                SourcingCandidate.external_product_id == selected_item,
                SourcingCandidate.external_sku_id == selected_sku,
            ))
            if not candidate:
                candidate = SourcingCandidate(research_product_id=product.id, provider="legacy_confirmed", external_product_id=selected_item, external_sku_id=selected_sku)
                session.add(candidate)
            candidate.title = str(payload.get("selected_1688_title") or ""); candidate.sku_name = str(payload.get("selected_1688_sku_name") or "")
            candidate.source_url = str(payload.get("selected_1688_url") or ""); candidate.unit_price_cny = payload.get("purchase_price_cny")
            candidate.domestic_delivery_cny = payload.get("domestic_delivery_cny"); candidate.landed_cost_cny = payload.get("selected_1688_landed_cost")
            candidate.selected = True; candidate.status = "selected"; candidate.raw_json = dumps({"skuName": candidate.sku_name, "title": candidate.title})
    source.close(); session.flush(); return count


def main() -> None:
    settings = get_settings()
    listing = json.loads((settings.legacy_ozon_listing_root / "data/config.local.json").read_text(encoding="utf-8"))
    composer_root = settings.legacy_composer_root / "data"
    composer = json.loads(Fernet((composer_root / "master.key").read_bytes().strip()).decrypt((composer_root / "settings.enc").read_bytes()).decode())
    image_root = settings.legacy_automation_root / "配置文件"
    image_oss = json.loads((image_root / "oss_config.json").read_text(encoding="utf-8-sig"))
    image_cfg = json.loads((image_root / "ozon_image_config.json").read_text(encoding="utf-8-sig"))
    child_cfg = json.loads((image_root / "children_image_config.json").read_text(encoding="utf-8-sig"))
    size_cfg = json.loads((image_root / "size_chart_config.json").read_text(encoding="utf-8-sig"))
    pipe_cfg = json.loads((image_root / "pipeline_config.json").read_text(encoding="utf-8-sig"))
    copy_cfg = json.loads((image_root / "copywriting_generated_test_config.json").read_text(encoding="utf-8-sig"))
    ai_studio = json.loads((settings.legacy_ai_root / "user_settings.json").read_text(encoding="utf-8-sig"))
    with SessionLocal() as session:
        # 只保留产品库接口 A4；它以后同时承担自有产品管理。
        _connection(session, "product_library", "统一产品库", listing.get("product_manager_api_url", ""), {
            "shop_id": listing.get("product_manager_shop_id"), "shop_name": listing.get("product_manager_shop_name")
        }, {"token": _legacy_dpapi(listing.get("product_manager_api_token", ""))})
        # AI 统一为 micuapi，文字与图片令牌各一份。
        _connection(session, "ai_chat", "micuapi 文字模型", "https://www.micuapi.ai/v1/chat/completions", {
            "models": ["gpt-5.6-terra", "gpt-5.6-sol"], "default_model": "gpt-5.6-terra"
        }, {"api_key": composer.get("micu_text_api_key") or _legacy_dpapi(listing.get("ai_api_key", ""))})
        _connection(session, "ai_image", "micuapi 生图模型", "https://www.micuapi.ai/v1/images/edits", {
            "models": ["gpt-image-2", "gpt-image-2-openai"], "default_model": "gpt-image-2"
        }, {"api_key": composer.get("micu_image_api_key") or _legacy_dpapi(listing.get("image_api_key", ""))})
        # 同一 Bucket 按功能拆分，允许各功能单独换前缀或凭据。
        _connection(session, "oss", "OZON 自动上架 OSS", listing.get("oss_endpoint", ""), {
            "region": listing.get("oss_region"), "bucket": listing.get("oss_bucket"), "public_base_url": listing.get("oss_public_base_url"), "prefix": listing.get("oss_object_prefix")
        }, {"access_key_id": _legacy_dpapi(listing.get("oss_access_key_id", "")), "access_key_secret": _legacy_dpapi(listing.get("oss_access_key_secret", ""))})
        for name, prefix in (("图片流水线 OSS", image_oss.get("object_prefix")), ("产品库与订单 OSS", composer.get("oss_object_prefix"))):
            _connection(session, "oss", name, image_oss.get("endpoint", ""), {
                "region": image_oss.get("region"), "bucket": image_oss.get("bucket"), "public_base_url": image_oss.get("public_base_url"), "prefix": prefix
            }, {"access_key_id": image_oss.get("access_key_id"), "access_key_secret": image_oss.get("access_key_secret")})
        for provider, name, base, secrets, enabled in (
            ("hicustom", "HICUSTOM", "hicustom_base_url", {"app_key": composer.get("hicustom_app_key"), "app_secret": composer.get("hicustom_app_secret")}, True),
            ("s2b", "S2B", "s2b_base_url", {"app_key": composer.get("s2b_app_key"), "app_secret": composer.get("s2b_app_secret")}, bool(composer.get("s2b_enabled", True))),
            ("sds", "SDS", "sds_base_url", {"app_id": composer.get("sds_app_id"), "app_secret": composer.get("sds_app_secret")}, bool(composer.get("sds_enabled", True))),
            ("tms", "TMS 云仓", "tms_base_url", {"app_key": composer.get("tms_app_key"), "app_secret": composer.get("tms_app_secret")}, True),
        ):
            _connection(session, provider, name, composer.get(base, ""), {}, secrets, enabled)
        # 使用订单系统的店铺清单，自动消除另一套重复 ZYH。
        for raw in composer.get("order_stores", []):
            platform = "wildberries" if raw.get("platform") == "wb" else "ozon"
            external = str(raw.get("client_id") or "") or f"wb:{raw.get('name')}"
            store = session.scalar(select(Store).where(Store.platform == platform, Store.external_account_id == external))
            if not store: store = Store(platform=platform, external_account_id=external); session.add(store)
            store.name, store.currency, store.enabled = str(raw.get("name")), "RUB", bool(raw.get("enabled", True))
            store.config_json = dumps({k: raw.get(k) for k in ("performance_rub_to_cny_rate", "finance_rub_to_cny_rate") if raw.get(k) is not None})
            keys = {"client_id": raw.get("client_id"), "api_key": raw.get("api_key"), "token": raw.get("token"), "performance_token": raw.get("performance_token"), "performance_client_id": raw.get("performance_client_id")}
            store.secrets_encrypted = vault.encrypt({k: v for k, v in keys.items() if v})
        migrated_follow_work = _migrate_follow_work(session)
        _setting(session, "pricing", {k: listing.get(k) for k in listing if k.startswith("pricing_") or k == "target_cost_profit_margin"})
        _setting(session, "finance", {"default_rub_to_cny_rate": 0.0783})
        _setting(session, "copywriting", {k: copy_cfg.get(k) for k in ("title_target_chars", "title_max_chars", "max_tokens", "max_images_per_product", "desc_min_chars", "desc_max_chars", "concurrency_limit", "request_timeout") if k in copy_cfg})
        _setting(session, "image_generation", {k: image_cfg.get(k) for k in ("image_size", "image_quality", "image_plan_count", "max_concurrent_requests", "request_timeout", "request_retries", "failed_task_reruns", "image_types") if k in image_cfg})
        _setting(session, "size_chart", {k: size_cfg.get(k) for k in ("model", "template_key", "skip_exists", "enable_cache")})
        _setting(session, "watermark", {k: pipe_cfg.get(k) for k in ("watermark_file", "watermark_position", "watermark_scale", "watermark_opacity")})
        _setting(session, "ai_pod", {k: ai_studio.get(k) for k in ("planner_model", "image_model", "aspect_ratio", "max_pixels", "design_count", "workflow_mode", "quality", "output_format", "generation_concurrency", "super_resolution_concurrency", "image_retry_count", "super_resolution_scale")})
        # 收货资料包含个人信息，和接口密钥一起加密，而不是放进普通设置。
        _connection(session, "procurement", "采购与收货资料", "", {"hicustom_store_id": composer.get("hicustom_store_id"), "s2b_store_id": composer.get("s2b_store_id"), "tms_warehouse_id": composer.get("tms_warehouse_id")}, {"addresses": composer.get("batch_order_addresses") or [composer.get("batch_order_address")]})
        session.commit()
    print(f"Selected configuration migration completed; Ozon follow drafts: {migrated_follow_work}")


if __name__ == "__main__": main()
