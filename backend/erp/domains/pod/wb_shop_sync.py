"""Shop reconciliation: the originating PC reads WB; the server matches receipts."""
import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps, loads
from erp.core.security import vault
from erp.core.task_identity import task_identity
from erp.models import (Asset, CatalogProduct, CatalogVariant, Job, JobArtifact,
                        Store, SystemSetting, WbPublicationItem, WorkerNode)
from erp.services.jobs import enqueue_job

TERMINAL = {'completed', 'failed', 'cancelled'}


def key(store_id):
    return 'wb_shop_sync:' + store_id


def fingerprint(config):
    return hashlib.sha256(dumps(config).encode()).hexdigest()


def status(session, store_id):
    from erp.domains.pod.wb_export import _wb_store_snapshot
    store, config = _wb_store_snapshot(session, store_id)
    saved = session.get(SystemSetting, key(store_id))
    value = loads(saved.value_json, {}) if saved else {}
    if value.get('fingerprint') != fingerprint(vault.decrypt(config)):
        value = {}
    active = next((job for job in session.scalars(select(Job).where(
        Job.type == 'pod_wb_history_reconcile', Job.status.not_in(TERMINAL),
        Job.payload_json.contains(store_id)).order_by(Job.created_at.desc()))
        if loads(job.payload_json, {}).get('store_id') == store_id), None)
    return {**{k: v for k, v in value.items() if k != 'fingerprint'},
            'store_id': store.id, 'active_job_id': active.id if active else '',
            'active_status': active.status if active else ''}


def enqueue(session, store_id, refresh=False):
    from erp.domains.pod.wb_export import _wb_store_snapshot
    actor = task_identity.get() or {}
    device = session.get(WorkerNode, actor.get('worker_id', ''))
    if not device or device.owner_user_id != actor.get('id') or 'wb_shop_sync_v2' not in loads(device.capabilities_json, []):
        raise ConflictError('店铺完整同步需要在当前电脑安装并启动 0.6.90 或更新的客户端')
    store, config = _wb_store_snapshot(session, store_id)
    session.execute(update(Store).where(Store.id == store.id).values(enabled=Store.enabled))
    value = status(session, store_id)
    if value['active_job_id']:
        job = session.get(Job, value['active_job_id'])
        if job.created_by == actor.get('id') and job.target_worker_id == actor.get('worker_id') and loads(job.payload_json, {}).get('shop_sync'):
            session.commit()
            return {'job_id': job.id, 'cached': False, 'reused': True}
        raise ConflictError('该 WB 店铺已有同步任务，请等待原电脑完成；不会重复请求平台')
    now = datetime.now(timezone.utc).timestamp()
    if value.get('cooldown_until', 0) > now:
        raise ConflictError(f"WB 同步限流冷却中，请 {int(value['cooldown_until']-now)+1} 秒后人工同步")
    if not refresh and value.get('synced_epoch', 0) > now - 300 and not value.get('warnings'):
        session.commit()
        return {'job_id': value['job_id'], 'cached': True, 'summary': value['summary']}
    job = enqueue_job(session, job_type='pod_wb_history_reconcile',
        payload={'store_id': store.id, 'store_name': store.name, 'offer_prefix': store.offer_prefix,
                 'shop_sync': 1, 'stock_workflow': True, 'refresh': bool(refresh)},
        config_snapshot=config, idempotency_key='wb-shop-sync:' + uuid.uuid4().hex,
        max_attempts=1, steps=[('download', '批量读取店铺商品、库存及价格'),
                              ('match', '匹配本店铺 POD 规格'), ('ledger', '更新上架阶段与防重复状态')], commit=False)
    session.commit()
    return {'job_id': job.id, 'cached': False, 'reused': False}


def snapshot(session, job, token):
    payload = loads(job.payload_json, {})
    candidates = session.execute(select(CatalogVariant.id, CatalogVariant.sku, CatalogVariant.size,
        CatalogProduct.sku.label('product_sku')).join(CatalogProduct).where(
            CatalogProduct.source_type == 'pod', CatalogVariant.active.is_(True))).mappings().all()
    known = session.scalars(select(WbPublicationItem).where(WbPublicationItem.store_id == payload['store_id'])).all()
    return {'store_id': payload['store_id'], 'shop_sync': 1, 'refresh': payload.get('refresh', False),
            'config': {'store': {'secrets': {'token': token}}},
            'candidates': [dict(row) for row in candidates],
            'known': [{'offer_id': row.offer_id, 'variant_id': row.variant_id,
                       'nm_id': row.external_product_id} for row in known]}


def throttle(session,job,seconds):
    payload=loads(job.payload_json,{})
    if job.type!='pod_wb_history_reconcile' or payload.get('shop_sync')!=1 or type(seconds)is not int or not 1<=seconds<=86400:
        raise ConflictError('WB 同步冷却回执无效')
    session.execute(update(Store).where(Store.id==payload['store_id']).values(enabled=Store.enabled))
    saved=session.get(SystemSetting,key(payload['store_id']))
    value=loads(saved.value_json,{}) if saved else {}
    value.update(fingerprint=fingerprint(vault.decrypt(job.config_snapshot_json)),
                 cooldown_until=datetime.now(timezone.utc).timestamp()+seconds,
                 warnings=['WB 请求额度需要冷却，本轮停止请求；冷却结束后请人工重新同步'])
    if not saved:saved=SystemSetting(key=key(payload['store_id']));session.add(saved)
    saved.value_json=dumps(value)
    session.commit()
    return {'retry_seconds':seconds}


def validate_cards(cards):
    """Read-only receipts never accept arbitrary fields, negative stock or prices."""
    if not isinstance(cards, list):
        raise ConflictError('WB 店铺同步结果不是商品列表')
    for card in cards:
        if not isinstance(card, dict) or set(card) != {'vendorCode', 'nmID', 'sizes', 'stock', 'price'}:
            raise ConflictError('WB 店铺同步缺少商品、库存或价格核对结果')
        if not isinstance(card['vendorCode'], str) or not 0 < len(card['vendorCode']) <= 200 or type(card['nmID']) is not int or card['nmID'] <= 0:
            raise ConflictError('WB 商品身份无效')
        if not isinstance(card['sizes'], list) or len(card['sizes']) > 200:
            raise ConflictError('WB 商品规格无效')
        for size in card['sizes']:
            if not isinstance(size, dict) or set(size) != {'chrtID', 'techSize'} or type(size['chrtID']) is not int or size['chrtID'] <= 0 or not isinstance(size['techSize'], str) or len(size['techSize']) > 100:
                raise ConflictError('WB 商品规格身份无效')
        stock, price = card['stock'], card['price']
        if not isinstance(stock, dict) or set(stock) != {'state', 'amount'} or stock['state'] not in {'verified', 'unknown', 'present'} or (stock['amount'] is not None and (type(stock['amount']) is not int or stock['amount'] < 0)) or (stock['state'] != 'unknown' and stock['amount'] is None) or (stock['state'] == 'present' and stock['amount'] <= 0):
            raise ConflictError('WB 库存核对结果无效')
        if not isinstance(price, dict) or set(price) != {'state', 'price', 'discount', 'currency'} or price['state'] not in {'verified', 'missing', 'unknown'}:
            raise ConflictError('WB 价格核对结果无效')
        if price['state'] == 'verified':
            if type(price['price']) not in (int, float) or not 0 < price['price'] < 1e10 or type(price['discount']) is not int or not 0 <= price['discount'] <= 99 or not isinstance(price['currency'], str) or len(price['currency']) > 10:
                raise ConflictError('WB 实际价格或折扣无效')
        elif price['price'] is not None or price['discount'] is not None or price['currency']!='':
            raise ConflictError('WB 未确认价格不能携带价格值')


def _identity(session, store_id, cards):
    products = session.scalars(select(CatalogProduct).where(CatalogProduct.source_type == 'pod').options(selectinload(CatalogProduct.variants))).all()
    variants = {v.id: v for p in products for v in p.variants if v.active}
    skus = {v.sku.casefold(): [v] for v in variants.values()}
    for product in products:
        skus.setdefault(product.sku.casefold(), [v for v in product.variants if v.active])
    ledgers = list(session.scalars(select(WbPublicationItem).where(WbPublicationItem.store_id == store_id)))
    by_offer = {row.offer_id.casefold(): variants[row.variant_id] for row in ledgers if row.variant_id in variants}
    by_nm = {}
    for row in ledgers:
        if row.external_product_id and row.variant_id in variants:
            by_nm.setdefault(row.external_product_id, []).append(variants[row.variant_id])
    proposals, conflicts, unmatched = {}, [], []
    from erp.domains.pod.wb_export import _contained_longest
    for card in cards:
        vendor = card['vendorCode'].casefold()
        chosen = list({v.id: v for v in ([by_offer[vendor]] if vendor in by_offer else []) + by_nm.get(str(card['nmID']), [])}.values())
        if not chosen:
            tokens = re.findall(r'(?:hc|s2b|erp)-\d+(?:-\d+)?', vendor)
            chosen = list({v.id: v for token in tokens for v in skus.get(token, [])}.values())
        if not chosen:
            chosen = _contained_longest(vendor, list(variants.values()), 'sku')
            if not chosen:
                matched_products = _contained_longest(vendor, products, 'sku')
                chosen = [v for p in matched_products for v in p.variants if v.active]
        if len(chosen) != 1 or len(card['sizes']) != 1:
            if chosen:
                conflicts.append({'vendor_code': card['vendorCode'], 'nm_id': card['nmID'],
                                  'variant_ids': [v.id for v in chosen], 'reason': '规格不能唯一对应，需人工核验'})
            else:
                unmatched.append({'vendor_code': card['vendorCode'], 'nm_id': card['nmID']})
            continue
        proposals.setdefault(chosen[0].id, []).append(card)
    matched = {}
    conflicted = {v for entry in conflicts for v in entry.get('variant_ids', [])}
    for variant_id, values in proposals.items():
        if variant_id in conflicted or len({dumps(v) for v in values}) != 1:
            conflicts.append({'variant_ids': [variant_id], 'reason': '同一 ERP 规格匹配多张 WB 商品卡'})
        else:
            matched[variant_id] = values[0]
    return variants, {row.variant_id: row for row in ledgers}, matched, conflicts, unmatched


def apply(session, job, cards, *, warnings=None, retry_seconds=0,cards_read=0):
    validate_cards(cards)
    payload = loads(job.payload_json, {})
    store_id = payload['store_id']
    from erp.domains.pod.wb_export import _wb_store_snapshot, _roots
    _, current_config = _wb_store_snapshot(session, store_id)
    if fingerprint(vault.decrypt(current_config)) != fingerprint(vault.decrypt(job.config_snapshot_json)):
        raise ConflictError('店铺授权已变化，请重新同步，不接受旧账号结果')
    session.execute(update(Store).where(Store.id == store_id).values(enabled=Store.enabled))
    variants, existing, matched, conflicts, unmatched = _identity(session, store_id, cards)
    from erp.domains.pod.wb_price_state import release_after_refresh, latest_operations
    released, held = release_after_refresh(session, job, existing, matched, warnings=warnings)
    from erp.domains.pod.wb_price_recovery import resolve_from_sync
    resolved_prices = resolve_from_sync(session, job, existing, matched, warnings=warnings)
    latest_prices = latest_operations(session, list(existing.values()))
    phase_requests = {j.id: loads(j.payload_json, {}) for j in latest_prices.values()}
    phase_plans = {v['id']: v for p in phase_requests.values() for v in p.get('price_plan', [])}
    held.difference_update(identity for identity, phase in latest_prices.items() if phase.id in resolved_prices)
    counts = {s: 0 for s in ('pending_stock', 'pending_price', 'completed', 'preserved_active', 'unknown')}
    changed = [];written=0
    for variant_id, card in matched.items():
        row = existing.get(variant_id)
        if row and row.external_product_id and row.external_product_id != str(card['nmID']):
            conflicts.append({'variant_ids': [variant_id], 'reason': '原台账与 WB 商品编号冲突'})
            continue
        if not row:
            row = WbPublicationItem(store_id=store_id, product_id=variants[variant_id].product_id,
                variant_id=variant_id, offer_id=card['vendorCode'], mode='history', status='sync_unknown',
                job_id=job.id, template_id='')
            session.add(row); existing[variant_id] = row
        old_job = session.get(Job, row.job_id) if row.job_id and row.job_id != job.id else None
        # A live writer owns its stage. Recording the platform ID still blocks duplicate creation.
        row.external_product_id = str(card['nmID'])
        row.confirmed_at = datetime.now(timezone.utc)
        written+=1
        if row.id in held or old_job and old_job.status not in TERMINAL:
            counts['preserved_active'] += 1
            continue
        old = row.status
        previous_offer = row.offer_id
        # A terminal publish task must no longer be retryable against a recovered card.
        # Its snapshot checks ownership of all original ledger rows before any WB write.
        if old_job and old_job.status in {'failed', 'cancelled'} and old_job.type in {'pod_wb_api_publish', 'pod_wb_excel_export', 'pod_wb_excel_reconcile'}:
            row.job_id = job.id
        row.offer_id = card['vendorCode']
        confirmed_stock = old in {'pending_price', 'price_processing', 'price_failed'} or (
            old == 'completed' and (row.mode != 'history' or old_job and old_job.type != 'pod_wb_history_reconcile'))
        stock, price = card['stock'], card['price']
        if stock['state'] == 'unknown':
            counts['unknown'] += 1
            if row.status=='sync_unknown':row.last_error='商品已存在，库存或规格未能读取；请查看同步报告后人工重查'
            changed.append({'variant_id':variant_id,'sku':variants[variant_id].sku,'nm_id':card['nmID'],
                            'previous_offer_id':previous_offer,'offer_id':row.offer_id,'before':old,'after':row.status,
                            'stock':stock,'price':price,'reason':'库存读取未完成，保留原阶段'})
            continue
        stock_ready = stock['amount'] > 0 or confirmed_stock
        if stock['amount'] > 0 and row.mode == 'history':
            row.mode = 'shop_sync'  # A verified stock receipt survives a later sell-out.
        if not stock_ready:
            row.status = 'pending_stock'
        elif price['state'] == 'unknown':
            row.status = old if old in {'completed', 'pending_price', 'price_failed'} and confirmed_stock else 'sync_unknown'
            counts['unknown'] += 1
        else:
            price_ready = price['state'] == 'verified'
            request = loads(old_job.payload_json, {}) if old_job else {}
            planned_discount = (request.get('pricing') or {}).get('discount_percent')
            planned_row = next((v for v in request.get('rows', []) if v.get('id') == row.id), {})
            phase = latest_prices.get(row.id)
            if phase:
                phase_request = phase_requests[phase.id]
                planned_row = phase_plans.get(row.id) or planned_row
                if phase_request.get('price'):
                    planned_row = {'price': math.ceil(float(phase_request['price'])),
                                   'discount': phase_request.get('discount_percent')}
                planned_discount = phase_request.get('discount_percent', planned_discount)
            if planned_row:
                planned_discount = planned_row.get('discount', planned_discount)
                if price_ready and planned_row.get('price') is not None and float(planned_row['price']) != price['price']:
                    price_ready = False
            if price_ready and planned_discount is not None and int(planned_discount) != price['discount']:
                price_ready = False
            row.status = 'completed' if price_ready else 'pending_price'
        row.last_error = ''
        counts[row.status] = counts.get(row.status, 0) + 1
        changed.append({'variant_id': variant_id, 'sku': variants[variant_id].sku, 'nm_id': card['nmID'], 'previous_offer_id': previous_offer, 'offer_id': row.offer_id,
                        'before': old, 'after': row.status, 'stock': stock, 'price': price})
    # Ambiguous identities must not reappear as available products.
    for conflict in conflicts:
        for variant_id in conflict.get('variant_ids', []):
            variant = variants.get(variant_id)
            if not variant:
                continue
            row = existing.get(variant_id)
            if not row:
                row = WbPublicationItem(store_id=store_id, product_id=variant.product_id, variant_id=variant.id,
                    offer_id=f'ERP-SYNC-CONFLICT-{variant.id}', mode='history', status='sync_conflict', job_id=job.id, template_id='')
                session.add(row); existing[variant_id] = row
            original_job=session.get(Job,row.job_id) if row.job_id and row.job_id!=job.id else None
            if not row.external_product_id and (not original_job or original_job.status in TERMINAL):
                row.status = 'sync_conflict'
                row.last_error = conflict['reason']
    session.flush()
    from erp.domains.pod.wb_export import _WB_BLOCKING_STATUSES, _effective_ledger_status
    available = sum(1 for variant_id in variants if variant_id not in existing or (
        not existing[variant_id].external_product_id and _effective_ledger_status(session, existing[variant_id]) not in _WB_BLOCKING_STATUSES))
    observed_ids = {str(card['nmID']) for card in cards}
    not_found = [{'variant_id': row.variant_id, 'nm_id': row.external_product_id} for row in existing.values()
                 if row.external_product_id and row.external_product_id not in observed_ids]
    summary = {'wb_cards': len(cards), 'cards_read':cards_read, 'matched_variants': written, **counts,
               'released_tasks': len(released),
               'resolved_price_tasks': len(resolved_prices),
               'available_variants': available, 'not_found': len(not_found), 'unmatched_cards': len(unmatched), 'ambiguous': len(conflicts)}
    now = datetime.now(timezone.utc)
    report = _roots()[1] / job.id / ('WB店铺完整同步报告-' + uuid.uuid4().hex[:8] + '.json')
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({'store_id': store_id, 'summary': summary, 'warnings': warnings or [],
        'matched': changed, 'ambiguous': conflicts, 'unmatched': unmatched, 'not_found_retained': not_found}, ensure_ascii=False, indent=2), encoding='utf-8')
    asset = Asset(kind='file', name=report.name, source='wb_shop_sync', local_path=str(report),
                  sha256=hashlib.sha256(report.read_bytes()).hexdigest(), mime_type='application/json',
                  size_bytes=report.stat().st_size, read_only=True)
    session.add(asset); session.flush()
    session.add(JobArtifact(job_id=job.id, asset_id=asset.id, role='wb_reconcile_report'))
    saved = session.get(SystemSetting, key(store_id))
    if not saved:
        saved = SystemSetting(key=key(store_id)); session.add(saved)
    saved.value_json = dumps({'fingerprint': fingerprint(vault.decrypt(job.config_snapshot_json)),
        'job_id': job.id, 'synced_at': now.isoformat(), 'synced_epoch': now.timestamp(),
        'summary': summary, 'warnings': warnings or [], 'cooldown_until': now.timestamp() + min(86400, max(0, retry_seconds))})
    return summary
