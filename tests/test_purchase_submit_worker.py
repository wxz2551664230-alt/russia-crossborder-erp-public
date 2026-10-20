from pathlib import Path
from erp.domains.orders import desktop_purchase_submit as bridge, service
from test_pod_orders import orders
from test_purchase_submit_bridge import setup, task
from contextlib import closing
import pytest
from erp.core.security import vault
from erp.domains.orders.legacy_engine import HicustomOrderQueue, OrderError


def transport(job):
    def post(path,body):
        if path=='/purchase-submit-snapshot':return bridge.snapshot(None,job)
        if path=='/purchase-operation':return bridge.operation(None,job,body['method'],body['args'],body['kwargs'])
        if path=='/purchase-submit-result':return bridge.completed_state(None,job)
        if path=='/purchase-write-intent':return bridge.write_intent(None,job)
        raise AssertionError(path)
    return post


def test_original_s2b_purchase_runs_locally_and_completed_result_recovers(orders,monkeypatch,tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    import purchase_submit_worker
    setup(orders,monkeypatch);job=task()
    orders.update_batch_group('group',fulfillment_product_code='ready',remote_product_id=123,logistics_id=109)
    calls=[]
    class Supplier:
        async def order_by_out_order_id(self,external):calls.append('lookup');return None
        async def create_order(self,payload):
            assert payload['third_order_id']=='external' and payload['logistics_id']==109
            assert payload['store_id']==1 and payload['items'][0]['product_id']==123
            calls.append('create');return {'order_id':'remote','id':'remote','subtotal':'10'}
        def close(self):calls.append('close')
    monkeypatch.setattr(service,'supplier_client',lambda *a:Supplier())
    def post(path,body):
        if path=='/purchase-submit-snapshot':return bridge.snapshot(None,job)
        if path=='/purchase-operation':return bridge.operation(None,job,body['method'],body['args'],body['kwargs'])
        if path=='/purchase-submit-result':return bridge.completed_state(None,job)
        if path=='/purchase-write-intent':return bridge.write_intent(None,job)
        raise AssertionError(path)
    result=purchase_submit_worker.execute(tmp_path,{'id':job.id},post,lambda:None)
    assert result=={'submitted':1}
    assert calls==['lookup','create','close']
    assert orders.get_batch_group('group')['submission_status']=='submitted'
    assert purchase_submit_worker.execute(tmp_path,{'id':job.id},post,lambda:None)==result
    assert calls==['lookup','create','close']


@pytest.mark.parametrize('provider',['hicustom','s2b'])
@pytest.mark.parametrize('interrupt_before_save',[False,True])
def test_original_product_rebuild_and_purchase_flow(orders,monkeypatch,tmp_path,provider,interrupt_before_save):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    import purchase_submit_worker
    setup(orders,monkeypatch);job=task()
    job.config_snapshot_json=vault.encrypt({provider+'_store_id':1})
    with closing(orders._connect()) as db:
        db.execute('UPDATE batch_order_groups SET supplier=?,created_at=?',(provider,'2026-09-24T00:00:00+00:00'));db.commit()
    monkeypatch.setattr(orders.library,'get_product',lambda sku:{'sku':sku,'supplier':provider,'recipe':{
        'supplier':provider,'product_type_id':1,'default_color_id':2,'sides':[{'view_id':1,'mime_type':'image/png'}]},'oss_assets':[]})
    async def source(*a):return b'image','image.png','image/png'
    monkeypatch.setattr(HicustomOrderQueue,'_source_content',source)
    calls=[]
    class Supplier:
        async def upload_gallery(self,**kwargs):
            assert kwargs['content']==b'image';calls.append('upload');return {'code':'material'}
        async def create_product(self,value):
            assert value['sides'][0]['gallery_code']=='material';calls.append('product');return {'id':123,'code':'created-product'}
        async def calculate_products(self,**kwargs):
            assert kwargs['products'][0]['product_id']==123;calls.append('quote');return [{'logistics_id':109,'name':'channel','amount':'2'}]
        async def order_by_out_order_id(self,key):calls.append('lookup');return None
        async def create_order(self,value):
            assert value['store_id']==1
            if provider=='s2b':assert value['logistics_id']==109 and value['items'][0]['product_id']==123
            else:assert value['order_items'][0]['product_code']=='created-product'
            calls.append('order');return {'id':'remote','order_id':'remote','subtotal':'10'}
        def close(self):calls.append('close')
    monkeypatch.setattr(service,'supplier_client',lambda *a:Supplier())
    interrupted=[];send=transport(job)
    def post(path,body):
        if interrupt_before_save and not interrupted and path=='/purchase-operation' and body['method']=='update_batch_group' and body['kwargs'].get('fulfillment_product_code'):
            interrupted.append(True);raise RuntimeError('lost before shared product update')
        return send(path,body)
    if interrupt_before_save:
        with pytest.raises(RuntimeError,match='lost before'):
            purchase_submit_worker.execute(tmp_path,{'id':job.id},post,lambda:None)
        assert calls==['upload','product','close']
        calls.remove('close')
    result=purchase_submit_worker.execute(tmp_path,{'id':job.id},post,lambda:None)
    assert result=={'submitted':1}
    assert calls==(['upload','product','quote','lookup','order','close'] if provider=='s2b' else ['upload','product','lookup','order','close'])
    assert orders.get_batch_group('group')['fulfillment_product_code']=='created-product'


def test_hicustom_merged_purchase_submits_once_for_all_groups(orders,monkeypatch,tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    import purchase_submit_worker
    setup(orders,monkeypatch);job=task();job.config_snapshot_json=vault.encrypt({'hicustom_store_id':1})
    with closing(orders._connect()) as db:
        db.execute("UPDATE batch_order_groups SET supplier='hicustom',purchase_id='purchase',fulfillment_product_code='ready',created_at='2026-09-24'")
        row=dict(db.execute('SELECT * FROM batch_order_groups').fetchone());row['id']='second';row['sku']='SECOND'
        db.execute('INSERT INTO batch_order_groups('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',list(row.values()));db.commit()
    calls=[]
    class Supplier:
        async def order_by_out_order_id(self,key):calls.append('lookup');return None
        async def create_order(self,value):
            assert len(value['order_items'])==2 and value['out_order_id']=='BATCH-batch-P-purchase'
            calls.append('create');return {'order_id':'merged','subtotal':'20','shipping_amount':'4'}
        def close(self):pass
    monkeypatch.setattr(service,'supplier_client',lambda *a:Supplier())
    result=purchase_submit_worker.execute(tmp_path,{'id':job.id},transport(job),lambda:None)
    assert result=={'submitted':2} and calls==['lookup','create']
    assert {g['hicustom_order_id'] for g in orders.list_purchase_groups('purchase')}=={'merged'}
    assert purchase_submit_worker.execute(tmp_path,{'id':job.id},transport(job),lambda:None)==result
    assert calls==['lookup','create']


def test_unknown_supplier_order_is_not_resubmitted(orders,monkeypatch,tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    import purchase_submit_worker
    setup(orders,monkeypatch);job=task()
    orders.update_batch_group('group',fulfillment_product_code='ready',remote_product_id=123,logistics_id=109)
    calls=[]
    class Supplier:
        async def order_by_out_order_id(self,key):return None
        async def create_order(self,value):calls.append(True);raise TimeoutError('response lost')
        def close(self):pass
    monkeypatch.setattr(service,'supplier_client',lambda *a:Supplier())
    for _ in range(2):
        with pytest.raises(OrderError) as error:
            purchase_submit_worker.execute(tmp_path,{'id':job.id},transport(job),lambda:None)
        assert error.value.outcome_unknown
    assert calls==[True] and orders.get_batch_group('group')['submission_status']=='unknown'
