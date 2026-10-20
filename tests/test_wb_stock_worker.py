import json
import sys
from pathlib import Path
from threading import Event

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'client/desktop'))
import wb_stock_worker as stock


def test_stock_writes_once_and_resumes_from_readback(tmp_path, monkeypatch):
    snapshot = {'rows': [{'id': 'a' * 32, 'offer_id': 'SKU-1', 'nm_id': '123'}],
                'warehouse_id': 2073831, 'amount': 100, 'config': {'store': {'secrets': {'token': 'test'}}}}
    class Client:
        def request(self, method, path, body, extra_headers):
            assert path.endswith('/wb-stock-snapshot')
            return snapshot, {}
    card_reads = []
    def find_cards(*args):
        card_reads.append(1)
        return {'SKU-1': {'nmID': 123, 'sizes': [{'chrtID': 456}]}}
    monkeypatch.setattr(stock, 'find_cards_exact', find_cards)
    calls = {'put': 0, 'amount': 0}
    class Response:
        def __init__(self, status, body=None): self.status_code = status; self.body = body or {}
        def raise_for_status(self): assert self.status_code < 400
        def json(self): return self.body
    class Session:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def __init__(self): self.headers = {}
        def post(self, url, json, timeout):
            return Response(200, {'stocks': [{'chrtId': 456, 'amount': calls['amount']}]})
        def put(self, url, json, timeout):
            calls['put'] += 1
            assert json == {'stocks': [{'chrtId': 456, 'amount': 100}]}
            calls['amount'] = 100
            return Response(204)
    monkeypatch.setattr(stock.requests, 'Session', Session)
    job = {'id': 'b' * 32, 'attempts': 1}
    first = stock.run(Client(), '/worker/job', job, tmp_path, Event())
    assert first['verified'] and calls['put'] == 1
    job['attempts'] = 2
    second = stock.run(Client(), '/worker/job', job, tmp_path, Event())
    assert second == first and calls['put'] == 1
    assert card_reads == [1]
    assert json.loads((tmp_path / 'wb-stock-tasks' / job['id'] / 'write.json').read_text())['state'] == 'done'


def test_stock_pending_unknown_never_resends(tmp_path, monkeypatch):
    snapshot = {'rows': [{'id': 'a' * 32, 'offer_id': 'SKU-1', 'nm_id': '123'}],
                'warehouse_id': 2073831, 'amount': 100, 'config': {'store': {'secrets': {'token': 'test'}}}}
    class Client:
        def request(self, *args, **kwargs): return snapshot, {}
    monkeypatch.setattr(stock, 'find_cards_exact', lambda *args: {
        'SKU-1': {'nmID': 123, 'sizes': [{'chrtID': 456}]}})
    puts = []
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {'stocks': [{'chrtId': 456, 'amount': 0}]}
    class Session:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def __init__(self): self.headers = {}
        def post(self, *args, **kwargs): return Response()
        def put(self, *args, **kwargs):
            puts.append(1)
            raise stock.requests.Timeout('uncertain')
    monkeypatch.setattr(stock.requests, 'Session', Session)
    job = {'id': 'b' * 32, 'attempts': 1}
    with pytest.raises(stock.StockOutcomeUnknown):
        stock.run(Client(), '/worker/job', job, tmp_path, Event())
    assert puts == [1]
    with pytest.raises(stock.StockOutcomeUnknown):
        stock.run(Client(), '/worker/job', job, tmp_path, Event())
    assert puts == [1]
