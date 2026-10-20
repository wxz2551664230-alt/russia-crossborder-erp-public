import asyncio
from contextlib import closing
import pytest

from erp.domains.orders.receipts import apply_once
from erp.domains.orders.legacy_engine import OrderError
from test_pod_orders import orders, posting


def test_receipt_replay_does_not_overwrite_newer_state(orders):
    orders.import_postings([posting()], store_id='A')
    key=orders.list_orders()['orders'][0]['order_key']
    calls=[]
    def apply(private):
        calls.append(True)
        private.update_submission(key,submission_status='submitted',hicustom_order_id='first')
        return {'updated':1}
    assert apply_once(orders,'task:0',{'remote':'first'},apply)=={'updated':1}
    orders.update_submission(key,hicustom_order_id='newer')
    assert apply_once(orders,'task:0',{'remote':'first'},apply)=={'updated':1}
    assert calls==[True]
    assert orders.get_order(key)['hicustom_order_id']=='newer'
    with pytest.raises(OrderError):apply_once(orders,'task:0',{'remote':'different'},apply)


def test_all_nested_writes_rollback_before_receipt(orders):
    orders.import_postings([posting('A'),posting('B')],store_id='A')
    keys=[row['order_key'] for row in orders.list_orders()['orders']]
    def fail(private):
        for key in keys:
            private.update_submission(key,submission_status='submitted')
        raise RuntimeError('simulate process step failure')
    with pytest.raises(RuntimeError):apply_once(orders,'task:1',{},fail)
    assert all(orders.get_order(key)['submission_status']=='not_submitted' for key in keys)
    def success(private):
        for key in keys:private.update_submission(key,submission_status='submitted')
        return {'updated':2}
    assert apply_once(orders,'task:1',{},success)=={'updated':2}
    assert all(orders.get_order(key)['submission_status']=='submitted' for key in keys)


def test_original_async_thread_calls_share_receipt_transaction(orders):
    orders.import_postings([posting()],store_id='A')
    key=orders.list_orders()['orders'][0]['order_key']
    def apply(private):
        async def update():
            await asyncio.to_thread(private.update_submission,key,submission_status='submitted')
            row=await asyncio.to_thread(private.get_order,key)
            assert row['submission_status']=='submitted'
            return {'updated':1}
        return asyncio.run(update())
    assert apply_once(orders,'task:threads',{},apply)=={'updated':1}
    with closing(orders._connect()) as db:
        assert db.execute('select count(*) from erp_operation_receipts').fetchone()[0]==1
    assert orders.get_order(key)['submission_status']=='submitted'


def test_private_operation_receipt_encrypts_address_and_replays(orders):
    calls=[]
    def apply(private):calls.append(True);return {'address':'PRIVATE_ADDRESS','id':'group'}
    first=apply_once(orders,'private:1',{},apply,private_result=True)
    assert apply_once(orders,'private:1',{},apply,private_result=True)==first
    assert calls==[True]
    with closing(orders._connect()) as db:
        text=db.execute('SELECT summary_json FROM erp_operation_receipts WHERE receipt_key=?',('private:1',)).fetchone()[0]
    assert 'PRIVATE_ADDRESS' not in text and 'encrypted_result' in text
