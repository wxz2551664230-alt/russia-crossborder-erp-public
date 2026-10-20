from __future__ import annotations

import uuid
import requests
from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from erp.core.database import get_db
from erp.core.errors import ConflictError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.models import CatalogProduct, Job, Listing, ResearchProduct, SourcingCandidate, Store, StoreListing, SystemSetting
from erp.schemas import ImportRecoveryRequest, ListingCreate, ListingRunRequest, ListingUpdate
from erp.serializers import job_dict, listing_dict, store_listing_dict
from erp.services.config_resolution import build_job_snapshot
from erp.services.jobs import enqueue_job
from erp.services.listings import PIPELINE_STEPS
from erp.modules.ozon_follow.legacy.services.ai_listing import pending_main_image_path
from erp.modules.ozon_follow.legacy.services.ozon_listing import OzonListingDraft
from erp.modules.ozon_follow.legacy.services.pricing import calculate_actual_sale_for_target_cost_margin
from erp.services.listing_pricing import auto_price_listing, effective_pricing_package, source_landed_cost


router = APIRouter(prefix="/listings", tags=["listings"])


def _get(session: Session, listing_id: str) -> Listing:
    item = session.scalar(
        select(Listing).where(Listing.id == listing_id).options(
            selectinload(Listing.research_product),
            selectinload(Listing.catalog_product),
            selectinload(Listing.store_listings).selectinload(StoreListing.store),
        )
    )
    if not item:
        raise NotFoundError("上架草稿不存在")
    return item


@router.get("")
def list_listings(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    status: str = "",
    session: Session = Depends(get_db),
) -> dict:
    conditions = [Listing.status == status] if status else []
    total = int(session.scalar(select(func.count()).select_from(Listing).where(*conditions)) or 0)
    items = session.scalars(
        select(Listing).where(*conditions).order_by(Listing.updated_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
        .options(selectinload(Listing.store_listings).selectinload(StoreListing.store))
    ).all()
    return {"items": [listing_dict(item) for item in items], "page": page, "page_size": page_size, "total": total}


@router.get("/{listing_id}")
def listing_detail(listing_id: str, session: Session = Depends(get_db)) -> dict:
    return listing_dict(_get(session, listing_id))


@router.post("/{listing_id}/stores/{store_id}/recover-import")
def recover_uncertain_import(listing_id: str, store_id: str, request: ImportRecoveryRequest, session: Session = Depends(get_db)) -> dict:
    item = _get(session, listing_id)
    child = next((row for row in item.store_listings if row.store_id == store_id), None)
    if not child:
        raise NotFoundError("店铺上架版本不存在")
    if child.status not in {"import_uncertain", "import_pending", "import_failed"}:
        raise ConflictError("只有导入结果不确定、等待中或失败的店铺版本可以绑定 task_id")
    child.external_task_id = str(request.external_task_id)
    child.status = "import_pending"
    child.last_error = ""
    session.commit()
    return store_listing_dict(child)

@router.post("/{listing_id}/stores/{store_id}/reset-import")
def reset_failed_import(listing_id: str, store_id: str, session: Session = Depends(get_db)) -> dict:
    item = _get(session, listing_id)
    child = next((row for row in item.store_listings if row.store_id == store_id), None)
    if not child:
        raise NotFoundError("店铺上架版本不存在")
    if child.status != "import_failed":
        raise ConflictError("只有明确导入失败的店铺版本可以重新提交")
    child.external_task_id = ""
    child.external_product_id = ""
    child.status = "ready"
    child.last_error = ""
    session.commit()
    return store_listing_dict(child)


@router.post("/{listing_id}/stores/{store_id}/manual-complete")
def manually_complete_store_listing(listing_id: str, store_id: str, session: Session = Depends(get_db)) -> dict:
    item = _get(session, listing_id)
    child = next((row for row in item.store_listings if row.store_id == store_id), None)
    if not child:
        raise NotFoundError("店铺上架版本不存在")
    allowed = {
        "stock_pending", "IMPORT_COMPLETED_STOCK_PENDING",
        "promotion_pending", "STOCK_COMPLETED_PROMOTION_PENDING", "PUBLISHED",
    }
    if child.status not in allowed:
        raise ConflictError("只有待添加库存或待添加促销的店铺版本可以手动移入已完成")
    previous_status = child.status
    completed_at = datetime.now(timezone.utc).isoformat()
    audit = loads(child.settings_snapshot_json, {})
    audit["manual_completion"] = {
        "from_status": previous_status,
        "completed_at": completed_at,
        "skipped_stock": previous_status in {"stock_pending", "IMPORT_COMPLETED_STOCK_PENDING"},
        "skipped_promotion": True,
    }
    child.settings_snapshot_json = dumps(audit)
    child.stock_status = "completed"
    child.promotion_status = "completed"
    child.status = "completed"
    child.last_error = ""
    enabled_children = [row for row in item.store_listings if row.store.enabled]
    item.status = "completed" if enabled_children and all(row.status == "completed" for row in enabled_children) else "in_progress"
    session.commit()
    return {**store_listing_dict(child), "manual_completion": audit["manual_completion"]}


@router.post("", status_code=201)
def create_listing(request: ListingCreate, session: Session = Depends(get_db)) -> dict:
    product = session.get(ResearchProduct, request.research_product_id) if request.research_product_id else None
    catalog = session.get(CatalogProduct, request.catalog_product_id) if request.catalog_product_id else None
    if not product and not catalog:
        raise NotFoundError("商品来源不存在")
    stores = session.scalars(select(Store).where(Store.id.in_(request.store_ids), Store.enabled.is_(True))).all()
    if len(stores) != len(set(request.store_ids)):
        raise ConflictError("部分店铺不存在或已停用")
    listing = Listing(
        research_product_id=product.id if product else None,
        catalog_product_id=catalog.id if catalog else None,
        base_offer_id=request.base_offer_id.strip(),
        title=product.title if product else catalog.name,
        # OZON submission requires package dimensions; source listings often do
        # not provide reliable values, so use the agreed operational default.
        package_json=dumps({"depth_mm": 300, "width_mm": 200, "height_mm": 100, "weight_g": product.weight_grams if product else None, "dimensions_source": "operational_default"}),
        image_policy_json=dumps({"catalog_primary_store_id": request.catalog_primary_store_id or request.store_ids[0]}),
    )
    session.add(listing)
    session.flush()
    for store in stores:
        offer_id = f"{store.offer_prefix}{request.base_offer_id}" if store.offer_prefix else request.base_offer_id
        session.add(StoreListing(
            listing_id=listing.id,
            store_id=store.id,
            offer_id=offer_id,
            pricing_json=dumps({"currency": store.currency}),
            idempotency_key=f"listing:{listing.id}:{store.id}",
        ))
    if product:
        product.status = "listing_draft"
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise ConflictError("Offer ID 已存在或该商品已经建立过相同草稿") from error
    auto_price_listing(session, listing)
    session.commit()
    return listing_dict(_get(session, listing.id))


@router.patch("/{listing_id}")
def update_listing(listing_id: str, request: ListingUpdate, session: Session = Depends(get_db)) -> dict:
    item = _get(session, listing_id)
    values = request.model_dump(exclude_unset=True)
    child = next((row for row in item.store_listings if row.store_id == values.get("store_id")), None) if values.get("store_id") else None
    if values.get("store_id") and not child:
        raise NotFoundError("店铺上架版本不存在")
    if not child and "title" in values:
        item.title = values["title"] or ""
    if not child and "description_ru" in values:
        item.description_ru = values["description_ru"] or ""
    for input_key, model_key in (
        ("asset_ids", "asset_ids_json"), ("package", "package_json"),
    ):
        if input_key in values:
            setattr(item, model_key, dumps(values[input_key] or ([] if input_key in {"tags", "attributes", "complex_attributes", "asset_ids"} else {})))
    for input_key, model_key in (("attributes", "attributes_json"), ("complex_attributes", "complex_attributes_json")):
        if input_key in values:
            setattr(child or item, model_key, dumps(values[input_key] or []))
    if child and any(key in values for key in ("title", "description_ru", "tags")):
        content = loads(child.content_json, {})
        if "title" in values: content["title"] = values["title"] or ""
        if "description_ru" in values: content["description_ru"] = values["description_ru"] or ""
        if "tags" in values: content["tags"] = values["tags"] or []
        child.content_json = dumps(content)
    elif not child and "tags" in values:
        item.tags_json = dumps(values["tags"] or [])
    if "pricing" in values:
        targets = [child] if child and child.store.enabled else ([row for row in item.store_listings if row.store.enabled] if not child else [])
        if not child:
            item.pricing_json = dumps(values["pricing"] or {})
        for target in targets:
            merged = {**loads(target.pricing_json, {}), **(values["pricing"] or {})}
            target.pricing_json = dumps(merged)
    item.status = "draft"
    item.blockers_json = "[]"
    session.commit()
    return listing_dict(_get(session, listing_id))


@router.post("/{listing_id}/price")
def calculate_price(listing_id: str, session: Session = Depends(get_db)) -> dict:
    item = _get(session, listing_id)
    if not item.research_product_id:
        raise ConflictError("自动定价当前只支持跟卖选品商品")
    source = session.scalar(select(SourcingCandidate).where(
        SourcingCandidate.research_product_id == item.research_product_id,
        SourcingCandidate.selected.is_(True),
    ))
    if not source or source.unit_price_cny is None:
        raise ConflictError("请先完成人工货源核验并确认准确的 SKU 拿货价")
    profile = loads(session.get(SystemSetting, "pricing").value_json, {}) if session.get(SystemSetting, "pricing") else {}
    package = effective_pricing_package(item)
    results = []
    try:
      for child in item.store_listings:
        if not child.store.enabled:
          continue
        store_profile = {**profile, **(loads(child.store.config_json, {}).get("ozon_follow_pricing") or {})}
        result = calculate_actual_sale_for_target_cost_margin(
            target_cost_profit_margin=store_profile.get("target_cost_profit_margin") or 30,
            purchase_price=source_landed_cost(source),
            length_mm=package.get("depth_mm"), width_mm=package.get("width_mm"),
            height_mm=package.get("height_mm"), weight_g=package.get("weight_g"),
            category_module=store_profile.get("pricing_category_module", ""),
            product_category=store_profile.get("pricing_product_category", ""),
            sticker_fee=store_profile.get("pricing_sticker_fee") or 2.5,
            advertising_cost=store_profile.get("pricing_operating_cost") or 3,
            advertising_rate=store_profile.get("pricing_advertising_rate") or 0,
            return_rate=store_profile.get("pricing_return_rate") or 10,
            logistics_service_rate=store_profile.get("pricing_logistics_service_rate") or 2,
            price_multiplier=store_profile.get("pricing_price_multiplier") or 3,
            old_price_multiplier=store_profile.get("pricing_old_price_multiplier") or 3.7,
        )
        results.append((child, result))
    except ValueError as error:
        raise ConflictError(str(error)) from error
    for child, result in results:
        details = {key: str(value) for key, value in asdict(result).items()}
        pricing = {"price": details["ozon_price"], "old_price": details["ozon_old_price"], "action_price": details["actual_sale"], "currency": "CNY", "calculation": details}
        child.pricing_json = dumps({**loads(child.pricing_json, {}), **pricing})
    if results:
        item.pricing_json = results[0][0].pricing_json
    session.commit()
    return listing_dict(_get(session, listing_id))


@router.post("/{listing_id}/ai-prepare", status_code=202)
def prepare_with_ai(listing_id: str, session: Session = Depends(get_db)) -> dict:
    item = _get(session, listing_id)
    enabled_children = [child for child in item.store_listings if child.store.enabled]
    if not enabled_children:
        raise ConflictError("没有启用并参与跟卖的 OZON 店铺")
    active = session.scalars(select(Job).where(
        Job.type == "ozon_ai_prepare",
        Job.status.in_(["queued", "running", "retrying", "paused"]),
    )).all()
    for existing in active:
        if loads(existing.payload_json, {}).get("listing_id") == item.id:
            return job_dict(existing)
    steps = [(f"{child.store_id}:ai_prepare", f"{child.store.name} · 生成俄文内容和主图") for child in enabled_children]
    job = enqueue_job(
        session, job_type="ozon_ai_prepare", payload={"listing_id": item.id},
        config_snapshot=build_job_snapshot(session), idempotency_key=f"ozon_ai_prepare:{item.id}:{uuid.uuid4().hex}",
        priority=20,
        steps=steps, max_attempts=3,
    )
    item.status = "ai_preparing"; session.commit()
    return job_dict(job)


@router.post("/{listing_id}/stores/{store_id}/image/{action}", status_code=202)
def image_action(listing_id: str, store_id: str, action: str, session: Session = Depends(get_db)) -> dict:
    if action not in {"approve", "regenerate"}: raise ConflictError("主图操作只能是 approve 或 regenerate")
    item=_get(session,listing_id); child=next((x for x in item.store_listings if x.store_id==store_id),None)
    if not child: raise NotFoundError("店铺上架版本不存在")
    if not child.store.enabled: raise ConflictError("该店铺未启用，不参与 OZON 跟卖流程")
    active_jobs = session.scalars(select(Job).where(
        Job.type.in_(["ozon_ai_prepare", "ozon_image_review_action"]),
        Job.status.in_(["queued", "running", "retrying", "paused"]),
    )).all()
    for active in active_jobs:
        payload = loads(active.payload_json, {})
        selected_stores = payload.get("store_ids") or []
        affects_store = not selected_stores or store_id in selected_stores
        if payload.get("listing_id") != item.id or not affects_store:
            continue
        if active.type == "ozon_ai_prepare":
            store_step = next(
                (step for step in active.steps if step.key == f"{store_id}:ai_prepare"),
                None,
            )
            if store_step and store_step.status == "completed":
                continue
            raise ConflictError("该店铺版本仍在执行 AI 准备，请完成后再核验主图")
        if payload.get("action") == action:
            queued_status = "image_approval_queued" if action == "approve" else "image_regeneration_queued"
            if child.status != queued_status:
                child.status = queued_status
                child.last_error = ""
                session.commit()
            return job_dict(active)
        raise ConflictError("该店铺版本已有另一项主图操作正在执行，请完成后再操作")
    if child.status != "image_review":
        raise ConflictError("只有等待人工核验的店铺版本可以批准或重新生成主图")
    job=enqueue_job(session,job_type="ozon_image_review_action",payload={"listing_id":item.id,"store_ids":[store_id],"action":action},config_snapshot=build_job_snapshot(session,store_id=store_id),idempotency_key=f"image:{action}:{child.id}:{uuid.uuid4().hex}",steps=[(f"{store_id}:image_{action}",f"{child.store.name} · {'批准' if action=='approve' else '重新生成'}主图")],max_attempts=3)
    child.status = "image_approval_queued" if action == "approve" else "image_regeneration_queued"
    child.last_error = ""
    session.commit()
    return job_dict(job)


@router.delete("/{listing_id}/stores/{store_id}/image-review")
def reject_store_image(listing_id: str, store_id: str, session: Session = Depends(get_db)) -> dict:
    item = _get(session, listing_id)
    child = next((row for row in item.store_listings if row.store_id == store_id), None)
    if not child:
        raise NotFoundError("店铺上架版本不存在")
    if child.status != "image_review":
        raise ConflictError("只有等待人工核验的店铺版本可以不通过并移除")
    if child.external_task_id or child.external_product_id:
        raise ConflictError("该店铺版本已经提交 OZON，不能从核验页直接移除")
    active_jobs = session.scalars(select(Job).where(
        Job.type.in_(["ozon_ai_prepare", "ozon_image_review_action"]),
        Job.status.in_(["queued", "running", "retrying", "paused"]),
    )).all()
    for active in active_jobs:
        payload = loads(active.payload_json, {})
        if payload.get("listing_id") != item.id:
            continue
        selected_stores = payload.get("store_ids") or []
        if selected_stores and store_id not in selected_stores:
            continue
        if active.type == "ozon_ai_prepare":
            step = next((step for step in active.steps if step.key == f"{store_id}:ai_prepare"), None)
            if step and step.status == "completed":
                continue
        raise ConflictError("该店铺版本仍有图片任务正在执行，请完成后再移除")
    remaining = [row for row in item.store_listings if row.id != child.id]
    session.delete(child)
    if not remaining:
        session.delete(item)
    else:
        item.status = "completed" if all(row.status == "completed" for row in remaining) else "in_progress"
    session.commit()
    return {"removed": True, "store_id": store_id, "listing_removed": not remaining, "local_only": True}


@router.get("/{listing_id}/stores/{store_id}/image-preview")
def image_preview(listing_id: str, store_id: str, session: Session = Depends(get_db)):
    item=_get(session,listing_id);child=next((x for x in item.store_listings if x.store_id==store_id),None)
    if not child: raise NotFoundError("店铺上架版本不存在")
    preparation=loads(child.assets_json,{}).get("ai_preparation") or {}
    draft=OzonListingDraft(source_sku=item.research_product.source_sku if item.research_product else item.id,offer_id=child.offer_id,source_ozon_url="",title=item.title,exported_category="",ai_preparation=preparation)
    try:path=pending_main_image_path(draft)
    except ValueError as error:raise NotFoundError(str(error)) from error
    # The same preview URL can point to a newly regenerated candidate. Avoid a
    # browser retaining either an old image or a transient failed response.
    return FileResponse(path, headers={"Cache-Control": "no-store, max-age=0"})


@router.get("/{listing_id}/source-image")
def source_image(listing_id: str, session: Session = Depends(get_db)):
    item = _get(session, listing_id)
    source = item.research_product
    candidates = loads(source.image_urls_json, []) if source else []
    url = str((candidates or ([source.image_url] if source else []))[0] or "").strip()
    if not url.startswith("http"):
        raise NotFoundError("来源商品没有可用原图")
    try:
        remote = requests.get(url, timeout=(10, 45), headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.ozon.ru/"})
        remote.raise_for_status()
    except requests.RequestException as error:
        raise NotFoundError("来源商品原图暂时无法读取") from error
    media_type = remote.headers.get("content-type", "image/jpeg").split(";", 1)[0]
    return Response(content=remote.content, media_type=media_type, headers={"Cache-Control": "public, max-age=3600"})


@router.post("/{listing_id}/run", status_code=202)
def run_listing(listing_id: str, request: ListingRunRequest, session: Session = Depends(get_db)) -> dict:
    item = _get(session, listing_id)
    selected = set(request.store_ids or [])
    targets = [child for child in item.store_listings if child.store.enabled and (not selected or child.store_id in selected)]
    if not targets:
        raise ConflictError("没有可执行的目标店铺")
    target_store_ids = {child.store_id for child in targets}
    active_jobs = session.scalars(select(Job).where(
        Job.type == "ozon_listing_pipeline",
        Job.status.in_(["queued", "running", "retrying", "paused"]),
    )).all()
    for active in active_jobs:
        payload = loads(active.payload_json, {})
        if payload.get("listing_id") != item.id or payload.get("start_at") != request.start_at:
            continue
        active_store_ids = set(payload.get("store_ids") or [])
        if not target_store_ids.intersection(active_store_ids):
            continue
        if (
            target_store_ids == active_store_ids
            and bool(payload.get("dry_run", True)) == request.dry_run
        ):
            # A slow Ozon import/poll must look like one operation to repeated UI
            # requests. Returning the existing job also keeps its live progress.
            return job_dict(active)
        raise ConflictError("所选店铺的该步骤已有任务正在执行，请等待当前任务完成")
    overrides = {key: value for key, value in {
        "warehouse_id": request.warehouse_id, "stock": request.stock,
        "promotion_action_id": request.promotion_action_id, "ozon_product_id": request.ozon_product_id,
    }.items() if value is not None}
    snapshots = {child.store_id: build_job_snapshot(session, store_id=child.store_id, overrides=overrides) for child in targets}
    if request.action_price is not None:
        for child in targets:
            child.pricing_json = dumps({**loads(child.pricing_json, {}), "action_price": request.action_price})
    step_order = [key for key, _name in PIPELINE_STEPS]
    start_index = step_order.index(request.start_at)
    stop_after = {"submit": "product_sync", "stock": "stock", "promotion": "promotion"}.get(request.start_at)
    stop_index = step_order.index(stop_after) if stop_after else len(step_order) - 1
    steps = [
        (f"{child.store_id}:{key}", f"{child.store.name} · {name}")
        for child in targets for index, (key, name) in enumerate(PIPELINE_STEPS)
        if start_index <= index <= stop_index
    ]
    operation = "dry" if request.dry_run else "live"
    job = enqueue_job(
        session,
        job_type="ozon_listing_pipeline",
        payload={"listing_id": item.id, "store_ids": [child.store_id for child in targets], "start_at": request.start_at, "stop_after": stop_after, "dry_run": request.dry_run},
        config_snapshot=dumps({"stores": snapshots}),
        idempotency_key=f"listing_pipeline:{item.id}:{operation}:{request.start_at}:{uuid.uuid4().hex}",
        steps=steps,
        max_attempts=1 if not request.dry_run else 3,
    )
    item.status = "queued"
    session.commit()
    return job_dict(job)
