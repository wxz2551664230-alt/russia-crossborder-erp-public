"""Curated Codex business actions. Never dispatch arbitrary HTTP paths."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import mimetypes
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session
from sqlalchemy import select

from erp.core.access import access_key
from erp.core.config import get_settings
from erp.core.database import get_db
from erp.core.errors import AppError, ConflictError, NotFoundError
from erp.models import CatalogProduct, Job, SystemSetting, Publication, PublicationStore, Store, WbPublicationItem
from erp.models import WorkerNode
from erp.core.jsonutil import loads
from erp.core.task_identity import task_identity
from erp.api.routes.catalog import ProductNotesUpdate, save_product_notes

router = APIRouter(prefix="/codex-actions", tags=["codex-actions"])
PREVIEW_SECONDS = 300


class ActionPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arguments: dict = Field(default_factory=dict)


class ActionExecute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_token: str


class NotesArguments(ProductNotesUpdate):
    product_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class UpscaleArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_ids: list[str] = Field(min_length=1, max_length=30)
    scale: int = Field(default=2, ge=2, le=4)
    category_id: str = Field(default="uncategorized", min_length=1, max_length=100)


class ArtworkImportArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_ids: list[str] = Field(min_length=1, max_length=30)
    category_id: str = Field(min_length=1, max_length=100)


from erp.domains.pod.api import PodCompositionBatchCreate, PodWbExportCreate, PodWbStockBatch, PodWbPriceBatch
from erp.schemas import PublicationCreate, PublicationPreviewRequest, PublicationStockRequest, PublicationPromotionRequest
from typing import Literal


class CompositionArguments(PodCompositionBatchCreate):
    model_config = ConfigDict(extra="forbid")
    artwork_ids: list[str] = Field(min_length=1, max_length=100)
    prevent_duplicate: bool = True


class OzonPrepareArguments(PublicationCreate):
    model_config = ConfigDict(extra="forbid")
    product_ids: list[str] = Field(min_length=1, max_length=1)
    store_ids: list[str] = Field(min_length=1, max_length=1)
    generation_provider: Literal["codex"] = "codex"


class WbPublishArguments(PodWbExportCreate):
    model_config = ConfigDict(extra="forbid")
    generation_provider: Literal["codex"] = "codex"
    mode: Literal["api"] = "api"


class OzonFollowupArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    publication_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    store_id: str = Field(min_length=1, max_length=32)


class OzonStockArguments(OzonFollowupArguments, PublicationStockRequest):
    pass


class OzonPromotionArguments(OzonFollowupArguments, PublicationPromotionRequest):
    pass


class WbStockArguments(PodWbStockBatch):
    model_config = ConfigDict(extra="forbid")


class WbPriceArguments(PodWbPriceBatch):
    model_config = ConfigDict(extra="forbid")


def _notes_preview(db: Session, args: NotesArguments) -> dict:
    item = db.get(CatalogProduct, args.product_id)
    if not item:
        raise NotFoundError("产品不存在")
    if item.notes != args.expected_notes:
        raise ConflictError("备注已变化，请读取最新产品资料后重新预览")
    return {"product_id": item.id, "sku": item.sku, "product_name": item.name,
            "before": item.notes, "after": args.notes, "changed": item.notes != args.notes}


def _notes_execute(db: Session, args: NotesArguments) -> dict:
    return save_product_notes(args.product_id, ProductNotesUpdate(
        expected_notes=args.expected_notes, notes=args.notes), db)


def _upscale_preview(db: Session, args: UpscaleArguments) -> dict:
    from erp.domains.pod.ai_design import cloud
    from erp.domains.pod.models import PodArtworkCategory
    if args.scale not in {2, 4} or len(set(args.upload_ids)) != len(args.upload_ids):
        raise ConflictError("超分倍率或图片清单无效")
    if args.category_id != "uncategorized" and not db.get(PodArtworkCategory, args.category_id):
        raise ConflictError("设计图库分类不存在")
    actor = task_identity.get() or {}
    device = db.get(WorkerNode, actor.get("worker_id") or "")
    if not device or device.owner_user_id != actor.get("id") or "pod_ai_design_upscale" not in loads(device.capabilities_json, []):
        raise ConflictError("当前登录未选中支持超分的本人电脑")
    rows = cloud.uploads(db, args.upload_ids, 30)
    return {"worker_id": device.id, "worker_name": device.name, "count": len(rows),
            "scale": args.scale, "category_id": args.category_id,
            "images": [{"id": row["id"], "name": row["name"],
            "sha256": row["sha256"], "size_bytes": row["size_bytes"]} for row in rows]}


def _upscale_execute(db: Session, args: UpscaleArguments, token: str) -> dict:
    from erp.domains.pod.ai_design import cloud
    identity = hashlib.sha256(token.encode()).hexdigest()[:32]
    return cloud.create(db, "upscale", {"upscale_scale": args.scale, "category_id": args.category_id}, args.upload_ids, identity=identity)


def _artwork_preview(db: Session, args: ArtworkImportArguments) -> dict:
    from erp.domains.pod.ai_design import cloud
    from erp.domains.pod.models import PodArtworkCategory
    if len(set(args.upload_ids)) != len(args.upload_ids):
        raise ConflictError("图片清单不能重复")
    actor = task_identity.get() or {}
    device = db.get(WorkerNode, actor.get("worker_id") or "")
    if not device or device.owner_user_id != actor.get("id") or "pod_artwork_upload" not in loads(device.capabilities_json, []):
        raise ConflictError("当前登录未选中支持设计图入库的本人电脑")
    category = db.get(PodArtworkCategory, args.category_id) if args.category_id != "uncategorized" else None
    if args.category_id != "uncategorized" and not category:
        raise NotFoundError("指定的设计图库分类不存在")
    rows = cloud.uploads(db, args.upload_ids, 30)
    return {"worker_id": device.id, "worker_name": device.name,
            "category_id": args.category_id, "category_name": category.name if category else "未分类",
            "count": len(rows), "images": [{"id": row["id"], "name": row["name"],
            "sha256": row["sha256"], "size_bytes": row["size_bytes"]} for row in rows]}


def _staged_image(row: dict) -> bytes:
    filename = row.get("stored_name") or ""
    if Path(filename).name != filename or not filename.startswith(row["id"] + "."):
        raise ConflictError("图片暂存记录无效")
    path = get_settings().data_dir / "pod" / "ai-cloud-inputs" / filename
    if path.is_symlink() or not path.is_file():
        raise ConflictError("暂存图片文件缺失")
    content = path.read_bytes()
    if len(content) != row["size_bytes"] or hashlib.sha256(content).hexdigest() != row["sha256"]:
        raise ConflictError("暂存图片校验失败")
    return content


def _artwork_execute(db: Session, args: ArtworkImportArguments, token: str) -> dict:
    from erp.domains.pod.ai_design import cloud
    from erp.domains.pod.desktop_artwork import stage
    rows = cloud.uploads(db, args.upload_ids, 30)
    results = []
    for row in rows:
        deterministic = hashlib.sha256((token + ":" + row["id"]).encode()).hexdigest()[:32]
        content = _staged_image(row)
        mime = mimetypes.guess_type(row["name"])[0] or "application/octet-stream"
        results.append(stage(db, content, row["name"], mime, args.category_id, upload_token=deterministic))
    return {"accepted": len(results), "results": results,
            "job_ids": [item["job_id"] for item in results]}


def _composition_existing(db: Session, args: CompositionArguments, token: str) -> dict[str, dict]:
    from erp.domains.pod.models import PodComposition, PodCompositionSide
    actor = task_identity.get() or {}
    found = {}
    for artwork_id in args.artwork_ids:
        key = "codex-composition:" + hashlib.sha256((token + ":" + artwork_id).encode()).hexdigest()
        assigned = "assigned:" + hashlib.sha256(f"{actor.get('id')}:{actor.get('worker_id')}:{key}".encode()).hexdigest()
        job = db.scalar(select(Job).where(Job.idempotency_key == assigned))
        if not job:
            continue
        item = db.get(PodComposition, loads(job.payload_json, {}).get("composition_id"))
        side = db.scalar(select(PodCompositionSide).where(PodCompositionSide.composition_id == item.id,
                         PodCompositionSide.artwork_id == artwork_id)) if item else None
        if not item or not side or item.supplier != args.supplier or item.product_type_id != args.product_type_id:
            raise ConflictError("原合成任务资料异常，请人工核对")
        found[artwork_id] = {"artwork_id": artwork_id, "composition_id": item.id, "job_id": item.job_id}
    return found


def _composition_preview(db: Session, args: CompositionArguments, token: str | None = None) -> dict:
    from erp.domains.pod.models import PodArtwork
    from erp.domains.pod.artworks import used_artwork_ids
    if len(set(args.artwork_ids)) != len(args.artwork_ids) or len(args.placements) != 1:
        raise ConflictError("批量合成需要不重复的设计图及一个共用印刷面")
    actor = task_identity.get() or {}
    device = db.get(WorkerNode, actor.get("worker_id") or "")
    if not device or device.owner_user_id != actor.get("id") or "pod_native_composition" not in loads(device.capabilities_json, []):
        raise ConflictError("当前登录未选中支持 POD 合成的本人电脑")
    artworks = [db.get(PodArtwork, identity) for identity in args.artwork_ids]
    if any(item is None or not item.asset or not item.asset.oss_url for item in artworks):
        raise ConflictError("部分设计图不存在或尚未完成 OSS 入库")
    used = used_artwork_ids(db, artwork_ids=args.artwork_ids, supplier=args.supplier,
                           product_type_id=args.product_type_id)
    existing = _composition_existing(db, args, token) if token else {}
    if args.prevent_duplicate and used - set(existing):
        raise ConflictError("部分设计图已用于该供应商产品，请从图库筛选未合成设计图")
    return {"worker_id": device.id, "worker_name": device.name,
            "supplier": args.supplier, "product_type_id": args.product_type_id,
            "product_name": args.product_name, "artwork_count": len(artworks),
            "already_used_count": len(used), "prevent_duplicate": args.prevent_duplicate,
            "artwork_ids": args.artwork_ids, "selected_size_ids": args.selected_size_ids,
            "placement": args.placements[0].model_dump()}


def _composition_execute(db: Session, args: CompositionArguments, token: str) -> dict:
    from erp.domains.pod.api import _prepare_composition_payload
    from erp.domains.pod.composition import create_native_composition
    existing = _composition_existing(db, args, token)
    base = args.model_dump(exclude={"artwork_ids", "prevent_duplicate"})
    template = dict(base["placements"][0])
    prepared = None
    results = []
    for artwork_id in args.artwork_ids:
        key = "codex-composition:" + hashlib.sha256((token + ":" + artwork_id).encode()).hexdigest()
        if artwork_id in existing:
            results.append(existing[artwork_id])
            continue
        else:
            if prepared is None:
                prepared = _prepare_composition_payload(db, base)
            item = create_native_composition(db, request={**prepared,
                "placements": [{**template, "artwork_id": artwork_id}]},
                defer_source_preparation=True, job_idempotency_key=key)
        results.append({"artwork_id": artwork_id, "composition_id": item.id, "job_id": item.job_id})
    return {"accepted": len(results), "results": results,
            "job_ids": [item["job_id"] for item in results]}


def _existing_once(db: Session, action_id: str, token: str) -> dict | None:
    actor = task_identity.get() or {}
    record = db.get(SystemSetting, "codex-action:" + hashlib.sha256(token.encode()).hexdigest())
    if not record:
        return None
    value = loads(record.value_json, {})
    if value.get("user_id") != actor.get("id") or value.get("worker_id") != actor.get("worker_id") or value.get("action_id") != action_id:
        raise ConflictError("原操作身份或电脑不匹配")
    if value.get("state") == "done":
        return value["result"]
    raise ConflictError("原操作结果待核对，请查看任务和业务记录，不要重新提交")


def _started_once(db: Session, action_id: str, token: str, callback) -> dict:
    """Persistent guard for original workflows that commit internally.

    A transport interruption never starts another platform-facing preparation.
    If the original call committed but its receipt was lost, the caller must
    inspect the original business records before any new request.
    """
    actor = task_identity.get() or {}
    key = "codex-action:" + hashlib.sha256(token.encode()).hexdigest()
    old = _existing_once(db, action_id, token)
    if old is not None:
        return old
    record = SystemSetting(key=key, value_json=json.dumps({
        "state": "started", "user_id": actor.get("id"), "worker_id": actor.get("worker_id"),
        "action_id": action_id, "started_at": int(time.time())}))
    db.add(record); db.commit()
    try:
        result = callback()
        db.get(SystemSetting, key).value_json = json.dumps({
            "state": "done", "user_id": actor.get("id"), "worker_id": actor.get("worker_id"),
            "action_id": action_id, "result": result}, ensure_ascii=False)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _ozon_prepare_preview(db: Session, args: OzonPrepareArguments) -> dict:
    from erp.api.routes.publications import preview_publications
    product_id, store_id = args.product_ids[0], args.store_ids[0]
    actor = task_identity.get() or {}
    device = db.get(WorkerNode, actor.get("worker_id") or "")
    if not device or device.owner_user_id != actor.get("id") or "codex_handoff_v1" not in loads(device.capabilities_json, []):
        raise ConflictError("当前电脑尚未连接 Ozon Codex 接手能力")
    prior = db.scalars(select(PublicationStore).join(Publication).where(
        Publication.product_id == product_id, PublicationStore.store_id == store_id,
        PublicationStore.status.notin_({"failed", "cancelled"})).limit(1)).first()
    if prior:
        raise ConflictError("该商品已有此店铺的上架记录，请从原记录继续或核对，不重复建品")
    rows = preview_publications(PublicationPreviewRequest(
        product_ids=args.product_ids, store_ids=args.store_ids,
        requirements=args.requirements), db)
    return {"worker_id": device.id, "generation_provider": "codex", "image_policy": args.image_policy,
            "dry_run": args.dry_run, "submit_via_api": args.submit_via_api,
            "count": rows["count"], "items": rows["items"]}


def _ozon_prepare_execute(db: Session, args: OzonPrepareArguments, token: str) -> dict:
    from erp.api.routes.publications import create_publications
    return _started_once(db, "ozon.prepare.codex", token, lambda: create_publications(args, db))


def _wb_publish_preview(db: Session, args: WbPublishArguments) -> dict:
    from erp.domains.pod.wb_export import _template, preview_export
    template = _template(args.template_id)
    store = db.get(Store, args.store_id)
    if not store or not store.enabled or store.platform.casefold() not in {"wb", "wildberries"}:
        raise ConflictError("WB 店铺不存在或已停用")
    actor = task_identity.get() or {}
    device = db.get(WorkerNode, actor.get("worker_id") or "")
    if not device or device.owner_user_id != actor.get("id") or "codex_wb_v1" not in loads(device.capabilities_json, []):
        raise ConflictError("当前电脑尚未连接 WB Codex 接手能力")
    report = preview_export(db, product_ids=args.product_ids,
        subject_id=template["subject_id"], variant_selection=args.variant_selection,
        variant_ids=args.variant_ids, pricing=args.pricing)
    return {"worker_id": device.id, "store_id": store.id, "store_name": store.name,
            "template_id": args.template_id, "template_subject": template["subject_name"],
            "generation_provider": "codex", "mode": "api", "price_cny": args.price_cny,
            "report": report}


def _wb_publish_execute(db: Session, args: WbPublishArguments, token: str) -> dict:
    from erp.domains.pod.wb_export import create_export
    from erp.serializers import job_dict
    def create():
        job = create_export(db, **args.model_dump())
        db.commit(); db.refresh(job)
        return {"job": job_dict(job)}
    return _started_once(db, "wb.publish.codex_api", token, create)


def _ozon_followup_preview(db: Session, args: OzonFollowupArguments, expected: str) -> dict:
    publication = db.get(Publication, args.publication_id)
    child = db.scalar(select(PublicationStore).where(
        PublicationStore.publication_id == args.publication_id,
        PublicationStore.store_id == args.store_id))
    if not publication or not child: raise NotFoundError("原 Ozon 上架记录不存在")
    if child.status != expected: raise ConflictError("该商品尚未进入此阶段，请先完成原页面核验或核对平台回执")
    actor = task_identity.get() or {}
    worker = db.get(WorkerNode, actor.get("worker_id") or "")
    if not worker or worker.owner_user_id != actor.get("id"):
        raise ConflictError("请连接当前发起人的执行电脑")
    if expected == "ready_for_submit" and child.external_task_id:
        raise ConflictError("已有平台提交编号，请先核对结果")
    return {"publication_id": publication.id, "store_id": child.store_id,
            "offer_id": child.offer_id, "stage": child.status,
            "warehouse_id": getattr(args, "warehouse_id", None),
            "stock": getattr(args, "stock", None),
            "action_id": getattr(args, "action_id", None),
            "action_price": getattr(args, "action_price", None)}


def _ozon_submit_preview(db: Session, args: OzonFollowupArguments) -> dict:
    return _ozon_followup_preview(db, args, "ready_for_submit")


def _ozon_stock_preview(db: Session, args: OzonStockArguments) -> dict:
    return _ozon_followup_preview(db, args, "stock_pending")


def _ozon_promotion_preview(db: Session, args: OzonPromotionArguments) -> dict:
    return _ozon_followup_preview(db, args, "promotion_pending")


def _ozon_submit_execute(db: Session, args: OzonFollowupArguments, token: str) -> dict:
    from erp.api.routes.publications import submit_publication_store
    return _started_once(db, "ozon.submit.reviewed", token,
        lambda: submit_publication_store(args.publication_id, args.store_id, db))


def _ozon_stock_execute(db: Session, args: OzonStockArguments, token: str) -> dict:
    from erp.api.routes.publications import add_publication_stock
    return _started_once(db, "ozon.stock.add", token,
        lambda: add_publication_stock(args.publication_id, args.store_id,
            PublicationStockRequest(warehouse_id=args.warehouse_id, stock=args.stock), db))


def _ozon_promotion_execute(db: Session, args: OzonPromotionArguments, token: str) -> dict:
    from erp.api.routes.publications import add_publication_promotion
    return _started_once(db, "ozon.promotion.add", token,
        lambda: add_publication_promotion(args.publication_id, args.store_id,
            PublicationPromotionRequest(action_id=args.action_id, action_price=args.action_price, stock=args.stock), db))


def _wb_followup_preview(db: Session, args: WbStockArguments | WbPriceArguments, expected: set[str]) -> dict:
    ids = list(dict.fromkeys(args.ledger_ids))
    rows = db.scalars(select(WbPublicationItem).where(WbPublicationItem.id.in_(ids))).all()
    if len(rows) != len(ids) or not rows: raise NotFoundError("部分 WB 商品记录不存在")
    if len({row.store_id for row in rows}) != 1 or any(row.status not in expected for row in rows):
        raise ConflictError("所选记录的店铺或阶段不一致，请先核对原任务")
    actor = task_identity.get() or {}
    worker = db.get(WorkerNode, actor.get("worker_id") or "")
    if not worker or worker.owner_user_id != actor.get("id"):
        raise ConflictError("请连接当前发起人的执行电脑")
    return {"store_id": rows[0].store_id,
            "items": [{"ledger_id": row.id, "offer_id": row.offer_id, "status": row.status} for row in rows],
            "warehouse_id": getattr(args, "warehouse_id", None), "amount": getattr(args, "amount", None),
            "price": getattr(args, "price", None), "discount_percent": getattr(args, "discount_percent", None)}


def _wb_stock_preview(db: Session, args: WbStockArguments) -> dict:
    return _wb_followup_preview(db, args, {"pending_stock", "stock_failed"})


def _wb_price_preview(db: Session, args: WbPriceArguments) -> dict:
    return _wb_followup_preview(db, args, {"pending_price", "price_failed"})


def _wb_stock_execute(db: Session, args: WbStockArguments, token: str) -> dict:
    from erp.domains.pod.api import pod_wb_stock
    return _started_once(db, "wb.stock.add", token, lambda: pod_wb_stock(args, db))


def _wb_price_execute(db: Session, args: WbPriceArguments, token: str) -> dict:
    from erp.domains.pod.api import pod_wb_price
    return _started_once(db, "wb.price.set", token, lambda: pod_wb_price(args, db))


# A new business action must register an explicit schema, preview and executor.
# Descriptions are data for discovery; they do not grant arbitrary API access.
REGISTRY = {
    "catalog.notes.compare_save": {
        "title": "比较并保存产品内部备注",
        "module": "产品库",
        "description": "只修改指定产品的内部备注；原备注变化时停止。不会修改商品价格、图片或上架状态。",
        "execution": "cloud_shared_data",
        "input_model": NotesArguments,
        "preview": _notes_preview,
        "execute": _notes_execute,
    },
    "pod.ai.upscale.staged": {
        "title": "在当前电脑超分已暂存的图片",
        "module": "AI 生图与超分",
        "description": "每批1至30张，2倍或4倍；只在发起人当前选中的电脑执行，结果按原流程入共享设计图库。",
        "execution": "initiator_computer",
        "input_model": UpscaleArguments,
        "preview": _upscale_preview,
        "execute": _upscale_execute,
    },
    "pod.artwork.import.staged": {
        "title": "暂存图片入指定设计图库分类",
        "module": "设计图库",
        "description": "每批1至30张；原电脑校验图片并入库 OSS，完成后通过上传任务回执取得设计图编号。",
        "execution": "initiator_computer",
        "input_model": ArtworkImportArguments,
        "preview": _artwork_preview,
        "execute": _artwork_execute,
    },
    "pod.composition.batch": {
        "title": "用指定设计图批量合成 POD 产品",
        "module": "POD 图片合成",
        "description": "复用一个原 ERP 印刷面排版；每批最多100张，默认拒绝同类产品重复合成，任务在当前电脑运行。",
        "execution": "initiator_computer",
        "input_model": CompositionArguments,
        "preview": _composition_preview,
        "execute": _composition_execute,
    },
    "ozon.prepare.codex": {
        "title": "为 Ozon 店铺准备 Codex 上架任务",
        "module": "Ozon 产品上架",
        "description": "一次一个产品和一家店铺，按原图序、定价及类目规则准备；文案和所选生图等待原电脑 Codex 回填，图片核验后才能提交。",
        "execution": "initiator_computer", "input_model": OzonPrepareArguments,
        "preview": _ozon_prepare_preview, "execute": _ozon_prepare_execute,
    },
    "wb.publish.codex_api": {
        "title": "为 WB 店铺准备 Codex API 建品任务",
        "module": "WB 产品上架",
        "description": "按原 WB 官方模板和规格/图片顺序准备；等待原电脑 Codex 文案回填和原业务页面核验后才调用 WB API。",
        "execution": "initiator_computer", "input_model": WbPublishArguments,
        "preview": _wb_publish_preview, "execute": _wb_publish_execute,
    },
    "ozon.submit.reviewed": {
        "title": "提交已核验的 Ozon 商品", "module": "Ozon 产品上架",
        "description": "只提交原页面已完成人工图片核验、状态为待提交的商品；未知结果须查询原任务。",
        "execution": "initiator_computer", "input_model": OzonFollowupArguments,
        "preview": _ozon_submit_preview, "execute": _ozon_submit_execute,
    },
    "ozon.stock.add": {
        "title": "为已建 Ozon 商品添加库存", "module": "Ozon 产品上架",
        "description": "沿原库存流程在发起人电脑执行；仅接受待添加库存记录。",
        "execution": "initiator_computer", "input_model": OzonStockArguments,
        "preview": _ozon_stock_preview, "execute": _ozon_stock_execute,
    },
    "ozon.promotion.add": {
        "title": "为 Ozon 商品加入促销", "module": "Ozon 产品上架",
        "description": "沿原促销流程执行；只接受已到促销阶段且有平台商品ID的记录。",
        "execution": "initiator_computer", "input_model": OzonPromotionArguments,
        "preview": _ozon_promotion_preview, "execute": _ozon_promotion_execute,
    },
    "wb.stock.add": {
        "title": "批量添加 WB 库存", "module": "WB 产品上架",
        "description": "沿原 WB 批量库存任务执行，限同店铺和待库存记录。",
        "execution": "initiator_computer", "input_model": WbStockArguments,
        "preview": _wb_stock_preview, "execute": _wb_stock_execute,
    },
    "wb.price.set": {
        "title": "批量设置 WB 价格与折扣", "module": "WB 产品上架",
        "description": "沿原 WB 批量价格流程执行；平台结果未知时先核对，不自动重发。",
        "execution": "initiator_computer", "input_model": WbPriceArguments,
        "preview": _wb_price_preview, "execute": _wb_price_execute,
    },
}


def _action(action_id: str) -> dict:
    if action_id not in REGISTRY:
        raise NotFoundError("此业务操作尚未接入 Codex")
    return REGISTRY[action_id]


def _encode(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    digest = hmac.new(access_key().encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{digest}"


def _decode(token: str, user_id: str, action_id: str) -> dict:
    try:
        if len(token) > 30000:
            raise ValueError()
        body, digest = token.split(".", 1)
        expected = hmac.new(access_key().encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, expected):
            raise ValueError()
        value = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if value["user_id"] != user_id or value["action_id"] != action_id or value["expires_at"] < time.time():
            raise ValueError()
        return value
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, binascii.Error) as error:
        raise ConflictError("操作预览已失效，请重新预览") from error


@router.get("")
def list_actions() -> dict:
    return {"contract": 1, "actions": [
        {"id": key, "title": row["title"], "module": row["module"],
         "description": row["description"], "execution": row["execution"],
         "input_schema": row["input_model"].model_json_schema()}
        for key, row in REGISTRY.items()]}


@router.post("/{action_id}/preview")
def preview_action(action_id: str, body: ActionPreview, request: Request, db: Session = Depends(get_db)) -> dict:
    action = _action(action_id)
    try:
        args = action["input_model"].model_validate(body.arguments)
    except ValidationError as error:
        raise AppError("操作参数不完整或格式错误", details=error.errors()) from error
    effect = action["preview"](db, args)
    expires_at = int(time.time()) + PREVIEW_SECONDS
    token = _encode({"user_id": request.state.user["id"], "action_id": action_id,
                     "worker_id": request.state.user.get("worker_id") if action["execution"] == "initiator_computer" else None,
                     "arguments": args.model_dump(), "expires_at": expires_at})
    return {"action_id": action_id, "effect": effect, "execution": action["execution"],
            "expires_at": expires_at, "preview_token": token}


@router.post("/{action_id}/execute")
def execute_action(action_id: str, body: ActionExecute, request: Request, db: Session = Depends(get_db)) -> dict:
    action = _action(action_id)
    payload = _decode(body.preview_token, request.state.user["id"], action_id)
    if action["execution"] == "initiator_computer" and payload["worker_id"] != request.state.user.get("worker_id"):
        raise ConflictError("执行电脑已变化，请重新预览")
    if action_id in {"ozon.prepare.codex", "wb.publish.codex_api", "ozon.submit.reviewed",
                     "ozon.stock.add", "ozon.promotion.add", "wb.stock.add", "wb.price.set"}:
        completed = _existing_once(db, action_id, body.preview_token)
        if completed is not None:
            return {"action_id": action_id, "result": completed}
    args = action["input_model"].model_validate(payload["arguments"])
    if action_id == "pod.composition.batch":
        action["preview"](db, args, body.preview_token)
    else:
        action["preview"](db, args)  # Recheck shared state immediately before writing.
    result = action["execute"](db, args, body.preview_token) if action["execution"] == "initiator_computer" else action["execute"](db, args)
    return {"action_id": action_id, "result": result}
