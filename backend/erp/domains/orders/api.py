from typing import Annotated, Literal
from uuid import uuid4
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StringConstraints, model_validator
from sqlalchemy.orm import Session

from erp.core.database import get_db
from erp.core.errors import AppError
from erp.core.jsonutil import loads
from erp.core.security import vault
from erp.domains.orders.legacy_engine import OrderError, is_platform_order_cancelled
from erp.domains.orders.service import delete_purchase_address, manager, procurement_settings, saved_purchase_addresses, save_purchase_address, tms_preview
from erp.models import Store
from erp.services.jobs import enqueue_job

router = APIRouter(prefix="/orders/pod", tags=["pod-orders"])


def invoke(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except OrderError as error:
        raise AppError(str(error), code="pod_order_error", status_code=error.status_code) from None


class Selection(BaseModel):
    order_keys: list[str] = Field(min_length=1, max_length=500)
    merge_mode: Literal["smart", "same_sku", "separate"] = "smart"


class QueueBatch(Selection):
    address: dict[str, str]
    logistics: list[dict]


class SubmitPurchase(BaseModel):
    target_type: Literal["group", "order"]
    target_id: str
    confirmation: Literal["CONFIRM_REAL_PURCHASE"]
    idempotency_key: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=100)


class SubmitTms(BaseModel):
    confirmation: Literal["CONFIRM_REAL_TMS"]
    idempotency_key: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=100)


class SubmitTmsBatch(BaseModel):
    order_keys: list[str] = Field(min_length=1, max_length=200)
    confirmation: Literal["CONFIRM_REAL_TMS"]
    idempotency_key: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=100)


class ManualTracking(BaseModel):
    tracking_number: str = Field(min_length=1, max_length=100)
    tracking_company: str = Field(min_length=1, max_length=100)


class ImportOrders(BaseModel):
    store_id: str
    postings: list[dict] = Field(min_length=1, max_length=500)


class ManualOrderItem(BaseModel):
    sku: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    quantity: int = Field(ge=1, le=10000, strict=True)


class ManualOrder(BaseModel):
    store_id: str = Field(min_length=1, max_length=32)
    order_number: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$')]
    items: list[ManualOrderItem] = Field(min_length=1, max_length=100)


class SavedAddress(BaseModel):
    id: str = ""
    name: str = Field(min_length=1,max_length=100)
    is_default: bool = False
    address: dict[str,str]


class SyncOrders(BaseModel):
    store_id: str
    days: int = Field(default=30, ge=1, le=90)
    status: str = Field(default="", max_length=50)
    sync_mode: Literal["time", "numbers"] = "time"
    date_from: datetime | None = None
    date_to: datetime | None = None
    order_numbers: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]] = Field(default_factory=list, max_length=100)
    sync_images: bool = True
    scope_version: Literal[1] | None = None

    @model_validator(mode="after")
    def validate_scope(self):
        if (self.date_from is None) != (self.date_to is None):
            raise ValueError("请同时填写开始和结束时间")
        if self.date_from is not None:
            if self.date_from.tzinfo is None or self.date_to.tzinfo is None:
                raise ValueError("时间必须包含时区")
            if not timedelta(0) < self.date_to - self.date_from <= timedelta(days=90):
                raise ValueError("时间范围必须大于零且不超过90天")
            if self.date_to > datetime.now(UTC) + timedelta(minutes=1):
                raise ValueError("结束时间不能晚于当前时间")
        self.order_numbers = list(dict.fromkeys(self.order_numbers))
        if self.sync_mode == "numbers" and not self.order_numbers:
            raise ValueError("请填写平台订单号")
        if self.sync_mode == "time" and self.order_numbers:
            raise ValueError("按时间同步不能同时指定订单号")
        return self

    idempotency_key: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=100)


def store_config(session, store_id):
    store = session.get(Store, store_id)
    if not store or not store.enabled or store.platform.lower() not in {"ozon", "wb", "wildberries"}:
        raise AppError("请选择启用的 OZON 或 WB 店铺", code="invalid_order_store")
    config = loads(store.config_json, {})
    secrets = vault.decrypt(store.connection.secrets_encrypted) if store.connection else {}
    secrets.update(vault.decrypt(store.secrets_encrypted))
    return {"platform": "ozon" if store.platform.lower() == "ozon" else "wb", "store_id": store.id,
            "store_name": store.name, "ozon_client_id": secrets.get("client_id") or store.external_account_id,
            "ozon_api_key": secrets.get("api_key") or secrets.get("ozon_api_key"),
            "wildberries_token": secrets.get("token") or secrets.get("api_key") or secrets.get("wildberries_token"),
            "wb_rate": float(config.get("wildberries_order_rub_to_cny_rate") or 0.0783)}


@router.get("")
def orders(search: str = "", match_status: str = "", submission_status: str = "", platform: str = "", store_id: str = "", workflow_status: str = "", tracking_status: str = "",
           page:int=Query(1,ge=1),page_size:int=Query(100,ge=20,le=200),limit: int|None = Query(None, ge=1, le=500), session: Session = Depends(get_db)):
    size=limit or page_size;offset=0 if limit is not None else (page-1)*page_size
    if store_id.strip().lower() in {"undefined", "null"}:
        store_id = ""
    return invoke(manager(session).list_orders, search=search, match_status=match_status,
                  submission_status=submission_status, platform=platform, store_id=store_id,
                  workflow_status=workflow_status, tracking_status=tracking_status, limit=size, offset=offset)


@router.post("/sync")
def sync(body: SyncOrders, session: Session = Depends(get_db)):
    config = store_config(session, body.store_id)
    validate_sync_credentials(config)
    sync_payload = body.model_dump(mode="json")
    if body.scope_version or body.date_from or body.sync_mode == "numbers" or not body.sync_images:
        end = body.date_to or datetime.now(UTC)
        start = body.date_from or end - timedelta(days=body.days)
        if config["platform"] == "wb":
            if end - start > timedelta(days=30):
                raise AppError("WB 单次查询范围不能超过30天", code="invalid_order_scope")
            if any(not number.isascii() or not number.isdigit() or int(number) <= 0 for number in body.order_numbers):
                raise AppError("WB 请填写数字类型的组装任务 ID", code="invalid_order_scope")
        sync_payload.update(scope_version=1, date_from=start.isoformat(), date_to=end.isoformat())
    job = enqueue_job(session, job_type="pod_order_sync", payload=sync_payload, config_snapshot=vault.encrypt(config),
                      idempotency_key=f"pod-order-sync:{body.store_id}:{body.idempotency_key}", steps=[("sync", "同步平台订单并匹配 POD SKU"), ("images", "读取平台商品卡图片")])
    return {"job_id": job.id}


def validate_sync_credentials(config):
    if config['platform'] == 'wb':
        if not str(config.get('wildberries_token') or '').strip():
            raise AppError(f"WB 店铺 {config['store_name']} 未配置 API Token，请在店铺管理中填写后再同步订单",
                           code='order_store_credentials_missing')
    elif not str(config.get('ozon_client_id') or '').strip() or not str(config.get('ozon_api_key') or '').strip():
        raise AppError(f"OZON 店铺 {config['store_name']} 缺少 Client ID 或 API Key，请在店铺管理中填写后再同步订单",
                       code='order_store_credentials_missing')


class SyncImages(BaseModel):
    store_id: str
    idempotency_key: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=100)


@router.post("/images/sync")
def sync_images(body: SyncImages, session: Session = Depends(get_db)):
    config = store_config(session, body.store_id)
    validate_sync_credentials(config)
    job = enqueue_job(session, job_type="pod_order_image_sync", payload=body.model_dump(),
                      config_snapshot=vault.encrypt(config),
                      idempotency_key=f"pod-order-image-sync:{body.store_id}:{body.idempotency_key}",
                      steps=[("images", "读取已有订单的平台商品卡图片")])
    return {"job_id": job.id}


@router.post("/import")
def import_orders(body: ImportOrders, session: Session = Depends(get_db)):
    config = store_config(session, body.store_id)
    return invoke(manager(session).import_postings, body.postings, platform=config["platform"], store_id=body.store_id,
                  store_name=config["store_name"], wb_rub_to_cny_rate=config["wb_rate"])


@router.post("/manual")
def create_manual_order(body: ManualOrder, session: Session = Depends(get_db)):
    # Manual entry needs a real store identity, but no platform credentials/API call.
    store = session.get(Store, body.store_id)
    if not store or not store.enabled or store.platform.lower() not in {"ozon", "wb", "wildberries"}:
        raise AppError("请选择启用的 OZON 或 WB 店铺", code="invalid_order_store")
    return invoke(manager(session).create_manual_order, order_number=body.order_number,
                  items=[item.model_dump() for item in body.items], store_id=store.id,
                  store_name=store.name, platform="ozon" if store.platform.lower() == "ozon" else "wb")


@router.post("/rematch")
def rematch(session: Session = Depends(get_db)):
    return invoke(manager(session).rematch)


@router.get("/addresses")
def addresses(session: Session = Depends(get_db)):
    return {"items":saved_purchase_addresses(session)}


@router.post("/addresses")
def save_address(body: SavedAddress,session: Session = Depends(get_db)):
    return {"items":save_purchase_address(session,name=body.name,address=body.address,address_id=body.id,make_default=body.is_default)}


@router.delete("/addresses/{address_id}")
def delete_address(address_id:str,session: Session = Depends(get_db)):
    return {"items":delete_purchase_address(session,address_id)}


@router.get("/product-options")
def products(search: str = "", session: Session = Depends(get_db)):
    return invoke(manager(session).product_options, search=search)


@router.post("/batch/preview")
def preview(body: Selection, session: Session = Depends(get_db)):
    return invoke(manager(session).preview_batch, body.order_keys, body.merge_mode)


@router.post("/batch/queue")
def queue_batch(body: QueueBatch, session: Session = Depends(get_db)):
    return invoke(manager(session).queue_batch, body.order_keys, body.address, body.logistics, body.merge_mode)


@router.get("/batch/groups")
def groups(session: Session = Depends(get_db)):
    return invoke(manager(session).list_batch_groups, limit=500)


@router.get("/{key}/purchase-groups")
def order_purchase_groups(key: str, session: Session = Depends(get_db)):
    invoke(manager(session).get_order, key)
    groups = invoke(manager(session).list_batch_groups, limit=500)
    from .desktop_purchase_submit import recoverable_reserved_job
    result=[]
    for group in groups:
        if any(str(source.get("order_key") or "") == key for source in group.get("source_orders") or []):
            result.append({**group,"recovery_job":recoverable_reserved_job(session,group['id'])})
    return result


@router.post("/purchase/submit")
def submit_purchase(body: SubmitPurchase, session: Session = Depends(get_db)):
    orders = manager(session)
    target = invoke(orders.get_batch_group if body.target_type == "group" else orders.get_order, body.target_id)
    if body.target_type == "group":
        invoke(orders.assert_purchase_sources_active, [body.target_id])
    elif is_platform_order_cancelled(target) or target.get("tms_status") == "manual_tracking":
        raise AppError("平台订单已取消或已人工录入物流单号，禁止提交供应商采购", code="pod_order_error", status_code=409)
    external_key = str(target.get("out_order_id") or target.get("order_key") or body.target_id)
    from .desktop_purchase_submit import resume_reserved
    existing = invoke(resume_reserved, session, body.target_type, body.target_id)
    if existing is not None:
        return {"job_id": existing.id, "external_key": external_key}
    job = enqueue_job(session, job_type="pod_purchase_submit", payload=body.model_dump(exclude={"confirmation"}),
        config_snapshot=vault.encrypt(procurement_settings(session)), idempotency_key=f"pod-purchase:{external_key}:{body.idempotency_key}",
        max_attempts=1, steps=[("preflight", "核对外部订单号与既有结果"), ("submit", "提交供应商采购")])
    return {"job_id": job.id, "external_key": external_key}


@router.post("/purchase/sync")
def sync_purchase(supplier: Literal["", "hicustom", "s2b"] = "", session: Session = Depends(get_db)):
    job = enqueue_job(session, job_type="pod_purchase_sync", payload={"supplier": supplier},
        config_snapshot=vault.encrypt(procurement_settings(session)), idempotency_key=f"pod-purchase-sync:{supplier}:{uuid4().hex}",
        steps=[("sync", "查询供应商采购与物流状态")])
    return {"job_id": job.id}


@router.post("/manual-complete")
def complete(body: Selection, session: Session = Depends(get_db)):
    return invoke(manager(session).mark_orders_completed, body.order_keys)


@router.post("/{key}/manual-tracking")
def manual_tracking(key: str, body: ManualTracking, session: Session = Depends(get_db)):
    return invoke(manager(session).record_manual_tracking, key, body.tracking_number, body.tracking_company)


@router.post("/tms/batch-submit")
def submit_tms_batch(body: SubmitTmsBatch, session: Session = Depends(get_db)):
    raise AppError(
        "POD TMS 提交功能已弃用，请导出正式物流单号并人工处理",
        code="pod_tms_retired",
        status_code=410,
    )


@router.post("/{key}/rematch")
def rematch_one(key: str, session: Session = Depends(get_db)):
    invoke(manager(session).rematch, key)
    return invoke(manager(session).get_order, key)


@router.get("/purchase-export")
def export(purchase_date: str, session: Session = Depends(get_db)):
    path, _ = invoke(manager(session).export_ozon_purchases, purchase_date)
    return FileResponse(path, filename=path.name)


@router.post("/tracking-export")
def tracking(body: Selection, session: Session = Depends(get_db)):
    path, _ = invoke(manager(session).export_tms_tracking, body.order_keys)
    return FileResponse(path, filename=path.name)


@router.get("/{key}")
def detail(key: str, session: Session = Depends(get_db)):
    return invoke(manager(session).get_order, key)


@router.get("/{key}/tms/preview")
def preview_tms(key: str, session: Session = Depends(get_db)):
    raise AppError(
        "POD TMS 提交功能已弃用，请导出正式物流单号并人工处理",
        code="pod_tms_retired",
        status_code=410,
    )


@router.post("/{key}/tms/submit")
def submit_tms(key: str, body: SubmitTms, session: Session = Depends(get_db)):
    raise AppError(
        "POD TMS 提交功能已弃用，请导出正式物流单号并人工处理",
        code="pod_tms_retired",
        status_code=410,
    )


class Recipient(BaseModel):
    recipient: dict[str, str]


@router.put("/{key}/recipient")
def recipient(key: str, body: Recipient, session: Session = Depends(get_db)):
    return invoke(manager(session).update_recipient, key, body.recipient)


class Match(BaseModel):
    sku: str = Field(min_length=1, max_length=200)


@router.post("/{key}/items/{line}/match")
def match(key: str, line: int, body: Match, session: Session = Depends(get_db)):
    return invoke(manager(session).manual_match, key, line, body.sku)
