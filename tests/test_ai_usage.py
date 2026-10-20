import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from erp.core import ai_usage as usage, ai_concurrency as gate
from erp.core.database import SessionLocal
from erp.models import SystemSetting


@pytest.fixture(autouse=True)
def ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(usage, 'get_settings', lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(gate, '_root', lambda: tmp_path)
    monkeypatch.setattr(gate, 'limits', lambda: {'text': 5, 'image': 3})
    prices = [dict(host='www.micuapi.ai', kind='text', model='text-model', currency='CNY', mode='per_million_tokens', input=5, output=30),
              dict(host='www.micuapi.ai', kind='image', model='image-model', currency='CNY', mode='per_image', rate=.1)]
    with SessionLocal() as session:
        session.add(SystemSetting(key='ai_pricing', value_json=json.dumps(prices)))
        session.commit()


def response(body, status=200):
    return SimpleNamespace(status_code=status, headers={'x-request-id': 'request-1'}, json=lambda: body)


def send(kind='text', body=None, status=200):
    with usage.task_scope('job-1', 'test', 1):
        return gate.post(kind, lambda *args, **kwargs: response(body, status), 'https://www.micuapi.ai/v1/chat?secret=hidden', json={'model': kind + '-model', 'messages': ['private prompt']})


def test_token_cost_and_snapshot_not_repriced():
    send(body={'usage': {'prompt_tokens': 1000, 'completion_tokens': 200}})
    with SessionLocal() as session:
        row = session.get(SystemSetting, 'ai_pricing'); row.value_json = '[]'; session.commit()
    item = usage.report(['job-1'])['items'][0]
    assert item['text'] == item['success'] == 1
    assert Decimal(item['costs']['CNY']) == Decimal('.011')
    with usage.connect() as db:
        row = dict(db.execute('SELECT * FROM requests').fetchone())
    assert 'private prompt' not in json.dumps(row) and 'hidden' not in json.dumps(row)


def test_images_count_returned_not_requested():
    send('image', {'data': [{'url': 'one'}, {'b64_json': 'two'}]})
    item = usage.report(['job-1'])['items'][0]
    assert item['image'] == 1
    assert Decimal(item['costs']['CNY']) == Decimal('.2')


def test_errors_and_retry_are_counted_without_free_cost_assumption():
    send(body={'error': 'bad'}, status=500)
    send(body={'usage': {'input_tokens': 100, 'output_tokens': 100}})
    with usage.task_scope('job-1', 'test', 2):
        with pytest.raises(TimeoutError):
            gate.post('text', lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()), 'https://www.micuapi.ai', json={'model': 'text-model'})
    item = usage.report(['job-1'])['items'][0]
    assert item['text'] == 3 and item['errors'] == 1 and item['pending'] == 1
    assert item['unknown_cost'] == 2


def test_missing_and_cached_usage_are_unknown_not_zero():
    send(body={})
    send(body={'usage': {'input_tokens': 100, 'output_tokens': 10, 'input_tokens_details': {'cached_tokens': 50}}})
    item = usage.report(['job-1'])['items'][0]
    assert item['unknown_cost'] == 2 and item['costs'] == {}


def test_async_records_before_caller_validation_and_only_once():
    class Response:
        status = 200
        headers = {}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self, **kwargs): return {'data': [{'url': 'one'}]}
    class Client:
        def post(self, *args, **kwargs):
            assert 'usage_model' not in kwargs
            return Response()
    async def run():
        with usage.task_scope('job-1', 'test', 1):
            with pytest.raises(ValueError):
                async with gate.async_post('image', Client(), 'https://www.micuapi.ai', data=object(), usage_model='image-model'):
                    raise ValueError('downstream validation')
    asyncio.run(run())
    item = usage.report(['job-1'])['items'][0]
    assert item['image'] == item['success'] == 1
    assert Decimal(item['costs']['CNY']) == Decimal('.1')


def test_async_read_timeout_is_unknown():
    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self, **kwargs): raise TimeoutError()
    class Client:
        def post(self, *args, **kwargs): return Response()
    async def run():
        with usage.task_scope('job-1', 'test', 1):
            with pytest.raises(TimeoutError):
                async with gate.async_post('image', Client(), 'https://www.micuapi.ai', json={'model':'image-model'}): pass
    asyncio.run(run())
    assert usage.report(['job-1'])['items'][0]['pending'] == 1


def test_api_and_invalid_prices(client):
    assert client.get('/api/v1/ai-usage', headers={'Authorization': ''}).status_code in (401, 403)
    send(body={'usage': {'input_tokens': 10, 'output_tokens': 10}})
    result = client.get('/api/v1/ai-usage', params={'job_id': 'job-1'})
    assert result.status_code == 200 and result.json()['items'][0]['text'] == 1
    for value in ({}, [{'host': 'host', 'kind': 'text', 'model': 'm', 'currency':'CNY', 'mode': 'per_request', 'rate':'NaN'}]):
        assert client.put('/api/v1/settings/ai_pricing', json={'value': value}).status_code == 422


def test_ledger_failure_does_not_replay_success(monkeypatch):
    original = usage.connect
    calls = []
    def sender(*args, **kwargs):
        calls.append(1)
        monkeypatch.setattr(usage, 'connect', lambda: (_ for _ in ()).throw(OSError('disk full')))
        return response({'usage': {'input_tokens': 100, 'output_tokens': 100}})
    with usage.task_scope('job-1', 'test', 1):
        result = gate.post('text', sender, 'https://www.micuapi.ai', json={'model': 'text-model'})
    assert result.status_code == 200 and len(calls) == 1
    monkeypatch.setattr(usage, 'connect', original)
    assert usage.report(['job-1'])['items'][0]['pending'] == 1


def test_parallel_jobs_remain_separate_and_initial_failure_sends_nothing(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    def work(index):
        with usage.task_scope(f'parallel-{index % 2}', 'test', 1):
            gate.post('text', lambda *a, **kw: response({'usage': {'input_tokens': 1000, 'output_tokens': 200}}), 'https://www.micuapi.ai', json={'model': 'text-model'})
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(work, range(20)))
    for item in usage.report(['parallel-0', 'parallel-1'])['items']:
        assert item['text'] == 10
        assert Decimal(item['by_kind']['text']['costs']['CNY']) == Decimal('.11')
    monkeypatch.setattr(usage, 'connect', lambda: (_ for _ in ()).throw(OSError('disk full')))
    calls = []
    with pytest.raises(OSError):
        gate.post('text', lambda *a, **kw: calls.append(1), 'https://www.micuapi.ai')
    assert calls == []
