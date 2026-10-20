from __future__ import annotations

import asyncio
import json
from uuid import uuid4
from contextlib import closing
from pathlib import Path
from functools import lru_cache

from cryptography.fernet import Fernet
from sqlalchemy.orm import Session
from sqlalchemy import select, tuple_

from erp.core.config import get_settings
from erp.core.errors import AppError
from erp.core.security import vault
from erp.domains.orders.legacy_engine import OrderManager, OzonClient, WildberriesOrderClient, HicustomOrderQueue, TmsClient, OrderError, utc_now, is_platform_order_cancelled
from erp.domains.orders.products import ProductLibrary
from erp.core.jsonutil import loads
from erp.domains.pod.integrations.common import SupplierConnectionConfig
from erp.domains.pod.integrations.hicustom import HicustomClient
from erp.domains.pod.integrations.s2b import S2BClient
from erp.domains.pod.gallery import mark_ordered_gallery_codes
from erp.domains.pod.wb_export import _WbContentClient
from erp.models import IntegrationConnection, PublicationStore, StoreListing
from erp.services.jobs import JobContext, job_handler


class ErpOrderManager(OrderManager):
    def _attach_platform_images(self, orders):
        keys = {(str(order.get('store_id') or ''), str(item.get('offer_id') or ''))
                for order in orders for item in order.get('items') or []
                if order.get('store_id') and item.get('offer_id')}
        if not keys:
            return orders
        images = {}
        session = self.library.session
        for model in (PublicationStore, StoreListing):
            rows = session.scalars(select(model).where(tuple_(model.store_id, model.offer_id).in_(keys)).order_by(model.updated_at.desc())).all()
            for row in rows:
                assets = loads(row.assets_json, {})
                url = str(assets.get('primary_image') or '').strip() if isinstance(assets, dict) else ''
                if url.startswith(('https://', 'http://')):
                    images.setdefault((row.store_id, row.offer_id), url)
        for order in orders:
            for item in order.get('items') or []:
                cached = str(item.get('platform_image_url') or '')
                item['platform_image_url'] = cached or images.get((str(order.get('store_id') or ''), str(item.get('offer_id') or '')), '')
                item['platform_image_source'] = 'platform' if cached else 'listing' if item['platform_image_url'] else ''
        return orders

    def _attach_catalog_images(self, orders):
        cached = {}
        for order in orders:
            for item in order.get('items') or []:
                if item.get('image_url'):
                    continue
                sku = str(item.get('matched_sku') or '').strip()
                if not sku:
                    continue
                if sku not in cached:
                    try:
                        assets = self.library.get_product(sku).get('oss_assets') or []
                        cached[sku] = next((str(asset.get('oss_url') or '') for asset in assets
                                            if asset.get('asset_type') == 'rendering'
                                            and str(asset.get('oss_url') or '').startswith('https://')), '')
                    except ValueError:
                        cached[sku] = ''
                item['image_url'] = cached[sku]

    def list_orders(self, **kwargs):
        self._summary_only = True
        try:
            with closing(self._connect()) as db:
                rows = db.execute("SELECT s.posting_number, MAX(g.paid_at), MAX(NULLIF(g.tracking_number,'')) FROM batch_order_groups g JOIN batch_order_sources s ON s.group_id=g.id WHERE g.submission_status='submitted' GROUP BY s.posting_number").fetchall()
            self._fulfillment_summary = {row[0]: (row[1], row[2]) for row in rows}
            result = super().list_orders(**kwargs)
            self._attach_platform_images(result['orders'])
            self._attach_catalog_images(result['orders'])
            return result
        finally:
            self._summary_only = False
            self._fulfillment_summary = {}

    def _public_order(self, row, items=None):
        if not getattr(self, '_summary_only', False):
            result = super()._public_order(row, items)
            self._attach_platform_images([result])
            return result
        # The list needs no per-item finance/history scans. Expand those only in detail.
        recipient = self._decrypt_recipient(row['recipient_enc'])
        result = {name: row[name] for name in row.keys() if name not in {'recipient_enc', 'financial_data_json'}}
        result.update(order_key=str(row['posting_number']), posting_number=str(row['external_order_id'] or row['posting_number']),
                      items=items or [], recipient_ready=all(recipient.get(name) for name in self.REQUIRED_ADDRESS_FIELDS),
                      last_ship_at=str(row['shipment_date'] or ''))
        fulfillment = self._fulfillment_summary.get(row['posting_number'], (None, None))
        if is_platform_order_cancelled(row):
            status = 'cancelled_purchased' if row['submission_status'] == 'submitted' else 'cancelled'
        elif row['tms_status'] == 'manual_tracking':
            status = 'manual_tracking'
        elif row['tms_status'] in {'submitted', 'completed'}:
            status = 'completed'
        elif row['tms_status'] == 'failed':
            status = 'tms_error'
        elif row['match_status'] != 'matched':
            status = 'pending_match'
        elif fulfillment[0] and fulfillment[1]:
            status = 'purchased_in_transit' if row['platform'] == 'wb' else 'purchased_pending_tms'
        elif fulfillment[0]:
            status = 'waiting_official_tracking'
        elif row['submission_status'] == 'submitted':
            status = 'supplier_processing'
        else:
            status = 'pending_purchase'
        result['workflow_status'] = status
        return result

    def _order_key(self, platform: str, store_id: str, external_order_id: str) -> str:
        # Preserve supplier out_order_id / historical composite keys after store binding.
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT posting_number FROM ozon_orders WHERE platform=? AND store_id=? AND external_order_id=?",
                                      (platform, store_id, external_order_id)).fetchall()
        if len(rows) > 1:
            raise AppError("同店订单身份存在冲突，请先核对历史记录", code="order_identity_conflict")
        return str(rows[0][0]) if rows else super()._order_key(platform, store_id, external_order_id)


@lru_cache(maxsize=4)
def _initialize(root: Path) -> None:
    # No legacy worker is started: imported queued/unknown purchases are never replayed.
    OrderManager(root, None, Fernet(vault._key())).initialize()


def manager(session: Session) -> OrderManager:
    root = get_settings().data_dir / "orders" / "pod"
    _initialize(root)
    return ErpOrderManager(root, ProductLibrary(session), Fernet(vault._key()))


def _first_image(value) -> str:
    if isinstance(value, str):
        return value.strip() if value.strip().startswith(('https://', 'http://')) else ''
    if isinstance(value, list):
        return next((url for item in value if (url := _first_image(item))), '')
    if isinstance(value, dict):
        return _first_image(next((value[key] for key in ('big', 'large', 'url', 'link', 'c516x688', 'c246x328') if value.get(key)), ''))
    return ''


def fetch_platform_images(config: dict, offers: list[str], check=lambda: None) -> dict[str, str]:
    """Read platform images on the task's computer; no shared DB access."""
    check()
    images: dict[str, str] = {}
    if config['platform'] == 'ozon':
        async def fetch_ozon():
            client = OzonClient(config)
            try:
                for start in range(0, len(offers), 100):
                    check()
                    payload = await client._post('/v3/product/info/list', {'offer_id': offers[start:start + 100], 'product_id': [], 'sku': []})
                    for item in payload.get('items') or []:
                        offer = str(item.get('offer_id') or '')
                        image = _first_image(item.get('primary_image')) or _first_image(item.get('images'))
                        if offer in offers and image:
                            images[offer] = image
            finally:
                await client.aclose()
        if offers:
            asyncio.run(fetch_ozon())
    else:
        client = _WbContentClient({'store': {'secrets': {'token': config.get('wildberries_token')}}})
        try:
            for offer in offers:
                check()
                card = client.find_card(offer)
                if card and str(card.get('vendorCode') or '').casefold() == offer.casefold():
                    image = _first_image(card.get('photos'))
                    if image:
                        images[offer] = image
        finally:
            client.close()
    check()
    return images


def sync_platform_images(session: Session, config: dict) -> dict:
    orders = manager(session)
    offers = orders.platform_image_offers(config['store_id'])
    images = fetch_platform_images(config, offers)
    return {'offers': len(offers), 'with_platform_image': len(images),
            'order_items_updated': orders.update_platform_images(config['store_id'], images)}


def procurement_settings(session: Session) -> dict:
    result: dict = {}
    for row in session.query(IntegrationConnection).filter(IntegrationConnection.enabled.is_(True)).all():
        values = {**loads(row.config_json, {}), **vault.decrypt(row.secrets_encrypted)}
        if row.provider in {"hicustom", "s2b", "tms"}:
            result[f"{row.provider}_base_url"] = row.base_url
            result.update({f"{row.provider}_{key}": value for key, value in values.items()})
        elif row.provider == "procurement": result.update(values)
    return result


def saved_purchase_addresses(session: Session) -> list[dict]:
    row = session.query(IntegrationConnection).filter(IntegrationConnection.provider == "procurement").first()
    values = vault.decrypt(row.secrets_encrypted) if row else {}
    # `addresses` is the key used by the first ERP migration. Keep reading it
    # so existing encrypted addresses do not disappear after the UI moved to
    # the clearer `saved_addresses` key.
    addresses = values.get("saved_addresses")
    if not isinstance(addresses, list):
        addresses = values.get("addresses") if isinstance(values.get("addresses"), list) else []
    return [dict(item) for item in addresses if isinstance(item, dict)]


def save_purchase_address(session: Session, *, name: str, address: dict, address_id: str = "", make_default: bool = False) -> list[dict]:
    row = session.query(IntegrationConnection).filter(IntegrationConnection.provider == "procurement").first()
    if not row:
        row = IntegrationConnection(provider="procurement", name="采购与物流", enabled=True)
        session.add(row); session.flush()
    secrets = vault.decrypt(row.secrets_encrypted)
    items = saved_purchase_addresses(session); identity = address_id or uuid4().hex
    clean = {key: str(address.get(key) or "").strip() for key in (
        "country", "region", "city", "area", "street", "postcode", "firstname", "lastname",
        "mobile_phone", "telephone", "email",
    )}
    # Procurement accepts a full recipient name in firstname; lastname is
    # optional there and must not prevent saving the same usable address.
    required = {'country': '国家', 'region': '省/州', 'city': '城市',
                'street': '详细地址', 'firstname': '名字（收件人）', 'mobile_phone': '手机'}
    missing = [label for key, label in required.items() if not clean.get(key)]
    if missing:
        raise AppError('常用地址缺少：' + '、'.join(missing), code="purchase_address_incomplete")
    current = next((item for item in items if item.get("id") == identity), None)
    value = {"id": identity, "name": name.strip()[:100] or "常用地址", "is_default": bool(make_default), "address": clean}
    if current: items[items.index(current)] = value
    else: items.append(value)
    if make_default or len(items) == 1:
        for item in items: item["is_default"] = item["id"] == identity
    secrets["saved_addresses"] = items
    # Remove the legacy duplicate after the first successful save. Keeping two
    # independently editable address arrays caused old values to reappear.
    secrets.pop("addresses", None)
    row.secrets_encrypted = vault.encrypt(secrets); session.commit()
    return items


def delete_purchase_address(session: Session, address_id: str) -> list[dict]:
    row = session.query(IntegrationConnection).filter(IntegrationConnection.provider == "procurement").first()
    if not row: return []
    secrets = vault.decrypt(row.secrets_encrypted); items = [item for item in saved_purchase_addresses(session) if item.get("id") != address_id]
    if items and not any(item.get("is_default") for item in items): items[0]["is_default"] = True
    secrets["saved_addresses"] = items
    secrets.pop("addresses", None)
    row.secrets_encrypted = vault.encrypt(secrets); session.commit(); return items


class AsyncSupplier:
    def __init__(self, client): self.client = client
    async def _call(self, name, *args, **kwargs):
        try: return await asyncio.to_thread(getattr(self.client, name), *args, **kwargs)
        except AppError as error:
            raise OrderError(str(error), status_code=error.status_code, outcome_unknown=bool((error.details or {}).get("outcome_unknown"))) from None
    async def order_by_out_order_id(self, value): return await self._call("order_by_out_order_id", value)
    async def create_order(self, value): return await self._call("create_order", value)
    async def gallery_exists(self, value): return await self._call("gallery_exists", value)
    async def upload_gallery(self, **kwargs): return await self._call("upload_gallery", **kwargs)
    async def create_product(self, value): return await self._call("create_product", value)
    async def custom_product(self, value): return await self._call("custom_product", value)
    async def calculate_products(self, **kwargs): return await self._call("calculate_products", **kwargs)
    def close(self): self.client.close()


def supplier_client(session: Session, supplier: str, settings: dict | None = None) -> AsyncSupplier:
    if settings is not None:
        prefix = supplier + "_"
        values = {key.removeprefix(prefix): value for key, value in settings.items() if key.startswith(prefix)}
        cfg = SupplierConnectionConfig(provider=supplier, connection_id="snapshot", base_url=str(values.pop("base_url", "")), config=values, secrets=values)
        return AsyncSupplier(S2BClient(cfg) if supplier == "s2b" else HicustomClient(cfg))
    row = session.query(IntegrationConnection).filter(IntegrationConnection.provider == supplier, IntegrationConnection.enabled.is_(True)).first()
    if not row: raise AppError(f"{supplier.upper()} 接口未配置", code="supplier_connection_missing", status_code=503)
    cfg = SupplierConnectionConfig(provider=supplier, connection_id=row.id, base_url=row.base_url, config=loads(row.config_json, {}), secrets=vault.decrypt(row.secrets_encrypted))
    return AsyncSupplier(S2BClient(cfg) if supplier == "s2b" else HicustomClient(cfg))


def order_queue(session: Session, settings: dict | None = None):
    clients = []
    async def loader(supplier="hicustom"):
        client = supplier_client(session, supplier, settings); clients.append(client); return client
    snapshot = settings or procurement_settings(session)
    return HicustomOrderQueue(manager(session), lambda: snapshot, loader), clients


def purchase_read_targets(orders, supplier: str = "") -> list[dict]:
    """Only identifiers needed for the original supplier status queries."""
    targets=[]; seen=set()
    for group in orders.list_batch_groups(limit=500):
        provider=str(group.get('supplier') or 'hicustom')
        external=str(group.get('out_order_id') or '').strip()
        key=(provider,external)
        if (group.get('submission_status') not in {'submitted','unknown'} or not external
                or (supplier and provider != supplier) or key in seen):
            continue
        seen.add(key)
        targets.append({'supplier':provider,'out_order_id':external})
    return targets


def fetch_purchase_status(settings: dict, targets: list[dict], check=lambda: None) -> list[dict]:
    """Read supplier facts on a personal PC without opening an order database."""
    async def fetch():
        clients={}; results=[]
        try:
            for target in targets:
                check()
                provider=target['supplier']; external=target['out_order_id']
                if provider not in {'hicustom','s2b'}:
                    raise AppError('不支持的采购供应商',code='invalid_purchase_supplier')
                if provider not in clients:
                    clients[provider]=supplier_client(None,provider,settings)
                try:
                    value=await clients[provider].order_by_out_order_id(external)
                    results.append({**target,'remote':value,'failed':False})
                except Exception:
                    # Supplier error strings may contain credentials or addresses.
                    results.append({**target,'remote':None,'failed':True})
                check()
        finally:
            for client in clients.values():client.close()
        return results
    return asyncio.run(fetch())


@job_handler("pod_purchase_submit")
def purchase_job(context: JobContext) -> None:
    context.start_step("preflight"); orders = manager(context.session); target_type = context.payload["target_type"]; target_id = context.payload["target_id"]
    target = orders.get_batch_group(target_id) if target_type == "group" else orders.get_order(target_id)
    if target["submission_status"] == "submitted":
        context.complete_step("preflight", details={"already_submitted": True}); context.complete_step("submit"); return
    context.complete_step("preflight", details={"out_order_id": target.get("out_order_id") or target_id}); context.start_step("submit")
    queue, clients = order_queue(context.session, vault.decrypt(context.job.config_snapshot_json))
    try:
        asyncio.run(queue.process_batch(target_id) if target_type == "group" else queue.process(target_id))
    except Exception as error:
        unknown = bool(getattr(error, "outcome_unknown", False) or (getattr(error, "details", None) or {}).get("outcome_unknown"))
        message = str(error)[:1000] or error.__class__.__name__
        if target_type == "group":
            orders.update_batch_group(target_id, submission_status="unknown" if unknown else "failed", submission_error=message)
        else:
            orders.update_submission(target_id, submission_status="unknown" if unknown else "failed", submission_error=message)
        raise
    finally:
        for client in clients: client.close()
    refreshed = orders.get_batch_group(target_id) if target_type == "group" else orders.get_order(target_id)
    if refreshed["submission_status"] == "unknown": raise AppError("供应商创建结果未知，禁止自动重发；请先同步原外部订单号", code="purchase_outcome_unknown")
    if refreshed["submission_status"] != "submitted": raise AppError(refreshed.get("submission_error") or "供应商采购未成功", code="purchase_failed")
    try:
        gallery_codes = [str(value) for value in json.loads(str(refreshed.get("fulfillment_gallery_codes") or "[]")) if str(value).strip()]
    except (TypeError, ValueError, json.JSONDecodeError):
        gallery_codes = []
    if gallery_codes:
        mark_ordered_gallery_codes(
            context.session, supplier=str(refreshed.get("supplier") or "hicustom").casefold(),
            gallery_codes=gallery_codes, sku=str(refreshed.get("sku") or ""),
            product_name=str(refreshed.get("product_name") or ""),
            order_id=str(refreshed.get("hicustom_order_id") or refreshed.get("out_order_id") or target_id),
        )
    context.complete_step("submit", details={"external_order_id": refreshed.get("hicustom_order_id")})


@job_handler("pod_purchase_sync")
def purchase_sync_job(context: JobContext) -> None:
    context.start_step("sync"); queue, clients = order_queue(context.session, vault.decrypt(context.job.config_snapshot_json))
    try: result = asyncio.run(queue.sync_remote_orders(supplier=context.payload.get("supplier", "")))
    finally:
        for client in clients: client.close()
    context.complete_step("sync", details=result)


def tms_preview(session: Session, order_key: str, settings: dict | None = None):
    settings = settings or procurement_settings(session); warehouse = str(settings.get("tms_warehouse_id") or "")
    if not warehouse: raise AppError("TMS 仓库编号未配置", code="tms_warehouse_missing")
    orders = manager(session); local = orders.get_order(order_key); client = TmsClient(settings)
    async def prepare():
        try:
            remote = await client.find_wait_submit_order(str(local["posting_number"]))
            if remote is None: raise OrderError("TMS 待提交列表中没有找到这笔平台订单")
            payload, package_ids, item_ids = orders.tms_packaging_payload(order_key, remote, warehouse)
            return payload, package_ids, item_ids, str(remote.get("platformOrderId") or "")
        finally: await client.aclose()
    return asyncio.run(prepare())


@job_handler("pod_tms_submit")
def tms_submit_job(context: JobContext) -> None:
    context.start_step("preflight"); key = context.payload["order_key"]; snapshot = vault.decrypt(context.job.config_snapshot_json); payload, package_ids, item_ids, platform_id = tms_preview(context.session, key, snapshot)
    orders = manager(context.session); orders.update_tms_status(key, tms_status="submitting", tms_error="", tms_platform_order_id=platform_id, tms_package_ids=json.dumps(package_ids), tms_item_ids=json.dumps(item_ids))
    context.complete_step("preflight", details={"package_count": len(package_ids)}); context.start_step("submit"); client = TmsClient(snapshot)
    async def send():
        try:
            try:
                await client.submit_packaging(payload)
            except Exception:
                # A timeout/error can occur after TMS persisted domestic tracking.
                remote = await client.find_wait_submit_order(str(orders.get_order(key)["posting_number"]))
                expected = {str(head.get("trackingNo") or "") for package in payload["packages"] for item in package["items"] for head in item.get("headPackages") or []}
                actual = {str(head.get("trackingNo") or "") for package in (remote or {}).get("packages") or [] for item in package.get("items") or [] for head in item.get("headPackages") or [] if isinstance(head, dict)}
                if not remote or not expected or not expected <= actual:
                    return "unknown", "TMS 创建结果需后台核对"
                return "info_written", "国内物流已写入，请在 TMS 后台完成最终提交"
            try:
                await client.apply_waybills(package_ids)
                return "completed", ""
            except Exception as error:
                return "submitted", f"TMS 已提交，但平台面单申请失败：{error}"[:1000]
        finally:
            await client.aclose()
    status, error = asyncio.run(send())
    orders.update_tms_status(key, tms_status=status, tms_error=error, tms_submitted_at=utc_now() if status in {"completed", "submitted"} else "")
    if status == "unknown": raise AppError("TMS 提交结果不确定，禁止自动重发，请先在后台核对", code="tms_outcome_unknown")
    context.complete_step("submit", details={"result": status})


def fetch_platform_orders(config: dict, payload: dict) -> list[dict]:
    """Return platform facts without importing orders or opening an ERP DB."""
    async def fetch():
        client = WildberriesOrderClient(config) if config["platform"] == "wb" else OzonClient(config)
        try:
            if payload.get("scope_version") == 1:
                options = {key: payload.get(key) for key in ("date_from", "date_to", "order_numbers")}
                if config["platform"] == "wb":
                    return await client.orders(days=payload["days"], **options)
                return await client.postings(days=payload["days"], status=payload["status"], **options)
            if config["platform"] == "wb":
                return await client.orders(days=min(payload["days"], 30))
            return await client.postings(days=payload["days"], status=payload["status"])
        finally:
            await client.aclose()
    return asyncio.run(fetch())


@job_handler("pod_order_sync")
def sync_job(context: JobContext) -> None:
    sync_step = context.start_step("sync")
    config = vault.decrypt(context.job.config_snapshot_json)

    if sync_step.status != "completed":
        try:
            postings = fetch_platform_orders(config, context.payload)
        except Exception:
            # Client exception payloads may contain platform/recipient data; do not copy to job logs.
            raise AppError("订单同步失败，请检查店铺连接和网络后重试", code="pod_order_sync_failed") from None
        context.refresh()
        result = manager(context.session).import_postings(postings, platform=config["platform"],
            store_id=config["store_id"], store_name=config["store_name"], wb_rub_to_cny_rate=config["wb_rate"])
        context.complete_step("sync", details=result)
    if context.start_step("images").status != "completed":
        context.complete_step("images", details=sync_platform_images(context.session, config))


@job_handler("pod_order_image_sync")
def image_sync_job(context: JobContext) -> None:
    if context.start_step("images").status != "completed":
        context.complete_step("images", details=sync_platform_images(context.session, vault.decrypt(context.job.config_snapshot_json)))
