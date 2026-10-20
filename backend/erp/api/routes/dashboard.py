from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from erp.core.database import get_db
from erp.models import Asset, IntegrationConnection, Job, Listing, ResearchProduct, Store, StoreListing
from erp.serializers import listing_dict
from erp.services.legacy_migration import inspect_legacy_sources
from erp.core.config import get_settings
from erp.core.jsonutil import loads


router = APIRouter(tags=["dashboard"])

FLOW_STAGES = [
    ("imported", "导入选品"), ("category", "确认类目"), ("source", "确认货源"),
    ("content", "AI 文案"), ("image", "生图核验"), ("price", "定价"),
    ("submit", "提交 OZON"), ("stock", "添加库存"), ("promotion", "加入促销"), ("completed", "完成"),
]


def _flow_state(listing: Listing, *, include_detail: bool = True) -> dict:
    product = listing.research_product
    children = [child for child in listing.store_listings if child.store.enabled]
    done = {"imported"}
    if product and product.description_category_id and product.type_id: done.add("category")
    if product and any(candidate.selected for candidate in product.sourcing_candidates): done.add("source")
    if children and all(loads(child.content_json, {}).get("title") and loads(child.content_json, {}).get("description_ru") for child in children): done.add("content")
    if children and all(loads(child.assets_json, {}).get("primary_image") and loads(child.assets_json, {}).get("ai_preparation", {}).get("image_review_status") == "APPROVED" for child in children): done.add("image")
    if listing.pricing_json not in {"", "{}"}: done.add("price")
    if children and all(child.external_product_id and child.status in {"stock_pending", "promotion_pending", "completed"} for child in children): done.add("submit")
    if children and all(child.stock_status in {"completed", "skipped"} for child in children): done.add("stock")
    if children and all(child.promotion_status in {"completed", "skipped"} for child in children): done.add("promotion")
    if children and all(child.status == "completed" for child in children): done.add("completed")
    steps = [{"key": key, "name": name, "completed": key in done} for key, name in FLOW_STAGES]
    current = next((step for step in steps if not step["completed"]), steps[-1])
    return {"listing": listing_dict(listing) if include_detail else None, "steps": steps, "current": current, "progress": round(len(done) * 100 / len(FLOW_STAGES)), "store_progress": {"completed": sum(child.status == "completed" for child in children), "total": len(children)}}


@router.get("/ozon-follow/automation")
def ozon_follow_automation(session: Session = Depends(get_db), page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), stage: str = "") -> dict:
    listings = session.scalars(select(Listing).order_by(Listing.updated_at.desc()).options(
        selectinload(Listing.research_product).selectinload(ResearchProduct.sourcing_candidates),
        selectinload(Listing.store_listings).selectinload(StoreListing.store),
    )).all()
    states = [(item, _flow_state(item, include_detail=False)) for item in listings]
    filtered = [(item, flow) for item, flow in states if not stage or flow["current"]["key"] == stage]
    items = []
    for item, flow in filtered[(page - 1) * page_size:page * page_size]:
        flow = dict(flow); flow["listing"] = listing_dict(item); items.append(flow)
    return {"stages": [{"key": key, "name": name, "count": sum(flow["current"]["key"] == key for _, flow in states)} for key, name in FLOW_STAGES], "items": items, "pagination": {"page": page, "page_size": page_size, "total": len(filtered)}}


@router.get("/bootstrap")
def bootstrap(session: Session = Depends(get_db)) -> dict:
    def count(model) -> int:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)

    active_jobs = int(session.scalar(
        select(func.count()).select_from(Job).where(Job.status.in_(["queued", "running", "paused", "retrying"]))
    ) or 0)
    failed_jobs = int(session.scalar(
        select(func.count()).select_from(Job).where(Job.status == "failed")
    ) or 0)
    pending_products = int(session.scalar(
        select(func.count()).select_from(ResearchProduct).where(ResearchProduct.status != "completed")
    ) or 0)
    return {
        "application": {"name": "俄罗斯跨境 ERP", "version": "0.1.0", "mode": get_settings().runtime_mode},
        "counts": {
            "connections": count(IntegrationConnection),
            "stores": count(Store),
            "research_products": count(ResearchProduct),
            "pending_products": pending_products,
            "listings": count(Listing),
            "assets": count(Asset),
            "active_jobs": active_jobs,
            "failed_jobs": failed_jobs,
        },
        "legacy_sources": inspect_legacy_sources(),
    }
