from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp.core.database import get_db
from erp.core.config import get_settings
from erp.core.errors import AppError, ConflictError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.core.security import vault
from erp.integrations.ozon import OzonApiError, OzonSellerAdapter
from erp.integrations.ozon_performance import OzonPerformanceClient
from erp.models import IntegrationConnection, Listing, Store, StoreListing
from erp.schemas import StoreCreate, StoreUpdate
from erp.serializers import store_dict
from erp.services.config_resolution import build_job_snapshot, resolve_job_snapshot
from erp.services.ozon_categories import load_category_snapshot, save_category_snapshot
from erp.services.jobs import enqueue_job
from uuid import uuid4


router = APIRouter(prefix="/stores", tags=["stores"])


def _get(session: Session, store_id: str) -> Store:
    item = session.get(Store, store_id)
    if not item:
        raise NotFoundError("店铺不存在")
    return item


@router.get("")
def list_stores(session: Session = Depends(get_db)) -> dict:
    items = session.scalars(select(Store).order_by(Store.platform, Store.name)).all()
    return {"items": [store_dict(item) for item in items]}


@router.post("", status_code=201)
def create_store(request: StoreCreate, session: Session = Depends(get_db)) -> dict:
    if request.connection_id and not session.get(IntegrationConnection, request.connection_id):
        raise NotFoundError("关联的接口连接不存在")
    secrets = dict(request.secrets)
    if request.platform == "ozon" and request.external_account_id:
        secrets.setdefault("client_id", request.external_account_id)
    item = Store(
        platform=request.platform,
        name=request.name.strip(),
        external_account_id=request.external_account_id or None,
        offer_prefix=request.offer_prefix.strip(),
        currency=request.currency.upper(),
        enabled=request.enabled,
        connection_id=request.connection_id,
        config_json=dumps(request.config),
        secrets_encrypted=vault.encrypt(secrets),
    )
    session.add(item)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise ConflictError("该平台账号已经存在") from error
    session.refresh(item)
    return store_dict(item)


@router.patch("/{store_id}")
def update_store(store_id: str, request: StoreUpdate, session: Session = Depends(get_db)) -> dict:
    item = _get(session, store_id)
    values = request.model_dump(exclude_unset=True)
    previous_prefix = item.offer_prefix
    for key in ("name", "external_account_id", "offer_prefix", "currency", "enabled", "connection_id"):
        if key in values:
            setattr(item, key, values[key])
    if "offer_prefix" in values:
        item.offer_prefix = str(item.offer_prefix or "").strip()
        if item.offer_prefix != previous_prefix:
            # Offer IDs become external identities after the first Ozon import.
            # Only drafts that have never been submitted are safe to update.
            drafts = session.scalars(
                select(StoreListing).where(
                    StoreListing.store_id == item.id,
                    StoreListing.external_task_id == "",
                    StoreListing.external_product_id == "",
                )
            ).all()
            listing_ids = {row.listing_id for row in drafts}
            bases = {
                row.id: row.base_offer_id
                for row in session.scalars(select(Listing).where(Listing.id.in_(listing_ids))).all()
            } if listing_ids else {}
            for draft in drafts:
                base_offer_id = bases.get(draft.listing_id, "")
                if base_offer_id:
                    draft.offer_id = f"{item.offer_prefix}{base_offer_id}"
    if item.currency:
        item.currency = item.currency.upper()
    if "config" in values:
        item.config_json = dumps(values["config"] or {})
    if "secrets" in values or (item.platform == "ozon" and "external_account_id" in values):
        existing = vault.decrypt(item.secrets_encrypted) if item.secrets_encrypted else {}
        merged = vault.merge(existing, values.get("secrets") or {})
        if item.platform == "ozon" and item.external_account_id:
            # Seller API credentials are a pair. Never keep an old encrypted
            # Client ID after the visible account ID has been edited.
            merged["client_id"] = str(item.external_account_id).strip()
        item.secrets_encrypted = vault.encrypt(merged)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise ConflictError("该平台账号已经存在") from error
    session.refresh(item)
    return store_dict(item)


@router.delete("/{store_id}")
def delete_store(store_id: str, session: Session = Depends(get_db)) -> dict:
    item = _get(session, store_id)
    if item.store_listings:
        raise ConflictError("该店铺已有上架记录，不能删除；可以将店铺停用")
    session.delete(item)
    session.commit()
    return {"deleted": True}


@router.post("/{store_id}/test")
def test_store(store_id: str, session: Session = Depends(get_db)) -> dict:
    item = _get(session, store_id)
    if item.platform == "amazon":
        from erp.services.amazon_listings import new_job, store as amazon_store
        job = new_job(session, amazon_store(session, item.id), 'amazon_metadata', {'operation':'test'})
        return {'job_id':job.id}
    if item.platform != "ozon":
        secrets = vault.decrypt(item.secrets_encrypted) if item.secrets_encrypted else {}
        if not secrets.get("token"):
            raise AppError("WB 店铺缺少 Token", code="store_incomplete")
        return {"ok": True, "note": "凭据已配置；真实读取由 WB 业务模块执行"}
    snapshot = build_job_snapshot(session, store_id=item.id)
    if get_settings().multiuser_enabled:
        job = enqueue_job(session, job_type="store_connection_test",
                          payload={"store_id": item.id}, config_snapshot=snapshot,
                          idempotency_key=f"store-connection-test:{item.id}:{uuid4().hex}",
                          priority=10,
                          steps=[("test", "读取 Ozon Seller 类目根节点")])
        return {"job_id": job.id}
    try:
        return OzonSellerAdapter(resolve_job_snapshot(snapshot)).test_connection()
    except OzonApiError as error:
        raise AppError(str(error), code="ozon_store_connection_failed") from error


@router.post("/{store_id}/test-performance")
def test_performance_api(store_id: str, session: Session = Depends(get_db)) -> dict:
    item = _get(session, store_id)
    if item.platform != "ozon":
        raise AppError("广告 API 仅适用于 OZON 店铺", code="unsupported_platform")
    secrets = vault.decrypt(item.secrets_encrypted) if item.secrets_encrypted else {}
    client = OzonPerformanceClient(
        str(secrets.get("performance_client_id") or ""),
        str(secrets.get("performance_client_secret") or ""),
    )
    campaigns = client.campaigns()
    return {"ok": True, "campaign_count": len(campaigns), "note": "广告 API 连接成功"}


@router.get("/{store_id}/warehouses")
def list_warehouses(store_id: str, session: Session = Depends(get_db)) -> dict:
    item = _get(session, store_id)
    if item.platform != "ozon":
        raise AppError("当前仅支持读取 OZON 仓库", code="unsupported_platform")
    snapshot = build_job_snapshot(session, store_id=item.id)
    return {"items": OzonSellerAdapter(resolve_job_snapshot(snapshot)).warehouses()}


@router.get("/{store_id}/categories")
def list_categories(store_id: str, language: str = "ZH_HANS", session: Session = Depends(get_db)) -> dict:
    item = _get(session, store_id)
    if item.platform != "ozon": raise AppError("当前仅支持读取 OZON 类目", code="unsupported_platform")
    adapter = OzonSellerAdapter(resolve_job_snapshot(build_job_snapshot(session, store_id=item.id)))
    return {"items": adapter.category_tree(language)}


@router.get("/{store_id}/category-options")
def category_options(store_id: str, language: str = "ZH_HANS", search: str = "", limit: int = 0, session: Session = Depends(get_db)) -> dict:
    item = _get(session, store_id)
    if item.platform != "ozon": raise AppError("当前仅支持读取 OZON 类目", code="unsupported_platform")
    snapshot = load_category_snapshot(item.id, language)
    if snapshot is None:
        raise AppError("本地还没有该店铺的 Ozon 类目快照，请先同步类目树", code="ozon_categories_not_synced")
    options = snapshot.get("options", [])
    if search.strip(): options = [row for row in options if search.strip().casefold() in str(row.get("display", "")).casefold()]
    return {"options": options[:max(1, min(limit, 200))] if limit else options, "total": len(options), "synced_at": snapshot.get("synced_at")}


@router.post("/{store_id}/categories/sync")
def sync_categories(store_id: str, language: str = "ZH_HANS", session: Session = Depends(get_db)) -> dict:
    item = _get(session, store_id)
    if item.platform != "ozon":
        raise AppError("当前仅支持同步 OZON 类目", code="unsupported_platform")
    adapter = OzonSellerAdapter(resolve_job_snapshot(build_job_snapshot(session, store_id=item.id)))
    try:
        tree = adapter.category_tree(language)
    except OzonApiError as error:
        raise AppError(str(error), code="ozon_category_sync_failed") from error
    return save_category_snapshot(item.id, item.name, language, tree)


@router.get("/{store_id}/category-attributes")
def category_attributes(store_id: str, description_category_id: int, type_id: int, language: str = "ZH_HANS", session: Session = Depends(get_db)) -> dict:
    item = _get(session, store_id)
    if item.platform != "ozon": raise AppError("当前仅支持读取 OZON 属性", code="unsupported_platform")
    adapter = OzonSellerAdapter(resolve_job_snapshot(build_job_snapshot(session, store_id=item.id)))
    return {"items": adapter.category_attributes(description_category_id, type_id, language)}


@router.get("/{store_id}/category-attribute-values")
def category_attribute_values(store_id: str, description_category_id: int, type_id: int, attribute_id: int, language: str = "ZH_HANS", session: Session = Depends(get_db)) -> dict:
    item = _get(session, store_id)
    if item.platform != "ozon": raise AppError("当前仅支持读取 OZON 属性值", code="unsupported_platform")
    adapter = OzonSellerAdapter(resolve_job_snapshot(build_job_snapshot(session, store_id=item.id)))
    return {"items": adapter.category_attribute_values(description_category_id, type_id, attribute_id, language)}


@router.get("/{store_id}/promotions")
def promotions(store_id: str, session: Session = Depends(get_db)) -> dict:
    item = _get(session, store_id)
    if item.platform != "ozon": raise AppError("当前仅支持读取 OZON 促销", code="unsupported_platform")
    adapter = OzonSellerAdapter(resolve_job_snapshot(build_job_snapshot(session, store_id=item.id)))
    return {"items": adapter.promotions()}


@router.post('/{store_id}/ozon-options/{kind}')
def personal_ozon_options(store_id: str, kind: str, description_category_id: int = 0, type_id: int = 0, refresh: bool = False, session: Session = Depends(get_db)) -> dict:
    if kind not in {'warehouses', 'promotions', 'category_attributes', 'category_tree'}:
        raise AppError('不支持的 Ozon 资料类型', code='unsupported_options')
    item = _get(session, store_id)
    if item.platform != 'ozon':
        raise AppError('当前仅支持 OZON', code='unsupported_platform')
    params = {}
    if kind == 'category_attributes':
        if description_category_id <= 0 or type_id <= 0: raise AppError('请选择完整 Ozon 类目', code='invalid_category')
        params = {'category_id': description_category_id, 'type_id': type_id, 'language': 'ZH_HANS'}
    from erp.services.ozon_categories import load_options_cache
    cached = load_category_snapshot(store_id) if kind == 'category_tree' else load_options_cache(store_id, kind, params)
    if not refresh and cached is not None:
        return {'items': cached.get('tree', []) if kind == 'category_tree' else cached, 'cached': True}
    from erp.models import Job
    from erp.services.jobs import task_identity
    actor = task_identity.get() or {}
    for pending in session.scalars(select(Job).where(Job.type == 'catalog_ozon_options', Job.status.in_(['queued', 'running', 'retrying']))):
        payload = loads(pending.payload_json, {})
        if pending.created_by == actor.get('id', '') and pending.target_worker_id == actor.get('worker_id', '') and payload.get('store_id') == store_id and payload.get('kind') == kind and payload.get('params') == params:
            return {'job_id': pending.id}
    if not get_settings().multiuser_enabled:
        adapter = OzonSellerAdapter(resolve_job_snapshot(build_job_snapshot(session, store_id=item.id)))
        return {'items': getattr(adapter, kind)(**params)}
    job = enqueue_job(session, job_type='catalog_ozon_options', payload={'store_id': store_id, 'kind': kind, 'params': params},
        config_snapshot=build_job_snapshot(session, store_id=item.id),
        idempotency_key='ozon-options:' + uuid4().hex, steps=[('read', '读取店铺仓库' if kind == 'warehouses' else '读取店铺促销')])
    return {'job_id': job.id}
