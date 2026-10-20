from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from decimal import Decimal

from erp.core.errors import AppError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.integrations.ozon import OzonImportUncertain, OzonSellerAdapter, import_failed, import_items, import_succeeded
from erp.models import Asset, IntegrationConnection, Listing, ResearchProduct, SourcingCandidate, StoreListing
from erp.core.security import vault
from erp.modules.ozon_follow.legacy.services.product_manager import ProductManagerApi, build_product_manager_payload
from erp.modules.ozon_follow.legacy.services.ozon_listing import OzonListingDraft
from erp.modules.ozon_follow.legacy.services.sourcing_selector import SourcingCandidate as LegacyCandidate
from erp.services.config_resolution import resolve_job_snapshot
from erp.services.catalog import ensure_follow_catalog_product
from erp.services.jobs import JobContext, job_handler
from erp.services.listing_pricing import effective_pricing_package


PIPELINE_STEPS = [
    ("validate", "检查上架资料"),
    ("ai_prepare", "生成并核验文案与主图"),
    ("submit", "提交 OZON 商品"),
    ("product_sync", "同步统一产品库"),
    ("stock", "发布库存"),
    ("promotion", "加入促销"),
]


def validate_listing(listing: Listing, store_listing: StoreListing, session) -> list[str]:
    blockers: list[str] = []
    source = listing.research_product
    if not listing.title.strip():
        blockers.append("商品标题为空")
    if not source or not source.description_category_id or not source.type_id:
        blockers.append("尚未确认 OZON 实时类目")
    package = effective_pricing_package(listing)
    for key, label in (("depth_mm", "包装长度"), ("width_mm", "包装宽度"), ("height_mm", "包装高度"), ("weight_g", "包装重量")):
        if float(package.get(key) or 0) <= 0:
            blockers.append(f"{label}缺失")
    pricing = loads(store_listing.pricing_json, {}) or loads(listing.pricing_json, {})
    if float(pricing.get("price") or 0) <= 0:
        blockers.append("上架售价缺失")
    asset_ids = loads(listing.asset_ids_json, [])
    assets = session.scalars(select(Asset).where(Asset.id.in_(asset_ids))).all() if asset_ids else []
    child_assets = loads(store_listing.assets_json, {})
    urls = [asset.oss_url for asset in assets if asset.oss_url.startswith("https://")]
    urls.extend([child_assets.get("primary_image"), *(child_assets.get("images") or [])])
    urls = [url for url in urls if str(url or "").startswith("https://")]
    if not urls:
        blockers.append("主图和附图尚未上传 OSS")
    return blockers


def build_import_item(listing: Listing, store_listing: StoreListing, session) -> dict[str, Any]:
    blockers = validate_listing(listing, store_listing, session)
    if blockers:
        raise AppError("上架资料不完整", code="listing_blocked", details=blockers)
    source = listing.research_product
    package = effective_pricing_package(listing)
    pricing = loads(store_listing.pricing_json, {}) or loads(listing.pricing_json, {})
    content = loads(store_listing.content_json, {})
    asset_ids = loads(listing.asset_ids_json, [])
    assets = session.scalars(select(Asset).where(Asset.id.in_(asset_ids))).all()
    child_assets = loads(store_listing.assets_json, {})
    images = list(dict.fromkeys([
        child_assets.get("primary_image"), *(child_assets.get("images") or []),
        *(asset.oss_url for asset in assets if asset.oss_url.startswith("https://")),
    ]))
    images = [str(url) for url in images if str(url or "").startswith("https://")][:15]
    return {
        "attributes": loads(store_listing.attributes_json, []) or loads(listing.attributes_json, []),
        "barcode": "",
        "description_category_id": int(source.description_category_id),
        "new_description_category_id": 0,
        "color_image": images[0],
        "complex_attributes": loads(store_listing.complex_attributes_json, []) or loads(listing.complex_attributes_json, []),
        "currency_code": str(pricing.get("currency") or store_listing.store.currency or "CNY").upper(),
        "depth": float(package["depth_mm"]),
        "dimension_unit": "mm",
        "height": float(package["height_mm"]),
        "images": images[1:],
        "name": str(content.get("title") or listing.title)[:500],
        "offer_id": store_listing.offer_id,
        "old_price": str(pricing.get("old_price") or "0"),
        "pdf_list": [],
        "price": str(pricing["price"]),
        "primary_image": images[0],
        "type_id": int(source.type_id),
        "vat": str(pricing.get("vat") or "0"),
        "weight": float(package["weight_g"]),
        "weight_unit": "g",
        "width": float(package["width_mm"]),
    }


@job_handler("ozon_listing_pipeline")
def ozon_listing_pipeline_job(context: JobContext) -> None:
    listing = context.session.scalar(
        select(Listing).where(Listing.id == str(context.payload.get("listing_id") or ""))
        .options(selectinload(Listing.research_product).selectinload(ResearchProduct.sourcing_candidates), selectinload(Listing.store_listings).selectinload(StoreListing.store))
    )
    if not listing:
        raise NotFoundError("上架草稿不存在")
    store_ids = set(context.payload.get("store_ids") or [])
    targets = [item for item in listing.store_listings if item.store.enabled and (not store_ids or item.store_id in store_ids)]
    dry_run = bool(context.payload.get("dry_run", True))
    start_at = str(context.payload.get("start_at") or "validate")
    stop_after = str(context.payload.get("stop_after") or "")
    step_order = [key for key, _name in PIPELINE_STEPS]
    start_index = step_order.index(start_at)
    snapshots = loads(context.job.config_snapshot_json, {}).get("stores") or {}
    for store_listing in targets:
        prefix = store_listing.store.name
        blockers = validate_listing(listing, store_listing, context.session)
        listing.blockers_json = dumps(blockers)
        if start_index <= 0:
            context.start_step(f"{store_listing.store_id}:validate")
            if blockers:
                store_listing.status = "blocked"
                context.session.commit()
                raise AppError(f"{prefix} 上架资料不完整", code="listing_blocked", details=blockers)
            store_listing.status = "ready"
            context.session.commit()
            context.complete_step(f"{store_listing.store_id}:validate")
        if start_index <= 1:
            context.start_step(f"{store_listing.store_id}:ai_prepare")
            assets = loads(store_listing.assets_json, {})
            preparation = assets.get("ai_preparation") if isinstance(assets, dict) else {}
            content = loads(store_listing.content_json, {})
            if not content.get("title") or not content.get("description_ru"):
                raise AppError(f"{prefix} 尚未生成独立俄文文案", code="ai_content_missing")
            if not preparation or preparation.get("image_review_status") != "APPROVED":
                raise AppError(f"{prefix} 主图尚未完成核验", code="image_review_required")
            if not str(assets.get("primary_image") or "").startswith("https://"):
                raise AppError(f"{prefix} 已核验主图尚未上传 OSS", code="oss_image_missing")
            context.complete_step(f"{store_listing.store_id}:ai_prepare", details={"verified": True})
        if dry_run:
            prefix_key = f"{store_listing.store_id}:"
            for step in list(context.job.steps):
                if step.key.startswith(prefix_key) and step.status != "completed":
                    context.complete_step(step.key, details={"dry_run": True})
            store_listing.status = "ready"
            context.session.commit()
            continue
        snapshot_value = snapshots.get(store_listing.store_id)
        if not snapshot_value:
            raise AppError(f"{prefix} 缺少任务配置快照", code="snapshot_missing")
        adapter = OzonSellerAdapter(resolve_job_snapshot(snapshot_value))
        if start_index <= 2:
            context.start_step(f"{store_listing.store_id}:submit")
            submit_already_completed = bool(
                store_listing.external_product_id
                and store_listing.status in {"stock_pending", "promotion_pending", "completed"}
            )
            if submit_already_completed:
                context.complete_step(
                    f"{store_listing.store_id}:submit",
                    details={"task_id": store_listing.external_task_id, "idempotent_skip": True},
                )
            elif not store_listing.external_task_id:
                if store_listing.status == "import_uncertain":
                    raise AppError(f"{prefix} 创建结果不确定，请先在异常处理页填写 OZON task_id，禁止直接重发", code="ozon_import_recovery_required")
                try:
                    item = build_import_item(listing, store_listing, context.session)
                    store_listing.status = "import_uncertain"
                    store_listing.last_error = "提交意图已保存，等待 Ozon 回执"
                    context.session.commit()
                    task_id = adapter.import_product(item)
                except OzonImportUncertain as error:
                    store_listing.status = "import_uncertain"
                    store_listing.last_error = str(error)
                    context.session.commit()
                    raise
                store_listing.external_task_id = str(task_id)
                store_listing.status = "import_submitted"
                context.session.commit()
            if not submit_already_completed:
                result = adapter.wait_import(int(store_listing.external_task_id))
                if import_failed(result):
                    store_listing.status = "import_failed"
                    store_listing.last_error = dumps(result)[:4000]
                    context.session.commit()
                    raise AppError(f"{prefix} 商品导入失败", code="ozon_import_failed")
                if not import_succeeded(result):
                    store_listing.status = "import_pending"
                    context.session.commit()
                    raise AppError(f"{prefix} 导入任务尚未完成，可稍后重试查询", code="ozon_import_pending")
                store_listing.status = "stock_pending"
                imported_items = import_items(result)
                if imported_items:
                    store_listing.external_product_id = str(imported_items[0].get("product_id") or imported_items[0].get("id") or store_listing.external_product_id)
                store_listing.last_error = ""
                context.session.commit()
                context.complete_step(f"{store_listing.store_id}:submit", details={"task_id": store_listing.external_task_id})
        config = resolve_job_snapshot(snapshot_value)
        store_config = config
        if start_index <= 3:
            context.start_step(f"{store_listing.store_id}:product_sync")
            sync_state = next((loads(other.assets_json, {}).get("product_sync") for other in listing.store_listings if loads(other.assets_json, {}).get("product_sync")), None)
            result: dict[str, Any] = dict(sync_state) if isinstance(sync_state, dict) else {}
            catalog_product = listing.catalog_product
            if not catalog_product or not result.get("internal"):
                try:
                    catalog_product = ensure_follow_catalog_product(context.session, listing)
                    result.update({"catalog_product_id": catalog_product.id, "internal": True})
                    result.pop("pending", None)
                except AppError as error:
                    if error.code != "follow_catalog_incomplete":
                        raise
                    result.update({"internal": False, "pending": error.details or [str(error)]})
            connection = context.session.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "product_library", IntegrationConnection.enabled.is_(True)))
            source = next((item for item in listing.research_product.sourcing_candidates if item.selected), None) if listing.research_product else None
            # Local catalog and ECS are independent checkpoints. A previous local
            # validation failure must not permanently suppress a later ECS retry.
            if connection and source and catalog_product and not result.get("a4"):
                secret = vault.decrypt(connection.secrets_encrypted)
                candidate = LegacyCandidate(
                    item_id=source.external_product_id, sku_id=source.external_sku_id, title=source.title, sku_name=source.sku_name,
                    image_url=source.image_url, company="", unit_price=Decimal(str(source.unit_price_cny)) if source.unit_price_cny is not None else None,
                    delivery_price=Decimal(str(source.domestic_delivery_cny)) if source.domestic_delivery_cny is not None else None,
                    minimum_order_quantity=Decimal(str(source.minimum_order_quantity)) if source.minimum_order_quantity is not None else None,
                    sales=source.sales, raw={**loads(source.raw_json, {}), "skuName": source.sku_name, "title": source.title},
                )
                source_raw = loads(source.raw_json, {})
                draft = OzonListingDraft(source_sku=listing.research_product.source_sku, offer_id=store_listing.offer_id, base_offer_id=catalog_product.sku, source_ozon_url=listing.research_product.ozon_url, title=listing.title, exported_category=listing.research_product.category, primary_image=loads(store_listing.assets_json, {}).get("primary_image", ""), selected_1688_url=source.source_url, product_manager_color=str(source_raw.get("color") or ""), product_manager_size=str(source_raw.get("size") or ""), product_manager_remark=str(source_raw.get("remark") or ""))
                result["a4"] = ProductManagerApi(connection.base_url, str(secret.get("token") or "")).upsert_product(build_product_manager_payload(draft, candidate, int(loads(connection.config_json, {}).get("shop_id") or 0)))
            assets_state = loads(store_listing.assets_json, {}); assets_state["product_sync"] = result or {"ok": True}; store_listing.assets_json = dumps(assets_state); context.session.commit()
            context.complete_step(f"{store_listing.store_id}:product_sync", details={"idempotent": True})
        if stop_after == "product_sync":
            if store_listing.status not in {"promotion_pending", "completed"}:
                store_listing.status = "stock_pending"
            context.session.commit()
            continue
        if start_index <= 4:
            context.start_step(f"{store_listing.store_id}:stock")
            if store_listing.stock_status == "completed":
                context.complete_step(f"{store_listing.store_id}:stock", details={"idempotent_skip": True})
                if stop_after == "stock":
                    continue
            else:
                warehouse_id = int(store_config.get("warehouse_id") or 0)
                stock = int(store_config.get("stock") or 0)
                if warehouse_id <= 0 or stock <= 0:
                    store_listing.stock_status = "pending_configuration"
                    context.session.commit()
                    raise AppError(f"{prefix} 尚未配置仓库和库存", code="stock_configuration_missing")
                adapter.update_stock(store_listing.offer_id, warehouse_id, stock)
                store_listing.stock_status = "completed"
                store_listing.status = "promotion_pending"
                context.session.commit()
                context.complete_step(f"{store_listing.store_id}:stock")
        if stop_after == "stock":
            continue
        if start_index <= 5:
            context.start_step(f"{store_listing.store_id}:promotion")
            if store_listing.promotion_status == "completed":
                store_listing.status = "completed"
                context.session.commit()
                context.complete_step(f"{store_listing.store_id}:promotion", details={"idempotent_skip": True})
                continue
            action_id = int(store_config.get("promotion_action_id") or 0)
            product_id = int(store_config.get("ozon_product_id") or store_listing.external_product_id or 0)
            price = (loads(store_listing.pricing_json, {}) or loads(listing.pricing_json, {})).get("action_price")
            if not action_id or not product_id or not price:
                store_listing.promotion_status = "pending_eligibility"
                store_listing.status = "promotion_pending"
                context.session.commit()
                context.complete_step(f"{store_listing.store_id}:promotion", details={"pending": "eligibility_or_configuration"})
            else:
                candidate = adapter.find_promotion_candidate(action_id, product_id)
                if not candidate:
                    store_listing.promotion_status = "pending_eligibility"
                    store_listing.status = "promotion_pending"
                    context.session.commit()
                    context.complete_step(f"{store_listing.store_id}:promotion", details={"pending": "not_eligible"})
                    continue
                adapter.activate_promotion(action_id, product_id, price, int(store_config.get("stock") or 1))
                store_listing.promotion_status = "completed"
                store_listing.status = "completed"
                context.session.commit()
                context.complete_step(f"{store_listing.store_id}:promotion")
    listing.status = "completed" if targets and all(item.status == "completed" for item in targets) else "in_progress"
    context.session.commit()
