from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, Form, Query, UploadFile
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from erp.core.config import get_settings
from erp.core.database import get_db
from erp.core.errors import ConflictError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.core.security import vault
from erp.models import IntegrationConnection, Job, Listing, ResearchProduct, SourcingCandidate, StoreListing
from erp.schemas import CategoryConfirmation, SourcingCandidateCreate, SourcingRunRequest
from erp.serializers import candidate_dict, job_dict, research_product_dict
from erp.services.assets import register_local_asset
from erp.services.config_resolution import build_job_snapshot
from erp.services.jobs import enqueue_job
from erp.services.listing_pricing import auto_price_listing
from erp.modules.ozon_follow.legacy.services.alibaba_client import AlibabaNewtonClient


router = APIRouter(prefix="/research-products", tags=["research-products"])


def _get(session: Session, product_id: str) -> ResearchProduct:
    item = session.scalar(
        select(ResearchProduct).where(ResearchProduct.id == product_id).options(selectinload(ResearchProduct.sourcing_candidates))
    )
    if not item:
        raise NotFoundError("选品商品不存在")
    return item


@router.get("")
def list_products(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str = Query(default="", max_length=200),
    status: str = Query(default="", max_length=50),
    session: Session = Depends(get_db),
) -> dict:
    conditions = []
    if search.strip():
        token = f"%{search.strip()}%"
        conditions.append(or_(ResearchProduct.source_sku.ilike(token), ResearchProduct.title.ilike(token), ResearchProduct.category.ilike(token)))
    if status:
        conditions.append(ResearchProduct.status == status)
    total = int(session.scalar(select(func.count()).select_from(ResearchProduct).where(*conditions)) or 0)
    items = session.scalars(
        select(ResearchProduct).where(*conditions).order_by(
            ResearchProduct.source_file.asc().nulls_last(),
            ResearchProduct.source_row.asc().nulls_last(),
            ResearchProduct.created_at.asc(),
            ResearchProduct.id.asc(),
        )
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {"items": [research_product_dict(item) for item in items], "page": page, "page_size": page_size, "total": total}


@router.get("/{product_id}")
def product_detail(product_id: str, session: Session = Depends(get_db)) -> dict:
    item = _get(session, product_id)
    value = research_product_dict(item, include_raw=True)
    value["sourcing_candidates"] = [candidate_dict(candidate) for candidate in sorted(item.sourcing_candidates, key=lambda row: row.rank or 999999)]
    return value


@router.delete("/{product_id}")
def delete_product(product_id: str, session: Session = Depends(get_db)) -> dict:
    item = _get(session, product_id)
    listing_ids = [listing.id for listing in session.scalars(select(Listing).where(Listing.research_product_id == item.id)).all()]
    active_jobs = session.scalars(select(Job).where(Job.status.in_(["queued", "running", "paused", "retrying"]))).all()
    for job in active_jobs:
        payload = loads(job.payload_json, {})
        if payload.get("research_product_id") == item.id or payload.get("listing_id") in listing_ids:
            raise ConflictError("该商品仍有运行中或已暂停任务，请先在异常处理/任务中心停止任务")
    for listing in session.scalars(select(Listing).where(Listing.research_product_id == item.id)).all():
        session.delete(listing)
    session.delete(item)
    session.commit()
    return {"deleted": True, "local_only": True, "note": "仅删除 ERP 本地记录，未删除 OZON、A4 产品库或原始图片"}


@router.post("/imports/seerfar", status_code=202)
def import_seerfar(
    file: UploadFile = File(...),
    description_category_id: int = Form(...), type_id: int = Form(...), category_display: str = Form(...),
    session: Session = Depends(get_db),
) -> dict:
    suffix = Path(file.filename or "").suffix.casefold()
    if suffix not in {".xlsx", ".xlsm"}:
        raise ConflictError("请选择 Seerfar 导出的 .xlsx 或 .xlsm 文件")
    import_dir = get_settings().data_dir / "imports"
    import_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    temporary = import_dir / f"upload-{os.getpid()}-{id(file)}{suffix}.tmp"
    with temporary.open("wb") as target:
        while chunk := file.file.read(1024 * 1024):
            digest.update(chunk)
            target.write(chunk)
    sha256 = digest.hexdigest()
    final_path = import_dir / f"seerfar-{sha256[:16]}{suffix}"
    if final_path.exists():
        temporary.unlink(missing_ok=True)
    else:
        os.replace(temporary, final_path)
    asset = register_local_asset(
        session, final_path, kind="seerfar_workbook", source="upload", read_only=False,
        calculate_hash=True, metadata={"original_name": file.filename or final_path.name},
    )
    session.commit()
    job = enqueue_job(
        session,
        job_type="seerfar_import",
        payload={"asset_id": asset.id, "description_category_id": description_category_id, "type_id": type_id, "category_display": category_display},
        config_snapshot=build_job_snapshot(session),
        idempotency_key=f"seerfar_import:{sha256}",
        steps=[("read_workbook", "读取 Seerfar 表格"), ("upsert_products", "保存选品商品")],
    )
    return {"job": job_dict(job), "asset_id": asset.id}


@router.put("/{product_id}/category")
def confirm_category(product_id: str, request: CategoryConfirmation, session: Session = Depends(get_db)) -> dict:
    item = _get(session, product_id)
    item.description_category_id = request.description_category_id
    item.type_id = request.type_id
    item.category_display = request.display.strip()
    item.status = "category_confirmed" if not item.sourcing_candidates else item.status
    session.commit()
    session.refresh(item)
    return research_product_dict(item)


@router.post("/{product_id}/sourcing", status_code=202)
def start_sourcing(product_id: str, request: SourcingRunRequest, session: Session = Depends(get_db)) -> dict:
    _get(session, product_id)
    raise ConflictError("OZON 跟卖已停用自动找源，请按旧程序流程人工打开 1688 搜图并确认准确 SKU")


def _extract_variants(payload) -> list[dict]:
    found: list[dict] = []
    def walk(value):
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and (item.get("skuId") or item.get("sku_id") or item.get("specId") or item.get("spec_id")):
                    sku_id = str(item.get("skuId") or item.get("sku_id") or item.get("specId") or item.get("spec_id") or "").strip()
                    attributes = item.get("attributes") or item.get("specAttrs") or item.get("skuAttributes") or []
                    parts = []
                    for attribute in attributes if isinstance(attributes, list) else []:
                        if isinstance(attribute, dict):
                            name = str(attribute.get("attributeName") or attribute.get("name") or attribute.get("key") or "").strip()
                            text = str(attribute.get("attributeValue") or attribute.get("value") or attribute.get("valueName") or "").strip()
                            if text: parts.append(f"{name}:{text}" if name else text)
                    sku_name = str(item.get("skuName") or item.get("specName") or "").strip() or ";".join(parts)
                    price = item.get("price") or item.get("retailPrice") or item.get("discountPrice") or ""
                    found.append({"sku_id":sku_id,"sku_name":sku_name,"price":str(price or "")})
                walk(item)
        elif isinstance(value, dict):
            for nested in value.values(): walk(nested)
    walk(payload)
    return list({item["sku_id"]:item for item in found if item["sku_id"]}.values())


@router.post("/{product_id}/manual-source/variants")
def read_manual_source_variants(product_id: str, payload: dict = Body(...), session: Session = Depends(get_db)) -> dict:
    _get(session, product_id)
    source_url = str(payload.get("source_url") or "").strip()
    item_id = str(payload.get("item_id") or "").strip()
    if source_url:
        match = re.search(r"/offer/(\d+)", source_url, re.I) or re.search(r"[?&](?:offerId|itemId)=(\d+)", source_url, re.I)
        if not match: raise ConflictError("产品链接必须是包含商品 ID 的 1688 商品链接")
        item_id = match.group(1)
    if not re.fullmatch(r"\d{6,}", item_id): raise ConflictError("请先填写正确的 1688 商品链接或商品 ID")
    connection = session.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "alibaba", IntegrationConnection.enabled.is_(True)).order_by(IntegrationConnection.created_at))
    if not connection: raise ConflictError("尚未配置人工读取 1688 变体所需的 Alibaba 接口；仍可手工填写规格")
    secrets = vault.decrypt(connection.secrets_encrypted)
    client = AlibabaNewtonClient(str(secrets.get("app_key") or ""),str(secrets.get("app_secret") or ""),str(secrets.get("access_token") or ""))
    variants = _extract_variants(client.get_cross_product_info(item_id))
    if not variants: raise ConflictError("1688 商品详情没有返回可选择的 SKU 变体")
    return {"item_id":item_id,"items":variants}


@router.post("/{product_id}/sourcing-candidates", status_code=201)
def add_candidate(product_id: str, request: SourcingCandidateCreate, session: Session = Depends(get_db)) -> dict:
    item = _get(session, product_id)
    external_sku_id = request.external_sku_id.strip()
    raw = dict(request.raw)
    if request.provider == "manual" and not external_sku_id:
        external_sku_id = f"MANUAL-{request.external_product_id.strip()}"
        raw["supplier_sku_id_pending"] = True
    candidate = session.scalar(select(SourcingCandidate).where(
        SourcingCandidate.research_product_id == item.id,
        SourcingCandidate.provider == request.provider,
        SourcingCandidate.external_product_id == request.external_product_id,
        SourcingCandidate.external_sku_id.in_([external_sku_id, ""]),
    ).order_by(SourcingCandidate.selected.desc(), SourcingCandidate.updated_at.desc()))
    if candidate:
        candidate.external_sku_id = external_sku_id
        candidate.title = request.title
        candidate.sku_name = request.sku_name
        candidate.image_url = request.image_url
        candidate.source_url = request.source_url
        candidate.unit_price_cny = request.unit_price_cny
        candidate.domestic_delivery_cny = request.domestic_delivery_cny
        candidate.landed_cost_cny = (request.unit_price_cny or 0) + (request.domestic_delivery_cny or 0) if request.unit_price_cny is not None else None
        candidate.minimum_order_quantity = request.minimum_order_quantity
        candidate.confidence = request.confidence
        candidate.raw_json = dumps({**loads(candidate.raw_json, {}), **raw, "skuName": request.sku_name, "title": request.title})
        session.commit()
        session.refresh(candidate)
        return candidate_dict(candidate)
    candidate = SourcingCandidate(
        research_product_id=item.id,
        provider=request.provider,
        external_product_id=request.external_product_id,
        external_sku_id=external_sku_id,
        title=request.title,
        sku_name=request.sku_name,
        image_url=request.image_url,
        source_url=request.source_url,
        unit_price_cny=request.unit_price_cny,
        domestic_delivery_cny=request.domestic_delivery_cny,
        landed_cost_cny=(request.unit_price_cny or 0) + (request.domestic_delivery_cny or 0) if request.unit_price_cny is not None else None,
        sales=request.sales,
        minimum_order_quantity=request.minimum_order_quantity,
        rank=request.rank,
        confidence=request.confidence,
        raw_json=dumps({**raw, "skuName": request.sku_name, "title": request.title}),
    )
    session.add(candidate)
    item.status = "pending_source_confirmation"
    session.commit()
    session.refresh(candidate)
    return candidate_dict(candidate)


@router.post("/{product_id}/sourcing-candidates/{candidate_id}/select")
def select_candidate(product_id: str, candidate_id: str, session: Session = Depends(get_db)) -> dict:
    item = _get(session, product_id)
    candidate = session.scalar(select(SourcingCandidate).where(
        SourcingCandidate.id == candidate_id, SourcingCandidate.research_product_id == item.id,
    ))
    if not candidate:
        raise NotFoundError("候选货源不存在")
    session.execute(update(SourcingCandidate).where(SourcingCandidate.research_product_id == item.id).values(selected=False, status="candidate"))
    candidate.selected = True
    candidate.status = "selected"
    item.status = "source_confirmed"
    if not candidate.external_sku_id:
        candidate.external_sku_id = f"MANUAL-{candidate.external_product_id}"
        candidate.raw_json = dumps({**loads(candidate.raw_json, {}), "supplier_sku_id_pending": True})
    session.flush()
    listings = session.scalars(
        select(Listing).where(Listing.research_product_id == item.id)
        .options(selectinload(Listing.store_listings).selectinload(StoreListing.store))
    ).all()
    jobs = []
    for listing in listings:
        auto_price_listing(session, listing)
        enabled_children = [child for child in listing.store_listings if child.store.enabled]
        steps = [(f"{child.store_id}:ai_prepare", f"{child.store.name} · 生成俄文内容和独立风格主图") for child in enabled_children]
        if not steps:
            continue
        listing.status = "ai_preparing"
        for child in enabled_children:
            if child.status in {"draft", "pending_sourcing", "source_confirmed"}:
                child.status = "ai_preparing"
        session.flush()
        job = enqueue_job(
            session,
            job_type="ozon_ai_prepare",
            payload={"listing_id": listing.id},
            config_snapshot=build_job_snapshot(session),
            idempotency_key=f"ozon_ai_prepare_after_source:{listing.id}:{candidate.id}",
            priority=20,
            steps=steps,
            max_attempts=2,
        )
        jobs.append(job_dict(job))
    session.commit()
    return {"product": research_product_dict(item), "candidate": candidate_dict(candidate), "jobs": jobs, "next_stage": "ai_preparing" if jobs else "source_confirmed"}
