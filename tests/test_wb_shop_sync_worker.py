import copy
import importlib
import threading
from pathlib import Path

import pytest
import requests


class Signal:
    def is_set(self): return False
    def wait(self, seconds): return False


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    return importlib.import_module('wb_shop_sync_worker')


def cards(n):
    return [{'vendorCode': f'W-HC-{i:06}-01', 'nmID': i, 'sizes': [{'chrtID': i+10000, 'techSize': '0'}]} for i in range(1, n+1)]


def snapshot(n):
    return {'shop_sync': 1, 'store_id': 'store', 'config': {'store': {'secrets': {'token': 'secret'}}},
            'candidates': [{'sku': f'HC-{i:06}-01', 'product_sku': f'HC-{i:06}'} for i in range(1, n+1)], 'known': []}


class Cloud:
    def __init__(self): self.calls = []
    def request(self, method, path, body, extra_headers):
        assert method == 'POST'
        self.calls.append((path, copy.deepcopy(body)))
        return {}, {}


class Response:
    def __init__(self, value, status=200, headers=None): self.value, self.status_code, self.headers = value, status, headers or {}
    def json(self): return copy.deepcopy(self.value)
    def raise_for_status(self):
        if self.status_code >= 400: raise requests.HTTPError('test read failed')


def platform(monkeypatch, worker, n, *, fail_stock=0, omit_stock=False, fail_cards=0):
    calls = []; all_cards = cards(n)
    # A non-POD card must not be sent to inventory or pricing endpoints.
    all_cards.append({'vendorCode': 'UNRELATED', 'nmID': 999999, 'sizes': [{'chrtID': 888888, 'techSize': '0'}]})
    class HTTP:
        def __init__(self): self.headers = {}
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def request(self, method, url, json=None, timeout=60):
            calls.append((method, url, copy.deepcopy(json)))
            if url.endswith('/cards/list'):
                assert method == 'POST'
                if fail_cards: return Response({}, fail_cards)
                offset = int(json['settings']['cursor'].get('nmID') or 0)
                page = all_cards[offset:offset+100]
                return Response({'cards': page, 'cursor': {'updatedAt': '2026-10-17', 'nmID': offset+len(page)}})
            if url.endswith('/warehouses'):
                assert method == 'GET'
                return Response([{'id': 1}, {'id': 1}, {'id': 2}])
            if '/stocks/' in url:
                assert method == 'POST' and set(json) == {'chrtIds'} and len(json['chrtIds']) <= 1000
                assert 888888 not in json['chrtIds']
                if fail_stock: return Response({}, fail_stock, {'Retry-After': '120'})
                ids = json['chrtIds'][:-1] if omit_stock else json['chrtIds']
                return Response({'stocks': [{'chrtId': i, 'amount': 50} for i in ids]})
            if url.endswith('/goods/filter'):
                assert method == 'POST' and set(json) == {'nmList'} and len(json['nmList']) <= 1000
                assert 999999 not in json['nmList']
                return Response({'data': {'listGoods': [{'nmID': i, 'discount': 30, 'currencyIso4217': 'CNY', 'sizes': [{'price': 100}]} for i in json['nmList']]}})
            raise AssertionError('Unexpected or write API: '+url)
    monkeypatch.setattr(worker.requests, 'Session', HTTP)
    return calls


def observations(cloud):
    return [card for path, body in cloud.calls if path.endswith('/wb-history-part') for card in body['cards']]


def test_batch_1001_products_two_warehouses_and_receipt_resume_without_platform_calls(worker, monkeypatch, tmp_path):
    calls = platform(monkeypatch, worker, 1001); cloud = Cloud(); snap = snapshot(1001)
    job = {'id': 'job', 'attempts': 1}
    result = worker.run(cloud, '/jobs/job', job, tmp_path, Signal(), snap)
    assert result['cards'] == 1001
    # 11 cursor pages + 1 warehouse list + 4 stock batches + 2 price batches.
    assert len(calls) == 18
    assert all(c['stock']['amount'] == 100 and c['price']['discount'] == 30 for c in observations(cloud))
    calls.clear(); cloud.calls.clear()
    assert worker.run(cloud, '/jobs/job', job, tmp_path, Signal(), snap) == result
    assert calls == [] and len(observations(cloud)) == 1001
    # A distinct manual sync within five minutes also reuses the account cache.
    worker.run(Cloud(), '/jobs/new', {'id': 'new', 'attempts': 1}, tmp_path, Signal(), snap)
    assert calls == []


def test_force_refresh_bypasses_shared_cache_but_never_platform_cooldown(worker, monkeypatch, tmp_path):
    calls = platform(monkeypatch, worker, 2); snap = snapshot(2)
    worker.run(Cloud(), '/a', {'id': 'a', 'attempts': 1}, tmp_path, Signal(), snap)
    calls.clear(); snap['refresh'] = True
    worker.run(Cloud(), '/b', {'id': 'b', 'attempts': 1}, tmp_path, Signal(), snap)
    assert len(calls) == 5
    calls.clear(); platform(monkeypatch, worker, 2, fail_stock=429); cloud = Cloud()
    result = worker.run(cloud, '/c', {'id': 'c', 'attempts': 1}, tmp_path, Signal(), snap)
    assert result['shop_sync']['retry_seconds'] == 120
    assert all(c['stock']['state'] == c['price']['state'] == 'unknown' for c in observations(cloud))
    assert len([p for p, _ in cloud.calls if p.endswith('/wb-shop-sync-throttle')]) == 1
    calls2 = platform(monkeypatch, worker, 2)
    with pytest.raises(worker.SyncRateLimited):
        worker.run(Cloud(), '/d', {'id': 'd', 'attempts': 1}, tmp_path, Signal(), snap)
    assert calls2 == []


@pytest.mark.parametrize('fail_stock,omit_stock', [(403, False), (0, True)])
def test_successful_sparse_inventory_is_zero_but_failed_reads_remain_unknown(worker, monkeypatch, tmp_path, fail_stock, omit_stock):
    calls = platform(monkeypatch, worker, 2, fail_stock=fail_stock, omit_stock=omit_stock); cloud = Cloud()
    worker.run(cloud, '/a', {'id': 'a', 'attempts': 1}, tmp_path, Signal(), snapshot(2))
    rows = observations(cloud)
    assert rows[-1]['stock'] == ({'state': 'unknown', 'amount': None} if fail_stock else {'state': 'verified', 'amount': 0})
    assert rows[-1]['price']['state'] == 'verified'
    if fail_stock: assert len([c for c in calls if '/stocks/' in c[1]]) == 1


def test_card_scan_failure_never_updates_identity_or_retries(worker, monkeypatch, tmp_path):
    calls = platform(monkeypatch, worker, 2, fail_cards=429); cloud = Cloud()
    with pytest.raises(worker.SyncRateLimited):
        worker.run(cloud, '/a', {'id': 'a', 'attempts': 1}, tmp_path, Signal(), snapshot(2))
    assert len(calls) == 1 and observations(cloud) == []
    assert len(cloud.calls) == 1 and cloud.calls[0][0].endswith('/wb-shop-sync-throttle')


def test_reader_stops_when_lease_is_lost_and_paces_uncached_reads(worker, tmp_path):
    waits = []
    class Tracked(Signal):
        def wait(self, seconds): waits.append(seconds); return False
    class HTTP:
        def request(self, *args, **kwargs): return Response({})
    reader = worker.Reader(HTTP(), tmp_path, Tracked(), tmp_path/'cooldown.json')
    reader.last = worker.time.monotonic()
    reader.read('GET', 'https://example.invalid/1')
    assert len(waits) == 1 and 0 < waits[0] <= 1
    lost = threading.Event(); lost.set(); reader.lost = lost
    with pytest.raises(ValueError, match='连接中断'): reader.read('GET', 'https://example.invalid/1')


def test_base_token_continues_only_unread_price_batches_without_a_429(worker, monkeypatch, tmp_path):
    import base64, json, os, time
    calls = platform(monkeypatch, worker, 1001); cloud = Cloud(); snap = snapshot(1001)
    raw = base64.urlsafe_b64encode(json.dumps({'acc':1,'t':False}).encode()).decode().rstrip('=')
    snap['config']['store']['secrets']['token'] = 'header.'+raw+'.signature'
    result = worker.run(cloud, '/a', {'id':'a','attempts':1}, tmp_path, Signal(), snap)
    assert result['shop_sync']['retry_seconds'] >= 900
    assert len([v for v in calls if v[1].endswith('/goods/filter')]) == 1
    assert sum(r['price']['state']=='verified' for r in observations(cloud)) == 1000
    # Simulate a human continuing after the quota refill; all old read files are
    # older than the ordinary five-minute cache, but belong to this unfinished cycle.
    for file in tmp_path.rglob('*.json'):
        if file.name in {'cooldown.json','quota-prices.json'}:
            file.write_text('{"until":0}',encoding='utf-8')
        else: os.utime(file,(time.time()-1800,time.time()-1800))
    calls.clear(); cloud.calls.clear()
    result = worker.run(cloud, '/b', {'id':'b','attempts':1}, tmp_path, Signal(), snap)
    assert len(calls)==1 and calls[0][1].endswith('/goods/filter')
    assert result['shop_sync']['retry_seconds']==0
    assert all(r['price']['state']=='verified' for r in observations(cloud))


def test_successful_sparse_second_warehouse_keeps_first_warehouse_quantity(worker):
    class Reader:
        def read(self, method, url, body=None):
            if url.endswith('/warehouses'):return [{'id':1},{'id':2}]
            if url.endswith('/1'):return {'stocks':[{'chrtId':10001,'amount':50}]}
            return {'stocks':[]}
    observed=worker._stocks(Reader(),cards(1))
    assert observed[10001]=={'state':'verified','amount':50}


def test_stock_failure_after_first_warehouse_cannot_turn_missing_records_into_zero(worker):
    class Reader:
        def read(self, method, url, body=None):
            if url.endswith('/warehouses'):return [{'id':1},{'id':2}]
            if url.endswith('/1'):return {'stocks':[{'chrtId':10001,'amount':50}]}
            raise requests.HTTPError('Second warehouse failed')
    observed={}
    with pytest.raises(requests.HTTPError):worker._stocks(Reader(),cards(2),observed)
    assert observed[10001]=={'state':'present','amount':50}
    assert observed[10002]=={'state':'unknown','amount':None}


def test_successful_all_empty_warehouses_confirm_zero_without_individual_queries(worker):
    requests_seen=[]
    class Reader:
        def read(self, method, url, body=None):
            requests_seen.append(url)
            if url.endswith('/warehouses'):return [{'id':1},{'id':2}]
            return {'stocks':[]}
    observed=worker._stocks(Reader(),cards(3))
    assert all(v=={'state':'verified','amount':0} for v in observed.values())
    assert len(requests_seen)==3


@pytest.mark.parametrize('body',[{'stocks':None},{'stocks':[{'chrtId':10001,'amount':-1}]}])
def test_http_success_with_invalid_inventory_body_cannot_confirm_zero(worker,body):
    class Reader:
        def read(self,method,url,data=None):
            return [{'id':1}] if url.endswith('/warehouses') else body
    observed={}
    with pytest.raises(ValueError):worker._stocks(Reader(),cards(2),observed)
    assert observed=={}
