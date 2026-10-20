import json
import sys
from pathlib import Path
from threading import Event

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'client/desktop'))
import wb_price_worker as price


@pytest.mark.parametrize('fault', ['', 'missing', 'duplicate', 'discount', 'price'])
def test_batch_readback_checks_all_200_goods_in_one_request(fault):
    rows = [{'nm_id': n, 'price': 124, 'discount': 30} for n in range(1, 201)]
    goods = [{'nmID': n, 'discount': 30, 'sizes': [{'price': 124}]} for n in range(200, 0, -1)]
    if fault == 'missing': goods.pop()
    if fault == 'duplicate': goods.append(goods[0])
    if fault == 'discount': goods[0]['discount'] = 29
    if fault == 'price': goods[0]['sizes'][0]['price'] = 123
    class Http:
        calls = 0
        def post(self, url, json, timeout):
            self.calls += 1
            assert url.endswith('/list/goods/filter')
            assert json == {'nmList': list(range(1, 201))}
            return Response(200, {'data': {'listGoods': goods}})
    http = Http()
    assert price._prices_match(http, rows) is (not fault)
    assert http.calls == 1


SNAPSHOT = {'rows': [{'id': 'a' * 32, 'nm_id': 123, 'price': 124, 'discount': 30}],
            'config': {'store': {'secrets': {'token': 'test'}}}}


class Client:
    def request(self, method, path, body, extra_headers):
        assert path.endswith('/wb-price-snapshot')
        return SNAPSHOT, {}


class Response:
    def __init__(self, status, body=None, headers=None, text=''):
        self.status_code, self.body, self.headers, self.text = status, body or {}, headers or {}, text
    def json(self): return self.body
    def raise_for_status(self): assert self.status_code < 400


def test_rate_limit_wait_exits_on_lost_connection_without_resubmitting(tmp_path, monkeypatch):
    calls = []
    class Session:
        def __init__(self): self.headers = {}
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def post(self, url, json, timeout):
            calls.append(url)
            return Response(200, {'data': {'id': 777}})
        def get(self, url, params, timeout):
            return Response(429, headers={'X-RateLimit-Retry': '900'})
    class Connection:
        def is_set(self): return False
        def wait(self, seconds):
            assert seconds == 900
            return True
    monkeypatch.setattr(price.requests, 'Session', Session)
    with pytest.raises(price.PriceOutcomeUnknown, match='停止等待'):
        price.run(Client(), '/job', {'id': 'b'*32, 'attempts': 1}, tmp_path, Connection())
    assert len(calls) == 1
    assert json.loads((tmp_path/'wb-price-tasks'/('b'*32)/'write.json').read_text())['upload_id'] == 777


def test_cloud_upload_receipt_recovers_without_local_file_or_new_upload(tmp_path, monkeypatch):
    digest = price.hashlib.sha256(json.dumps({'rows': SNAPSHOT['rows']}, sort_keys=True).encode()).hexdigest()
    progress = []
    class CloudClient:
        def request(self, method, path, body, extra_headers):
            if path.endswith('/wb-price-snapshot'):
                return {**SNAPSHOT, 'progress_protocol': 1, 'upload_receipt': {
                    'kind': 'wb_price_upload', 'upload_id': 777, 'digest': digest}}, {}
            assert path.endswith('/wb-price-progress') and body['upload_id'] == 777
            progress.append(body)
            return {'wb_price_deferred': bool(body['wait_seconds'])}, {}
    class Session:
        def __init__(self): self.headers = {}
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def get(self, url, params, timeout):
            assert params['uploadID'] == 777
            return Response(429, headers={'X-RateLimit-Retry': '835'})
        def post(self, *args, **kwargs): raise AssertionError('已上传，不得重新上传')
    monkeypatch.setattr(price.requests, 'Session', Session)
    result = price.run(CloudClient(), '/job', {'id': 'b'*32, 'attempts': 2}, tmp_path, Event())
    assert result['wb_price_deferred'] and progress[-1]['wait_seconds'] == 835


def test_price_upload_verified_and_resume_does_not_resubmit(tmp_path, monkeypatch):
    submitted = []
    class Session:
        def __init__(self): self.headers = {}
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def post(self, url, json, timeout):
            if url.endswith('/list/goods/filter'):
                return self.get(url, json, timeout)
            submitted.append(json)
            return Response(200, {'data': {'id': 777}})
        def get(self, url, params, timeout):
            if 'history/tasks' in url:
                return Response(200, {'data': {'status': 3, 'successGoodsNumber': 1}})
            return Response(200, {'data': {'listGoods': [{'nmID': 123, 'discount': 30,
                'sizes': [{'price': 124}]}]}})
    monkeypatch.setattr(price.requests, 'Session', Session)
    job = {'id': 'b' * 32, 'attempts': 1}
    expected = {'verified': True, 'upload_id': 777, 'rows': SNAPSHOT['rows']}
    assert price.run(Client(), '/job', job, tmp_path, Event()) == expected
    assert price.run(Client(), '/job', job, tmp_path, Event()) == expected
    assert len(submitted) == 1
    assert submitted[0] == {'data': [{'nmID': 123, 'price': 124, 'discount': 30}]}
    assert json.loads((tmp_path / 'wb-price-tasks' / job['id'] / 'write.json').read_text())['state'] == 'done'


def test_price_uncertain_submission_never_resends(tmp_path, monkeypatch):
    submitted = []
    class Session:
        def __init__(self): self.headers = {}
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def post(self, *args, **kwargs):
            submitted.append(1)
            raise price.requests.Timeout('unknown')
    monkeypatch.setattr(price.requests, 'Session', Session)
    job = {'id': 'b' * 32, 'attempts': 1}
    with pytest.raises(price.PriceOutcomeUnknown):
        price.run(Client(), '/job', job, tmp_path, Event())
    with pytest.raises(price.PriceOutcomeUnknown):
        price.run(Client(), '/job', job, tmp_path, Event())
    assert submitted == [1]


def test_price_rate_limit_is_definitive_rejection(tmp_path, monkeypatch):
    calls = []
    class Session:
        def __init__(self): self.headers = {}
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def post(self, *args, **kwargs):
            calls.append(1)
            return Response(429, headers={'X-RateLimit-Retry': '779'})
    monkeypatch.setattr(price.requests, 'Session', Session)
    job = {'id': 'b' * 32, 'attempts': 1, 'payload': {'store_id': 'c' * 32}}
    with pytest.raises(price.PriceRateLimited):
        price.run(Client(), '/job', job, tmp_path, Event())
    checkpoint = json.loads((tmp_path / 'wb-price-tasks' / job['id'] / 'write.json').read_text())
    assert checkpoint['state'] == 'rejected_rate_limit' and checkpoint['retry_after'] == '779'
    assert calls == [1]
    with pytest.raises(price.PriceRateLimited):
        price.run(Client(), '/job', {'id': 'd' * 32, 'attempts': 1, 'payload': job['payload']}, tmp_path, Event())
    assert calls == [1]


def test_price_already_set_requires_exact_readback_and_never_resubmits(tmp_path, monkeypatch):
    submitted = []
    class Session:
        def __init__(self): self.headers = {}
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def post(self, *args, **kwargs):
            if args[0].endswith('/list/goods/filter'):
                return self.get(*args, **kwargs)
            submitted.append(1)
            return Response(400, text='Specified prices and discounts are already set')
        def get(self, *args, **kwargs):
            return Response(200, {'data': {'listGoods': [{'nmID': 123, 'discount': 30,
                'sizes': [{'price': 124}]}]}})
    monkeypatch.setattr(price.requests, 'Session', Session)
    job = {'id': 'b' * 32, 'attempts': 1}
    expected = {'verified': True, 'verified_existing': True, 'upload_id': 0, 'rows': SNAPSHOT['rows']}
    assert price.run(Client(), '/job', job, tmp_path, Event()) == expected
    assert price.run(Client(), '/job', job, tmp_path, Event()) == expected
    assert submitted == [1]


def test_price_already_set_mismatch_stays_unknown(tmp_path, monkeypatch):
    class Session:
        def __init__(self): self.headers = {}
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def post(self, *args, **kwargs):
            if args[0].endswith('/list/goods/filter'):
                return self.get(*args, **kwargs)
            return Response(400, text='Specified prices and discounts are already set')
        def get(self, *args, **kwargs):
            return Response(200, {'data': {'listGoods': [{'nmID': 123, 'discount': 0,
                'sizes': [{'price': 124}]}]}})
    monkeypatch.setattr(price.requests, 'Session', Session)
    with pytest.raises(price.PriceOutcomeUnknown, match='不一致'):
        price.run(Client(), '/job', {'id': 'b' * 32, 'attempts': 1}, tmp_path, Event())


def test_price_reconcile_reads_without_post(tmp_path, monkeypatch):
    class ReconcileClient:
        def request(self, method, path, body, extra_headers):
            return {**SNAPSHOT, 'verify_only': True}, {}
    class Session:
        def __init__(self): self.headers = {}
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def post(self, *args, **kwargs):
            if args[0].endswith('/list/goods/filter'):
                return self.get(*args, **kwargs)
            raise AssertionError('只读核对不能提交价格')
        def get(self, *args, **kwargs):
            return Response(200, {'data': {'listGoods': [{'nmID': 123, 'discount': 30,
                'sizes': [{'price': 124}]}]}})
    monkeypatch.setattr(price.requests, 'Session', Session)
    result = price.run(ReconcileClient(), '/job', {'id': 'e' * 32, 'attempts': 1}, tmp_path, Event())
    assert result == {'verified': True, 'verified_existing': True, 'upload_id': 0, 'rows': SNAPSHOT['rows']}
