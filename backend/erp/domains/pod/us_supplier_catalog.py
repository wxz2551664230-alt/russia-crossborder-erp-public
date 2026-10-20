"""Cloud storage and coordination for verified US supplier facts; no supplier calls."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from erp.core.errors import AppError, ConflictError, NotFoundError
from erp.core.config import get_settings
from erp.core.jsonutil import dumps, loads
from erp.core.task_identity import task_identity
from erp.domains.pod.integrations.common import load_supplier_connection
from erp.domains.pod.models import PodSupplierCatalogSnapshot
from erp.models import IntegrationConnection, Job, utc_now
from erp.serializers import job_dict
from erp.services.jobs import enqueue_job

JOB_TYPE = "pod_supplier_us_catalog_sync"
SCOPE = "amazon_us"


def current(session: Session) -> PodSupplierCatalogSnapshot | None:
    return session.scalar(select(PodSupplierCatalogSnapshot).where(
        PodSupplierCatalogSnapshot.provider == SCOPE,
        PodSupplierCatalogSnapshot.is_current.is_(True),
    ).order_by(PodSupplierCatalogSnapshot.created_at.desc(), PodSupplierCatalogSnapshot.id))


def read(session: Session) -> tuple[PodSupplierCatalogSnapshot | None, dict]:
    snapshot = current(session)
    data = loads(snapshot.payload_json, {}) if snapshot else {}
    return snapshot, {"market": "US", "products": [], "categories": [], "details": {}, **data}


def view(session: Session, *, search: str = "", category: str = "", page: int = 1, page_size: int = 50) -> dict:
    snapshot, data = read(session)
    items = data["products"]
    if search:
        term = search.strip().casefold()
        items = [x for x in items if term in " ".join(str(x.get(k) or "") for k in ("name", "en_name", "cn_name", "code", "source_id")).casefold()]
    if category:
        items = [x for x in items if str(x.get("category_id") or "") == category or any(str(c.get("id") or c.get("category_id") or "") == category for c in (x.get("categorys") or x.get("categories") or []) if isinstance(c, dict))]
    start = (page - 1) * page_size
    active = session.scalar(select(Job).where(Job.type.in_([JOB_TYPE, 'pod_supplier_us_freight_quote']),
        Job.status.in_(["queued", "running", "retrying", "paused"])).order_by(Job.created_at))
    return {"market": "US", "cached": bool(snapshot), "synced_at": data.get("synced_at"), "active_job": job_dict(active) if active else None,
            "total": len(items), "product_count": len(data["products"]), "items": items[start:start + page_size],
            "categories": data["categories"], "provider_status": {
                "s2b": {"status": "cached" if snapshot else "not_synced", "product_count": len(data["products"])},
                "hicustom": {"status": "awaiting_documentation", "message": "海外货盘存在，但美国生产商品的开放接口尚未核实"},
            }}


def product(session: Session, key: str) -> dict:
    _, data = read(session)
    item = next((x for x in data["products"] if x["catalog_key"] == key), None)
    if not item:
        raise NotFoundError("美国货盘商品不存在，请先同步美国货盘")
    return {"market": "US", "product": item, "detail": data["details"].get(key),
            "detail_cached": key in data["details"]}


def composition_detail(session: Session, key: str) -> dict:
    """The composer never substitutes domestic facts or fetches a vendor on GET."""
    value = product(session, key)
    detail = value["detail"]
    if (not key.startswith("s2b:") or not detail
            or str(detail.get("produce_country") or "").upper() != "US"
            or str(detail.get("id") or "") != value["product"]["source_id"]):
        raise ConflictError("请先在美国货盘读取该商品的规格与成本详情，再进入合成")
    # Normalize cached facts with the currently verified S2B units and CNY price.
    from erp.domains.pod.integrations.s2b import S2BClient
    return S2BClient.normalize_detail(detail)


def composer_catalog(session: Session) -> dict:
    from erp.domains.pod.supplier_catalog import _decorate_category
    snapshot, data = read(session)
    return {"cached": bool(snapshot), "synced_at": data.get("synced_at"),
            "products": data["products"], "product_count": len(data["products"]),
            "categories": [{"id": "s2b:all", "category_key": "s2b:all", "supplier": "s2b", "name": "S2B 美国货盘",
                            "children": [_decorate_category(c, "s2b") for c in data["categories"]]}],
            "previews": {p["catalog_key"]: p.get("preview_url") or p.get("view_image_src") for p in data["products"]},
            "provider_counts": {"s2b": len(data["products"])}}


def _lock_connection(session: Session, connection_id: str) -> None:
    # A common existing row serializes enqueue/result acceptance, including the
    # first snapshot when there is no snapshot row to lock yet.
    session.scalar(select(IntegrationConnection).where(IntegrationConnection.id == connection_id).with_for_update())


def create_job(session: Session, *, key: str = "") -> dict:
    if not get_settings().multiuser_enabled:
        raise ConflictError("美国货盘通过登录客户端在本机读取，请使用多人共享模式")
    connection = load_supplier_connection(session, "s2b")
    _lock_connection(session, connection.connection_id)
    active = session.scalar(select(Job).where(Job.type == JOB_TYPE,
        Job.status.in_(["queued", "running", "retrying", "paused"])).order_by(Job.created_at))
    if active:
        actor = task_identity.get() or {}
        value = loads(active.payload_json, {})
        if (active.created_by == actor.get("id", "") and active.target_worker_id == actor.get("worker_id", "")
                and value.get("product_key", "") == key):
            return {"job": job_dict(active), "reused": True}
        raise ConflictError("美国货盘已有读取任务，请完成后再读取，避免重复请求")
    snapshot, _ = read(session)
    if key:
        value = product(session, key)["product"]
        source_id = value["source_id"]
    else:
        source_id = ""
    job = enqueue_job(session, job_type=JOB_TYPE,
        payload={"market": "US", "kind": "detail" if key else "sync", "product_key": key,
                 "source_id": source_id, "connection_ids": {"s2b": connection.connection_id},
                 "base_snapshot_id": snapshot.id if snapshot else None},
        config_snapshot=dumps({"market": "US", "providers": ["s2b"], "read_only": True}),
        idempotency_key=f"{JOB_TYPE}:{uuid.uuid4().hex}", max_attempts=1,
        steps=[("sync", "读取美国货盘商品详情" if key else "同步 S2B 美国生产货盘")])
    return {"job": job_dict(job), "reused": False}


def accept(session: Session, job: Job, result: dict[str, Any]) -> dict:
    from erp.services.catalog_personal_publications import _public_result
    _public_result(result)
    request = loads(job.payload_json, {})
    if job.type != JOB_TYPE or request.get("market") != "US" or result.get("market") != "US" or result.get("kind") != request.get("kind"):
        raise ConflictError("美国货盘结果与任务不匹配")
    _lock_connection(session, request["connection_ids"]["s2b"])
    snapshot, data = read(session)
    if (snapshot.id if snapshot else None) != request.get("base_snapshot_id"):
        raise ConflictError("美国货盘已更新，请重新读取，原资料未覆盖")
    if request["kind"] == "sync":
        products, categories = result.get("products"), result.get("categories")
        if (not isinstance(products, list) or len(products) > 100_000 or not isinstance(categories, list)
                or len(categories) > 20_000 or any(not isinstance(c, dict) for c in categories)):
            raise ConflictError("美国货盘列表格式无效")
        keys = set()
        for item in products:
            if (not isinstance(item, dict) or item.get("supplier") != "s2b"
                    or str(item.get("produce_country") or "").upper() != "US"
                    or not str(item.get("source_id") or "").isdigit()
                    or item.get("catalog_key") != f"s2b:{item['source_id']}"
                    or item["catalog_key"] in keys):
                raise ConflictError("美国货盘结果含未核实国家或重复商品")
            keys.add(item["catalog_key"])
        # New list facts can change production variants/costs. Drop old detail
        # caches rather than presenting stale facts as matching the new list.
        data.update(products=products, categories=categories, details={}, synced_at=utc_now().isoformat())
    elif request["kind"] == "detail":
        detail = result.get("detail")
        if (not isinstance(detail, dict) or str(detail.get("produce_country") or "").upper() != "US"
                or str(detail.get("id") or "") != request["source_id"]
                or result.get("product_key") != request["product_key"]):
            raise ConflictError("美国商品详情国家或编号不匹配")
        product(session, request["product_key"])
        detail = {**detail, "supplier": "s2b", "catalog_key": request["product_key"], "fetched_at": utc_now().isoformat()}
        data["details"][request["product_key"]] = detail
    else:
        raise ConflictError("美国货盘读取类型无效")
    if request["kind"] == "detail" and snapshot:
        snapshot.payload_json = dumps(data)
        session.flush()
        return {"snapshot_id": snapshot.id, "product_count": snapshot.product_count, "errors": []}
    if snapshot:
        snapshot.is_current = False
    item = PodSupplierCatalogSnapshot(provider=SCOPE, is_current=True,
        product_count=len(data["products"]), category_count=len(data["categories"]),
        fetched_at=str(data.get("synced_at") or ""), payload_json=dumps(data))
    session.add(item)
    session.flush()
    return {"snapshot_id": item.id, "product_count": item.product_count, "errors": []}
