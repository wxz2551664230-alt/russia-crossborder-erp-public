from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select, update
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, selectinload

from erp.core.config import get_settings
from erp.core.database import get_db
from erp.core.errors import ConflictError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.core.security import vault
from erp.models import Asset, CatalogDuplicateSuggestion, CatalogImportRun, CatalogProduct, CatalogProductAsset, CatalogVariant, IntegrationConnection, Job, Publication, PublicationStore, Store, new_id
from erp.schemas import CatalogAssetLinkCreate, CatalogAssetPrepareCreate, CatalogAssetUrlCreate, CatalogDuplicateUpdate, CatalogProductCreate, CatalogProductUpdate, CatalogVariantInput, LocalDirectoryPickerRequest, PodImportRequest, SelfListingPipelineCreate
from erp.serializers import catalog_duplicate_dict, catalog_import_dict, catalog_product_dict, catalog_variant_dict, job_dict
from erp.services.catalog import create_catalog_product, get_catalog_product, link_catalog_asset, product_query, update_catalog_product
from erp.services.config_resolution import build_job_snapshot
from erp.services.jobs import enqueue_job
from erp.services.ozon_categories import find_category_option


def _selected_category_report(
    session: Session,
    store_id: str,
    description_category_id: int | None,
    type_id: int | None,
) -> dict:
    if not store_id or not description_category_id or not type_id:
        raise ConflictError("回传入库前请选择已同步的 Ozon 类目")
    option = find_category_option(store_id, description_category_id, type_id)
    if not option:
        raise ConflictError("选择的 Ozon 类目不在本地快照中，请重新选择或同步类目树")
    return {
        "ozon_category": {
            "store_id": store_id,
            "description_category_id": int(option["description_category_id"]),
            "type_id": int(option["type_id"]),
            "display": str(option["display"]),
            "path": option.get("path") or [],
            "source": "manual_import",
        }
    }


def _delete_follow_oss_object(session: Session, url: str) -> str:
    connection = session.scalar(select(IntegrationConnection).where(
        IntegrationConnection.provider == "oss",
        IntegrationConnection.enabled.is_(True),
        IntegrationConnection.name == "OZON 自动上架 OSS",
    ))
    if not connection:
        raise ConflictError("未找到 OZON 自动上架 OSS 配置，无法安全删除云端图片")
    config = loads(connection.config_json, {})
    secrets = vault.decrypt(connection.secrets_encrypted) if connection.secrets_encrypted else {}
    bucket = str(config.get("bucket") or "").strip()
    region = str(config.get("region") or "").strip()
    endpoint = str(connection.base_url or config.get("endpoint") or "").strip()
    public_base = str(config.get("public_base_url") or "").rstrip("/")
    prefix = str(config.get("prefix") or "ozon-auto-listing").strip("/")
    access_key_id = str(secrets.get("access_key_id") or "").strip()
    access_key_secret = str(secrets.get("access_key_secret") or "").strip()
    parsed = urlparse(str(url or ""))
    expected_host = urlparse(public_base).hostname if public_base else None
    key = unquote(parsed.path.lstrip("/"))
    if not parsed.hostname or (expected_host and parsed.hostname.casefold() != expected_host.casefold()) or not key.startswith(prefix + "/"):
        raise ConflictError("该图片不属于 ERP 管理的 OZON OSS 目录，拒绝删除")
    if not all((bucket, region, access_key_id, access_key_secret)):
        raise ConflictError("OZON OSS 配置不完整，无法删除云端图片")
    try:
        import alibabacloud_oss_v2 as oss
        provider = oss.credentials.StaticCredentialsProvider(access_key_id, access_key_secret)
        normalized_endpoint = endpoint if "://" in endpoint else f"https://{endpoint}"
        client = oss.Client(oss.Config(region=region, endpoint=normalized_endpoint, credentials_provider=provider))
        client.delete_object(oss.DeleteObjectRequest(bucket=bucket, key=key))
    except Exception as error:
        raise ConflictError("OSS 图片删除失败，产品库关联未变更", details=str(error)) from error
    return key


router = APIRouter(prefix="/catalog", tags=["catalog"])


class ProductNotesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_notes: str = Field(max_length=6000)
    notes: str = Field(max_length=6000)


@router.put("/products/{product_id}/notes")
def save_product_notes(product_id: str, request: ProductNotesUpdate, session: Session = Depends(get_db)) -> dict:
    # One atomic comparison protects another person's edits; no costs, images,
    # publication state, or supplier facts are changed by this client operation.
    changed = session.execute(update(CatalogProduct).where(
        CatalogProduct.id == product_id, CatalogProduct.notes == request.expected_notes,
    ).values(notes=request.notes)).rowcount
    if not changed:
        session.rollback()
        item = session.get(CatalogProduct, product_id)
        if not item:
            raise NotFoundError("产品不存在")
        if item.notes != request.notes:
            raise ConflictError("备注已被其他人修改，请重新读取后再操作")
    else:
        session.commit()
    return {"id": product_id, "notes": request.notes, "saved": True}


@router.get("/summary")
def catalog_summary(source_type: str = Query(default="", max_length=30), session: Session = Depends(get_db)) -> dict:
    by_source = {str(source): int(count) for source, count in session.execute(
        select(CatalogProduct.source_type, func.count()).group_by(CatalogProduct.source_type)
    ).all()}
    product_ids = select(CatalogProduct.id)
    if source_type:
        product_ids = product_ids.where(CatalogProduct.source_type == source_type)
    product_ids = product_ids.scalar_subquery()
    scoped_total = by_source.get(source_type, 0) if source_type else sum(by_source.values())
    return {
        "total_products": scoped_total,
        "by_source": by_source,
        "variants": int(session.scalar(select(func.count()).select_from(CatalogVariant).where(CatalogVariant.product_id.in_(product_ids))) or 0),
        "current_assets": int(session.scalar(select(func.count()).select_from(CatalogProductAsset).where(CatalogProductAsset.is_current.is_(True), CatalogProductAsset.product_id.in_(product_ids))) or 0),
        "duplicate_suggestions": int(session.scalar(select(func.count()).select_from(CatalogDuplicateSuggestion).where(
            CatalogDuplicateSuggestion.status == "pending",
            or_(CatalogDuplicateSuggestion.product_id.in_(product_ids), CatalogDuplicateSuggestion.candidate_product_id.in_(product_ids)),
        )) or 0),
    }


@router.get("/products")
def list_catalog_products(
    include_variants: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str = Query(default="", max_length=200),
    source_type: str = Query(default="", max_length=30),
    status: str = Query(default="", max_length=40),
    category: str = Query(default="", max_length=800),
    pod_category: str = Query(default="", max_length=800),
    category_status: str = Query(default="", pattern="^(|assigned|unassigned)$"),
    image_status: str = Query(default="", pattern="^(|with_images|without_images)$"),
    ozon_store_id: str = Query(default="", max_length=32),
    ozon_state: str = Query(default="", pattern="^(|available|unlisted|listed|processing|failed)$"),
    sort: str = Query(default="updated_desc", pattern="^(updated_desc|sku_desc)$"),
    session: Session = Depends(get_db),
) -> dict:
    conditions = []
    if search.strip():
        token = f"%{search.strip()}%"
        conditions.append(or_(CatalogProduct.sku.ilike(token), CatalogProduct.group_code.ilike(token), CatalogProduct.name.ilike(token), CatalogProduct.source_url.ilike(token)))
    if source_type: conditions.append(CatalogProduct.source_type == source_type)
    if status: conditions.append(CatalogProduct.status == status)
    if category_status == "assigned": conditions.append(CatalogProduct.description_category_id.is_not(None))
    if category_status == "unassigned": conditions.append(CatalogProduct.description_category_id.is_(None))
    current_asset = CatalogProduct.asset_links.any(CatalogProductAsset.is_current.is_(True))
    if image_status == "with_images": conditions.append(current_asset)
    if image_status == "without_images": conditions.append(~current_asset)
    if ozon_state and not ozon_store_id: raise ConflictError("请先选择要核对的 Ozon 店铺")
    if ozon_store_id:
        store=session.get(Store,ozon_store_id)
        if not store or store.platform!='ozon': raise ConflictError("Ozon 筛选店铺无效")
        base=select(PublicationStore.id).join(Publication,Publication.id==PublicationStore.publication_id).where(Publication.product_id==CatalogProduct.id,PublicationStore.store_id==ozon_store_id)
        listed=base.where(or_(PublicationStore.external_product_id!='',PublicationStore.status.in_(['stock_pending','promotion_pending','completed']))).exists()
        active=base.where(PublicationStore.status.not_in(['completed','failed','blocked','ready_for_export','cancelled'])).exists()
        failed=base.where(PublicationStore.status.in_(['failed','blocked'])).exists()
        if ozon_state=='available':conditions.extend([~listed,~active])
        if ozon_state=='unlisted':conditions.append(~listed)
        if ozon_state=='listed':conditions.append(listed)
        if ozon_state=='processing':conditions.extend([~listed,active])
        if ozon_state=='failed':conditions.extend([~listed,~active,failed])
    facet_conditions = list(conditions)
    if pod_category and source_type == "pod": conditions.append(CatalogProduct.category == pod_category)
    if category: conditions.append(CatalogProduct.category_display == category)
    total = int(session.scalar(select(func.count()).select_from(CatalogProduct).where(*conditions)) or 0)
    ordering = (CatalogProduct.sku.desc(), CatalogProduct.id.asc()) if sort == "sku_desc" else (CatalogProduct.updated_at.desc(), CatalogProduct.id.asc())
    items = session.scalars(product_query().where(*conditions).order_by(*ordering).offset((page-1)*page_size).limit(page_size)).all()
    category_rows = session.execute(
        select(CatalogProduct.category_display, func.count()).where(*facet_conditions, CatalogProduct.category_display != "").group_by(CatalogProduct.category_display).order_by(CatalogProduct.category_display)
    ).all()
    pod_category_rows = session.execute(select(CatalogProduct.category, func.count()).where(*facet_conditions, CatalogProduct.source_type == "pod", CatalogProduct.category != "").group_by(CatalogProduct.category).order_by(CatalogProduct.category)).all() if source_type == "pod" else []
    return {
        "pod_category_options": [{"label": str(name), "count": int(count)} for name, count in pod_category_rows],
        "items": [{**catalog_product_dict(item, include_details=False), **({"variants": [catalog_variant_dict(v) for v in item.variants]} if include_variants else {})} for item in items],
        "page": page, "page_size": page_size, "total": total,
        "category_options": [{"label": str(name), "count": int(count)} for name, count in category_rows],
    }


@router.post("/products", status_code=201)
def add_catalog_product(request: CatalogProductCreate, session: Session = Depends(get_db)) -> dict:
    return catalog_product_dict(create_catalog_product(session, request))


@router.get("/products/{product_id}")
def catalog_product_detail(product_id: str, session: Session = Depends(get_db)) -> dict:
    return catalog_product_dict(get_catalog_product(session, product_id))


@router.patch("/products/{product_id}")
def edit_catalog_product(product_id: str, request: CatalogProductUpdate, session: Session = Depends(get_db)) -> dict:
    return catalog_product_dict(update_catalog_product(session, get_catalog_product(session, product_id), request))


@router.delete("/products/{product_id}")
def delete_catalog_product(product_id: str, session: Session = Depends(get_db)) -> dict:
    item = get_catalog_product(session, product_id)
    active = session.scalars(select(Job).where(Job.status.in_(["queued", "running", "paused", "retrying"]))).all()
    if any(str(product_id) in job.payload_json for job in active):
        raise ConflictError("该产品仍有进行中或已暂停的任务")
    if item.listings:
        raise ConflictError("该产品已有发布历史，请将状态改为已归档，以保留审计记录")
    session.delete(item); session.commit()
    return {"deleted": True, "assets_preserved": True, "note": "仅删除产品库关联，原始文件和 OSS 对象未删除"}


@router.put("/products/{product_id}/variants")
def replace_variants(product_id: str, variants: list[CatalogVariantInput], session: Session = Depends(get_db)) -> dict:
    item = get_catalog_product(session, product_id)
    if not variants: raise ConflictError("至少保留一个 SKU")
    existing = {row.id: row for row in item.variants}
    for value in variants:
        target = existing.pop(value.id, None) if value.id else None
        if value.id and not target: raise NotFoundError("部分 SKU 不属于该产品")
        if not target: target = CatalogVariant(product_id=item.id); session.add(target)
        target.sku=value.sku.strip();target.supplier_product_id=value.supplier_product_id.strip();target.supplier_sku_id=value.supplier_sku_id.strip();target.specification=value.specification.strip();target.color=value.color.strip();target.size=value.size.strip();target.purchase_price_cny=value.purchase_price_cny;target.domestic_delivery_cny=value.domestic_delivery_cny;target.package_length_mm=value.package_length_mm;target.package_width_mm=value.package_width_mm;target.package_height_mm=value.package_height_mm;target.weight_grams=value.weight_grams;target.barcode=value.barcode.strip();target.active=value.active;target.properties_json=dumps(value.properties)
    for old in existing.values(): session.delete(old)
    session.commit()
    return catalog_product_dict(get_catalog_product(session, product_id))


@router.post("/products/{product_id}/assets", status_code=201)
def add_product_asset(product_id: str, request: CatalogAssetLinkCreate, session: Session = Depends(get_db)) -> dict:
    product = get_catalog_product(session, product_id)
    asset = session.get(Asset, request.asset_id)
    if not asset: raise NotFoundError("资产不存在")
    link_catalog_asset(session,product=product,asset=asset,role=request.role,position=request.position,variant_id=request.variant_id,source_kind=request.source_kind,set_current=request.set_current,metadata=request.metadata)
    session.commit()
    return catalog_product_dict(get_catalog_product(session, product_id))


@router.post("/products/{product_id}/assets/url", status_code=201)
def add_product_asset_url(product_id: str, request: CatalogAssetUrlCreate, session: Session = Depends(get_db)) -> dict:
    product = get_catalog_product(session, product_id)
    identity = hashlib.sha256(request.oss_url.encode("utf-8")).hexdigest()
    asset = session.scalar(select(Asset).where(Asset.legacy_source == "catalog_manual_url", Asset.legacy_id == identity))
    if not asset:
        asset = Asset(kind="image", name=request.name.strip() or Path(request.oss_url.split("?", 1)[0]).name or "product-image.jpg", source="catalog_manual", oss_url=request.oss_url, read_only=True, mirror_status="mirrored", mime_type="image/*", legacy_source="catalog_manual_url", legacy_id=identity)
        session.add(asset); session.flush()
    link_catalog_asset(session,product=product,asset=asset,role=request.role,position=request.position,variant_id=request.variant_id,source_kind="manual",set_current=request.set_current)
    session.commit()
    return catalog_product_dict(get_catalog_product(session, product_id))


@router.post("/products/{product_id}/assets/prepare", status_code=202)
def prepare_product_asset(product_id: str, request: CatalogAssetPrepareCreate, session: Session = Depends(get_db)) -> dict:
    product = get_catalog_product(session, product_id)
    if request.variant_id and not any(item.id == request.variant_id for item in product.variants):
        raise ConflictError("该 SKU 不属于当前产品")
    payload = {
        **request.model_dump(mode="json"),
        "product_id": product.id,
        "upload_to_oss": True,
    }
    identity = hashlib.sha256(dumps(payload).encode("utf-8")).hexdigest()
    job = enqueue_job(
        session,
        job_type="image_prepare",
        payload=payload,
        config_snapshot=build_job_snapshot(session),
        idempotency_key=f"catalog-image-prepare:{identity}",
        steps=[("download", "下载图片"), ("optimize", "检查并压缩图片"), ("register", "登记 OSS 产品资产")],
        max_attempts=3,
    )
    session.commit()
    return job_dict(job)


@router.delete("/products/{product_id}/assets/{link_id}")
def remove_product_asset(product_id: str, link_id: str, session: Session = Depends(get_db)) -> dict:
    product = get_catalog_product(session, product_id)
    link = session.scalar(select(CatalogProductAsset).where(CatalogProductAsset.id == link_id, CatalogProductAsset.product_id == product_id))
    if not link: raise NotFoundError("图片关联不存在")
    if product.source_type == "follow" and link.asset.oss_url:
        shared = int(session.scalar(select(func.count()).select_from(CatalogProductAsset).where(
            CatalogProductAsset.asset_id == link.asset_id,
            CatalogProductAsset.id != link.id,
        )) or 0)
        if shared:
            raise ConflictError("该 OSS 图片仍被其他产品引用，不能删除")
        key = _delete_follow_oss_object(session, link.asset.oss_url)
        link.asset.oss_url = ""
        link.asset.oss_key = ""
        link.asset.mirror_status = "deleted"
    else:
        key = ""
    session.delete(link); session.commit()
    return {"deleted": True, "asset_preserved": not bool(key), "oss_deleted": bool(key), "oss_key": key}


@router.get("/duplicates")
def list_duplicates(status: str = "pending", session: Session = Depends(get_db)) -> dict:
    conditions = [CatalogDuplicateSuggestion.status == status] if status else []
    items = session.scalars(select(CatalogDuplicateSuggestion).where(*conditions).order_by(CatalogDuplicateSuggestion.score.desc(), CatalogDuplicateSuggestion.created_at.desc())).all()
    return {"items": [catalog_duplicate_dict(item) for item in items]}


@router.patch("/duplicates/{suggestion_id}")
def update_duplicate(suggestion_id: str, request: CatalogDuplicateUpdate, session: Session = Depends(get_db)) -> dict:
    item = session.get(CatalogDuplicateSuggestion, suggestion_id)
    if not item: raise NotFoundError("重复提示不存在")
    item.status = request.status; session.commit()
    return catalog_duplicate_dict(item)


@router.get("/imports")
def list_catalog_imports(session: Session = Depends(get_db)) -> dict:
    items = session.scalars(select(CatalogImportRun).order_by(CatalogImportRun.created_at.desc()).limit(200)).all()
    return {"items": [catalog_import_dict(item) for item in items]}


def _enqueue_import(session: Session, run: CatalogImportRun, job_type: str, steps: list[tuple[str, str]]) -> dict:
    session.add(run); session.flush()
    payload = {"import_run_id": run.id, "dry_run": run.dry_run, "source_kind": run.kind,
               "source_sha256": loads(run.report_json, {}).get("source_sha256", "")}
    if run.kind == 'pod_history':
        payload['source_database'] = run.source_path
    job = enqueue_job(session,job_type=job_type,payload=payload,config_snapshot=build_job_snapshot(session),idempotency_key=f"{job_type}:{run.id}",steps=steps,max_attempts=3 if get_settings().multiuser_enabled else 1)
    run.job_id = job.id; session.commit()
    return {"import": catalog_import_dict(run), "job": job_dict(job)}


@router.post("/self-listing/pipeline", status_code=202)
def start_self_listing_pipeline(request: SelfListingPipelineCreate, session: Session = Depends(get_db)) -> dict:
    if get_settings().multiuser_enabled:
        # The path belongs to the initiating Windows PC. The server cannot
        # inspect or resolve it without silently changing the target machine.
        product_root = request.product_root.strip()
        if not PureWindowsPath(product_root).is_absolute():
            raise ConflictError("请填写当前执行电脑上的完整产品图片目录")
        for value in (request.output_dir, request.watermark_root):
            if value and not PureWindowsPath(value.strip()).is_absolute():
                raise ConflictError("输出和水印目录须填写当前执行电脑上的完整路径")
    else:
        product_root = Path(request.product_root).expanduser().resolve()
        if not product_root.is_dir(): raise ConflictError(f"产品图片目录不存在：{product_root}")
    step_names = {
        "generate_images": "AI 批量生图", "generate_copywriting": "生成文案表", "watermark": "批量加水印",
        "upload_original": "上传原图并生成 OSS 表", "upload_watermarked": "上传水印图并生成 OSS 表",
    }
    requested_steps = list(dict.fromkeys(request.selected_steps))
    required_providers: set[str] = set()
    if "generate_images" in requested_steps: required_providers.update({"ai_chat", "ai_image"})
    if "generate_copywriting" in requested_steps: required_providers.add("ai_chat")
    if {"upload_original", "upload_watermarked"}.intersection(requested_steps): required_providers.add("oss")
    selected: dict[str, IntegrationConnection] = {}
    for provider, connection_id in (("ai_chat", request.ai_chat_connection_id), ("ai_image", request.ai_image_connection_id), ("oss", request.oss_connection_id)):
        if provider not in required_providers: continue
        connection = session.get(IntegrationConnection, connection_id) if connection_id else session.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == provider, IntegrationConnection.enabled.is_(True)).order_by(IntegrationConnection.created_at))
        if not connection or connection.provider != provider or not connection.enabled: raise ConflictError(f"请先配置并启用 {provider} 接口")
        selected[provider] = connection
    public = {"connections": {provider: {"id": item.id, "base_url": item.base_url, "config": loads(item.config_json, {})} for provider, item in selected.items()}}
    secrets = {provider: vault.decrypt(item.secrets_encrypted) if item.secrets_encrypted else {} for provider, item in selected.items()}
    snapshot = dumps({"public": public, "secrets_encrypted": vault.encrypt(secrets)})
    payload = request.model_dump(exclude={"ai_chat_connection_id", "ai_image_connection_id", "oss_connection_id"})
    payload["product_root"] = str(product_root)
    job = enqueue_job(session, job_type="self_listing_pipeline", payload=payload, config_snapshot=snapshot, idempotency_key=f"self-listing:{new_id()}", max_attempts=3 if get_settings().multiuser_enabled else 1, steps=[(key, step_names[key]) for key in requested_steps])
    return job_dict(job)


@router.post("/self-listing/select-directory")
def select_self_listing_directory(request: LocalDirectoryPickerRequest) -> dict:
    if get_settings().runtime_mode != "local":
        raise ConflictError("文件夹选择器只能在本地 ERP 中打开")
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        initial = Path(request.initial_dir).expanduser() if request.initial_dir else None
        options = {"parent": root, "title": request.title, "mustexist": True}
        if initial and initial.is_dir(): options["initialdir"] = str(initial)
        selected = filedialog.askdirectory(**options)
        root.destroy()
    except Exception as error:
        raise ConflictError("无法打开本地文件夹选择器", details=str(error)) from error
    return {"path": str(Path(selected).resolve()) if selected else "", "cancelled": not bool(selected)}


@router.post("/imports/pod", status_code=202)
def import_pod(request: PodImportRequest, session: Session = Depends(get_db)) -> dict:
    if get_settings().multiuser_enabled:
        run = CatalogImportRun(kind='pod_history', source_path=str(request.source_database or ''), dry_run=request.dry_run)
        return _enqueue_import(session, run, 'catalog_pod_import',
                               [('products', '在发起人电脑只读核对 POD 产品、规格和资产'),
                                ('import', '仅补云端缺失的历史记录')])
    path = Path(request.source_database) if request.source_database else get_settings().legacy_composer_root / "data" / "product_library.db"
    run = CatalogImportRun(kind="pod_history",source_path=str(path.resolve()),dry_run=request.dry_run)
    return _enqueue_import(session,run,"catalog_pod_import",[("products", "校验并索引 POD 产品、SKU 和资产")])


@router.post("/imports/self-selected", status_code=202)
def import_self_selected(
    file: UploadFile = File(...), dry_run: bool = Form(True),
    store_id: str = Form(""), description_category_id: int | None = Form(None), type_id: int | None = Form(None),
    session: Session = Depends(get_db),
) -> dict:
    suffix = Path(file.filename or "").suffix.casefold()
    if suffix not in {".xlsx", ".xlsm"}: raise ConflictError("请选择 .xlsx 或 .xlsm 文件")
    digest = hashlib.sha256(); import_dir = get_settings().data_dir / "imports"; import_dir.mkdir(parents=True, exist_ok=True)
    temporary = import_dir / f"catalog-{os.getpid()}-{id(file)}{suffix}.tmp"
    try:
        with temporary.open("wb") as target:
            size = 0
            while chunk := file.file.read(1024*1024):
                size += len(chunk)
                if get_settings().multiuser_enabled and size > 20 * 1024 * 1024:
                    raise ConflictError("自主上架预演工作簿不得超过 20MB")
                digest.update(chunk); target.write(chunk)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    final = import_dir / f"self-selected-{digest.hexdigest()[:20]}{suffix}"
    if final.exists(): temporary.unlink(missing_ok=True)
    else: temporary.replace(final)
    report = _selected_category_report(session, store_id, description_category_id, type_id)
    run=CatalogImportRun(kind="self_selected",source_path=str(final),dry_run=dry_run,
                         report_json=dumps({**report, "source_sha256": digest.hexdigest()}))
    return _enqueue_import(session,run,"catalog_self_selected_import",[("validate", "校验字段、数值、链接和重复 SKU"),("import", "建立产品主档、SKU 和当前图片")])


@router.post("/imports/self-selected/bundle", status_code=202)
def import_self_selected_bundle(
    file: UploadFile = File(...), dry_run: bool = Form(True),
    store_id: str = Form(""), description_category_id: int | None = Form(None), type_id: int | None = Form(None),
    session: Session = Depends(get_db),
) -> dict:
    """Import one portable ZIP containing the workbook and its image files."""
    if Path(file.filename or "").suffix.casefold() != ".zip":
        raise ConflictError("请选择 .zip 产品资料包")
    digest = hashlib.sha256()
    import_dir = get_settings().data_dir / "imports"
    import_dir.mkdir(parents=True, exist_ok=True)
    temporary = import_dir / f"bundle-{os.getpid()}-{id(file)}.tmp"
    try:
        with temporary.open("wb") as target:
            size = 0
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if get_settings().multiuser_enabled and size > 2 * 1024**3:
                    raise ConflictError("资料包超过 2GB")
                digest.update(chunk); target.write(chunk)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    if get_settings().multiuser_enabled:
        connection = None
        if not dry_run:
            connection = session.scalar(select(IntegrationConnection).where(
                IntegrationConnection.provider == 'oss',
                IntegrationConnection.enabled.is_(True),
                IntegrationConnection.name == 'OZON 自动上架 OSS'))
            if not connection:
                temporary.unlink(missing_ok=True)
                raise ConflictError('未找到 OZON 自动上架 OSS 配置，无法正式入库图片')
        final = import_dir / f"self-selected-bundle-{digest.hexdigest()[:20]}.zip"
        if final.exists(): temporary.unlink(missing_ok=True)
        else: temporary.replace(final)
        category_report = _selected_category_report(session, store_id, description_category_id, type_id)
        run = CatalogImportRun(kind="self_selected_bundle", source_path=str(final), dry_run=dry_run,
                               report_json=dumps({**category_report, "source_sha256": digest.hexdigest(),
                                                  "oss_connection_id": connection.id if connection else ""}))
        return _enqueue_import(session, run, "catalog_self_selected_import",
                               [("validate", "在发起人电脑校验表格、图片清单和文件"),
                                ("import", "图片入 OSS 并同步产品与 SKU")])
    bundle_id = digest.hexdigest()[:20]
    bundle_root = import_dir / f"self-selected-{bundle_id}"
    if not bundle_root.exists():
        bundle_root.mkdir(parents=True)
        try:
            with zipfile.ZipFile(temporary) as archive:
                members = [item for item in archive.infolist() if not item.is_dir()]
                if len(members) > 5000 or sum(item.file_size for item in members) > 2 * 1024**3:
                    raise ConflictError("资料包过大（最多 5000 个文件、解压后 2GB）")
                root = bundle_root.resolve()
                for item in members:
                    destination = (bundle_root / item.filename).resolve()
                    if root not in destination.parents:
                        raise ConflictError("资料包包含不安全的文件路径")
                archive.extractall(bundle_root)
        except zipfile.BadZipFile as error:
            raise ConflictError("资料包不是有效的 ZIP 文件") from error
    temporary.unlink(missing_ok=True)
    workbooks = sorted([path for path in bundle_root.rglob("*") if path.suffix.casefold() in {".xlsx", ".xlsm"} and not path.name.startswith("~$")])
    if len(workbooks) != 1:
        raise ConflictError(f"资料包必须且只能包含一个 Excel，当前找到 {len(workbooks)} 个")
    category_report = _selected_category_report(session, store_id, description_category_id, type_id)
    run = CatalogImportRun(kind="self_selected_bundle", source_path=str(workbooks[0]), dry_run=dry_run, report_json=dumps({"bundle_root": str(bundle_root), **category_report}))
    return _enqueue_import(session, run, "catalog_self_selected_import", [("validate", "校验表格、图片清单和文件"), ("import", "同步产品、SKU 和本地图片资产")])


@router.get("/imports/self-selected/template")
def self_selected_template():
    template = Path(__file__).resolve().parents[2] / "templates" / "ozon自主上架标准模板.xlsx"
    if not template.is_file(): raise NotFoundError("自主上架标准模板不存在")
    return FileResponse(template, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename="ozon自主上架标准模板.xlsx")
