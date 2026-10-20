import asyncio
from datetime import datetime, UTC
import pytest
from pydantic import ValidationError
from erp.domains.orders.api import SyncOrders
from erp.domains.orders.legacy_engine import OzonClient, WildberriesOrderClient, OrderError

START = "2026-10-16T16:00:00+00:00"
END = "2026-10-19T16:00:00+00:00"

@pytest.mark.parametrize("extra", [
 {"date_from":START}, {"date_from":END,"date_to":START},
 {"date_from":"2026-10-16T00:00:00","date_to":END},
 {"sync_mode":"numbers"}, {"order_numbers":["123"]},
 {"sync_mode":"numbers","order_numbers":[" "]}])
def test_invalid_scope(extra):
    with pytest.raises(ValidationError): SyncOrders(store_id="test", **extra)

def test_scope_serializes_timezone_and_deduplicates():
    body=SyncOrders(store_id="test",date_from=START,date_to=END,sync_mode="numbers",order_numbers=["A","A","B"])
    assert body.model_dump(mode="json")["date_from"].startswith("2026-10-16T16:00:00")
    assert body.order_numbers==["A","B"]

def test_ozon_numbers_never_scan_list():
    async def run():
        client=OzonClient({"ozon_client_id":"test","ozon_api_key":"test"});calls=[]
        async def post(path,body):
            calls.append((path,body))
            assert path=="/v3/posting/fbs/get"
            return {"result":{"posting_number":body["posting_number"]}}
        client._post=post
        try:
            result=await client.postings(order_numbers=["A","A","B"])
            assert len(result)==2 and len(calls)==2
        finally: await client.aclose()
    asyncio.run(run())

def test_ozon_dates_and_short_page_has_next():
    async def run():
        client=OzonClient({"ozon_client_id":"test","ozon_api_key":"test"});lists=[]
        async def post(path,body):
            if path.endswith("/get"):return {"result":{"posting_number":body["posting_number"]}}
            lists.append(body)
            return {"result":{"postings":[{"posting_number":str(len(lists))}],"has_next":len(lists)==1}}
        client._post=post
        try:
            result=await client.postings(date_from=START,date_to=END)
            assert len(result)==2 and [r["offset"] for r in lists]==[0,1]
            assert all(r["filter"]["since"]==START and r["filter"]["to"]==END for r in lists)
        finally: await client.aclose()
    asyncio.run(run())

@pytest.mark.parametrize("numbers", [None,["2"]])
def test_wb_scope_avoids_unbounded_new_and_limits_status(numbers):
    async def run():
        client=WildberriesOrderClient({"wildberries_token":"test"});calls=[]
        async def request(method,path,**kwargs):
            calls.append((path,kwargs))
            if path=="/api/v3/orders":return {"orders":[{"id":1},{"id":2}],"next":0 if numbers is None else 99}
            assert path=="/api/v3/orders/status"
            return {"orders":[]}
        client._request=request
        try:
            result=await client.orders(date_from=START,date_to=END,order_numbers=numbers)
            assert len(result)==(2 if numbers is None else 1)
            assert len(calls)==2
            assert calls[0][1]["params"]["dateFrom"]==int(datetime.fromisoformat(START).timestamp())
            assert calls[1][1]["json_body"]["orders"]==([1,2] if numbers is None else [2])
        finally:await client.aclose()
    asyncio.run(run())

def test_wb_missing_number_does_not_report_partial_success():
    async def run():
        client=WildberriesOrderClient({"wildberries_token":"test"})
        async def request(*args,**kwargs):return {"orders":[{"id":1}],"next":0}
        client._request=request
        try:
            with pytest.raises(OrderError,match="未找到"):
                await client.orders(date_from=START,date_to=END,order_numbers=["1","2"])
        finally:await client.aclose()
    asyncio.run(run())
