import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

from erp.domains.orders import legacy_engine as engine


def exercise(monkeypatch, responses, path='/v3/posting/fbs/list', count=1):
    clock = [0.0]
    calls = []
    original_sleep = asyncio.sleep

    async def sleep(delay):
        clock[0] += delay
        await original_sleep(0)

    def handler(request):
        calls.append((request.url.path, clock[0]))
        return responses[min(len(calls)-1, len(responses)-1)]

    monkeypatch.setattr(engine, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(asyncio, 'sleep', sleep)

    async def run():
        client = engine.OzonClient({'ozon_client_id':'test', 'ozon_api_key':'test'})
        await client.http.aclose()
        client.http = httpx.AsyncClient(base_url='https://example.test', transport=httpx.MockTransport(handler))
        try:
            await asyncio.gather(*(client._post(path, {}) for _ in range(count)))
        except engine.OrderError as error:
            return error
        finally:
            await client.aclose()

    return asyncio.run(run()), calls


def test_read_retries_non_json_429_and_honors_retry_after(monkeypatch):
    error, calls = exercise(monkeypatch, [httpx.Response(429, text='rate limit', headers={'Retry-After':'5'}), httpx.Response(200, json={})])
    assert error is None
    assert len(calls) == 2 and calls[1][1] >= 5


def test_retries_bounded(monkeypatch):
    error, calls = exercise(monkeypatch, [httpx.Response(429)])
    assert error.status_code == 429 and len(calls) == 4


def test_long_cooldown_stops_without_early_retry(monkeypatch):
    error, calls = exercise(monkeypatch, [httpx.Response(429, headers={'Retry-After':'120'})])
    assert error.status_code == 429 and len(calls) == 1


def test_retry_after_http_date(monkeypatch):
    now = datetime.now(UTC).replace(microsecond=0)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return now
    monkeypatch.setattr(engine, 'datetime', Clock)
    date = format_datetime(now + timedelta(seconds=20))
    error, calls = exercise(monkeypatch, [httpx.Response(429, headers={'Retry-After':date}), httpx.Response(200, json={})])
    assert error is None and calls[1][1] > 18


@pytest.mark.parametrize('status,path', [(403,'/v3/posting/fbs/list'), (429,'/v3/posting/fbs/ship')])
def test_no_retry_of_auth_errors_or_write_endpoints(monkeypatch, status, path):
    error, calls = exercise(monkeypatch, [httpx.Response(status, json={'message':'denied'})], path)
    assert error.status_code == status and len(calls) == 1


def test_detail_reads_share_spacing(monkeypatch):
    error, calls = exercise(monkeypatch, [httpx.Response(200, json={})], '/v3/posting/fbs/get', 5)
    assert error is None and len(calls) == 5
    assert all(b[1]-a[1] >= 1.099 for a,b in zip(calls,calls[1:]))


def test_detail_rate_limit_not_silently_ignored():
    async def run():
        client = engine.OzonClient({'ozon_client_id':'test','ozon_api_key':'test'})
        async def post(path, payload):
            if path.endswith('/list'):
                return {'result':{'postings':[{'posting_number':'N1'}]}}
            raise engine.OrderError('limited', status_code=429)
        client._post = post
        try:
            with pytest.raises(engine.OrderError) as caught:
                await client.postings()
            assert caught.value.status_code == 429
        finally:
            await client.aclose()
    asyncio.run(run())
