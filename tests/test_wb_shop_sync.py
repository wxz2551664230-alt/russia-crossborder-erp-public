import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from test_personal_job_controls import team
from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps, loads
from erp.core.security import vault
from erp.domains.pod import wb_shop_sync, wb_export
from erp.models import CatalogProduct, CatalogVariant, Job, Store, WbPublicationItem


@pytest.fixture
def shop(team):
    clients, worker = team
    client = clients['owner']
    r = client.post('/api/v1/execution-devices/local/register', json={'id': worker, 'name': 'original PC', 'wb_history_version': 3})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        store = Store(platform='wb', name='WB', offer_prefix='W-', secrets_encrypted=vault.encrypt({'token': 'test-token'}))
        other = Store(platform='wb', name='Other', secrets_encrypted=vault.encrypt({'token': 'other-token'}))
        db.add_all([store, other]); db.flush()
        variants = []
        for n in range(1, 7):
            product = CatalogProduct(sku=f'HC-{n:06}', source_type='pod', name=f'测试{n}')
            db.add(product); db.flush()
            variant = CatalogVariant(product_id=product.id, sku=product.sku+'-01', size='100x100', active=True)
            db.add(variant); db.flush(); variants.append((variant.id, product.id))
        db.commit()
        return clients, worker, store.id, other.id, variants


def card(n, stock=100, price=100, discount=30):
    return {'vendorCode': f'W-HC-{n:06}-01', 'nmID': n, 'sizes': [{'chrtID': n+100, 'techSize': '0'}],
            'stock': {'state': 'verified' if stock is not None else 'unknown', 'amount': stock},
            'price': {'state': 'verified' if price is not None else 'missing', 'price': price,
                      'discount': discount if price is not None else None, 'currency': 'CNY' if price else ''}}


def begin(shop):
    clients, worker, store, _, _ = shop
    client = clients['owner']
    r = client.post(f'/api/v1/pod/wb/stores/{store}/sync')
    assert r.status_code == 202, r.text
    job = r.json()['job_id']
    base = f'/api/v1/execution-devices/local/{worker}'
    claimed = client.post(base+'/claim')
    assert claimed.status_code == 200 and claimed.json()['id'] == job, claimed.text
    path = base+'/jobs/'+job
    headers = {'X-Worker-Attempt': str(claimed.json()['attempts'])}
    snap = client.post(path+'/wb-history-snapshot', headers=headers)
    assert snap.status_code == 200 and snap.json()['shop_sync'] == 1, snap.text
    return client, job, path, headers


def finish(info, cards, warnings=None):
    client, job, path, headers = info
    r = client.post(path+'/wb-history-part', headers=headers, json={'index': 0, 'cards': cards})
    assert r.status_code == 200, r.text
    receipt = {'chunks': 1, 'cards': len(cards), 'sha256': hashlib.sha256(dumps(cards).encode()).hexdigest(),
               'shop_sync': {'warnings': warnings or [], 'retry_seconds': 0, 'cards_read': len(cards)}}
    r = client.post(path+'/complete', headers=headers, json={'result': receipt})
    assert r.status_code == 200, r.text
    assert r.json()['status'] == 'completed', r.text
    return r


def test_shop_sync_matches_all_stages_blocks_duplicates_and_is_store_scoped(shop):
    clients, worker, store, other, variants = shop
    info=begin(shop)
    finish(info, [card(1, 0), card(2, price=None), card(3), card(4, None)])
    downloaded=clients['owner'].get(f'/api/v1/pod/wb/history/{info[1]}/report')
    assert downloaded.status_code==200 and downloaded.json()['summary']['matched_variants']==4,downloaded.text
    assert any(v['stock']['state']=='unknown' and v['sku']=='HC-000004-01' for v in downloaded.json()['matched'])
    with SessionLocal() as db:
        rows = {r.variant_id: r for r in db.scalars(select(WbPublicationItem))}
        assert [rows[variants[n][0]].status for n in range(4)] == ['pending_stock', 'pending_price', 'completed', 'sync_unknown']
        assert all(rows[variants[n][0]].external_product_id for n in range(4))
        available = wb_export.publication_availability(db, store_id=store, product_ids=[p for _, p in variants])['products']
        assert all(available[variants[n][1]]['blocked'] for n in range(4))
        assert not available[variants[4][1]]['blocked']
        assert all(not v['blocked'] for v in wb_export.publication_availability(db, store_id=other, product_ids=[p for _, p in variants])['products'].values())
    status = clients['owner'].get(f'/api/v1/pod/wb/stores/{store}/sync-status').json()
    assert status['summary']['available_variants'] == 2
    assert status['summary']['pending_stock'] == status['summary']['pending_price'] == status['summary']['completed'] == 1
    assert clients['owner'].post(f'/api/v1/pod/wb/stores/{store}/sync').json()['cached']
    assert clients['owner'].get('/api/v1/pod/library/products', params={'wb_store_id': store, 'wb_state': 'available'}).json()['total'] == 2
    assert clients['owner'].get('/api/v1/pod/wb/publications', params={'stage': 'pending_price'}).json()['items'][0]['price'] is None


def test_429_cooldown_is_shared_and_force_does_not_bypass_it(shop):
    info = begin(shop); client, job, path, headers = info
    assert client.post(f'/api/v1/pod/wb/stores/{shop[2]}/sync').json()['job_id'] == job
    r = client.post(path+'/wb-shop-sync-throttle', headers=headers, json={'retry_seconds': 180})
    assert r.status_code == 200, r.text
    assert client.post(path+'/fail', headers=headers, json={'error_code': 'wb_shop_sync_rate_limited', 'error_message': 'WB 429'}).status_code == 200
    r = client.post(f'/api/v1/pod/wb/stores/{shop[2]}/sync?refresh=true')
    assert r.status_code == 409 and '冷却' in r.text, r.text
    assert shop[0]['viewer'].post(f'/api/v1/pod/wb/stores/{shop[2]}/sync').status_code == 403


def test_wrong_pc_old_receipt_and_credential_rotation_are_rejected(shop):
    client, job, path, headers = begin(shop)
    assert shop[0]['other'].post(path+'/wb-history-snapshot', headers=headers).status_code == 403
    old = {'chunks': 0, 'cards': 0, 'sha256': hashlib.sha256(b'').hexdigest()}
    assert client.post(path+'/complete', headers=headers, json={'result': old}).status_code == 409
    with SessionLocal() as db:
        db.get(Store, shop[2]).secrets_encrypted = vault.encrypt({'token': 'rotated'}); db.commit()
    old['shop_sync'] = {'warnings': [], 'retry_seconds': 0, 'cards_read': 0}
    assert client.post(path+'/complete', headers=headers, json={'result': old}).status_code == 409
    with SessionLocal() as db:
        assert list(db.scalars(select(WbPublicationItem))) == []


def test_conflicting_and_multisize_cards_are_reserved_without_guessing(shop):
    cards = [card(1), {**card(1), 'vendorCode': 'OLD-HC-000001-01', 'nmID': 19}, card(2)]
    cards[2]['sizes'].append({'chrtID': 999, 'techSize': 'L'})
    finish(begin(shop), cards)
    with SessionLocal() as db:
        rows = list(db.scalars(select(WbPublicationItem)))
        assert len(rows) == 2 and all(r.status == 'sync_conflict' for r in rows)
        assert wb_export.publication_availability(db, store_id=shop[2], product_ids=[p for _, p in shop[4]])['products'][shop[4][0][1]]['blocked']


def test_preserves_live_writer_and_completed_stock_when_sold_out(shop):
    info = begin(shop)
    with SessionLocal() as db:
        active = Job(type='pod_wb_stock', status='running', payload_json='{}', idempotency_key='active-stock')
        old = Job(type='pod_wb_price', status='completed', payload_json='{}', idempotency_key='complete-price')
        db.add_all([active, old]); db.flush()
        for n, status, job in [(1, 'stock_processing', active), (2, 'completed', old)]:
            v, p = shop[4][n-1]
            db.add(WbPublicationItem(store_id=shop[2], product_id=p, variant_id=v, offer_id=card(n)['vendorCode'],
                job_id=job.id, status=status, mode='history', external_product_id=str(n)))
        db.commit()
    finish(info, [card(1, 0), card(2, 0)])
    with SessionLocal() as db:
        rows = {r.variant_id: r.status for r in db.scalars(select(WbPublicationItem))}
        assert rows[shop[4][0][0]] == 'stock_processing'
        assert rows[shop[4][1][0]] == 'completed'


@pytest.mark.parametrize('state', ['stock_failed', 'price_failed', 'failed'])
def test_existing_platform_id_never_becomes_available_or_retryable(shop, state):
    with SessionLocal() as db:
        v, p = shop[4][0]
        db.add(WbPublicationItem(store_id=shop[2], product_id=p, variant_id=v, offer_id='W-HC-000001-01',
            status=state, external_product_id='1')); db.commit()
        assert wb_export.publication_availability(db, store_id=shop[2], product_ids=[p])['products'][p]['blocked']
    c = shop[0]['owner']
    for name in ['available', 'retryable']:
        r = c.get('/api/v1/pod/library/products', params={'wb_store_id': shop[2], 'wb_state': name})
        assert r.status_code == 200 and p not in {v['id'] for v in r.json()['items']}, r.text


def test_full_sync_never_executes_on_server():
    with pytest.raises(Exception, match='仅允许原发起电脑'):
        wb_export.wb_history_reconcile_job(SimpleNamespace(payload={'shop_sync': 1}))


def test_older_client_is_stopped_before_a_job_is_created(shop):
    client, worker = shop[0]['owner'], shop[1]
    client.post('/api/v1/execution-devices/local/register', json={'id': worker, 'name': 'old PC', 'wb_history_version': 1})
    response = client.post(f'/api/v1/pod/wb/stores/{shop[2]}/sync')
    assert response.status_code == 409 and '0.6.90' in response.text
    with SessionLocal() as db: assert db.scalar(select(Job.id)) is None


def test_positive_inventory_observed_by_sync_survives_later_sellout(shop):
    info = begin(shop); finish(info, [card(1)])
    with SessionLocal() as db:
        wb_shop_sync.apply(db, db.get(Job, info[1]), [card(1, 0)], cards_read=1)
        db.commit()
        assert db.scalar(select(WbPublicationItem)).status == 'completed'


def test_actual_discount_must_match_plan_and_unknown_prices_are_not_complete(shop):
    info = begin(shop)
    with SessionLocal() as db:
        original = Job(type='pod_wb_excel_export', status='completed', payload_json=dumps({'pricing': {'discount_percent': 30}}), idempotency_key='plan')
        db.add(original); db.flush()
        v, p = shop[4][0]
        db.add(WbPublicationItem(store_id=shop[2], product_id=p, variant_id=v, job_id=original.id, offer_id=card(1)['vendorCode'], status='pending_stock'))
        db.commit()
    unknown = card(2); unknown['price'] = {'state': 'unknown', 'price': None, 'discount': None, 'currency': ''}
    finish(info, [card(1, discount=0), unknown])
    with SessionLocal() as db:
        rows = {r.variant_id:r for r in db.scalars(select(WbPublicationItem))}
        assert rows[shop[4][0][0]].status == 'pending_price'
        assert rows[shop[4][1][0]].status == 'sync_unknown'
