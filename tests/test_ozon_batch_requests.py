from pathlib import Path
from types import SimpleNamespace as Obj
import json
import pytest
from cryptography.fernet import Fernet
from erp.integrations.ozon import OzonSellerAdapter, OzonApiError
from erp.services import catalog_ozon_batch as server
from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps

def test_duplicate_stock_receipt_does_not_hide_other_success(monkeypatch):
    children=[Obj(id=str(n),offer_id='A' if n<3 else 'B',external_product_id='101' if n<3 else '102',status='stock_pending',stock_status='unknown',promotion_status='pending',last_error='',assets_json='{}') for n in (1,2,3)]
    rows=[(Obj(stores=[c]),c,{'params':{'warehouse_id':10,'stock':500}}) for c in children]
    job=Obj(id='job',external_ref='',status='running',steps=[Obj(key=c.id,status='pending',progress=0) for c in children])
    monkeypatch.setattr(server,'snapshot',lambda *args:{'operation_hash':'hash','phase':'stock'})
    monkeypatch.setattr(server,'targets',lambda *args:('stock',rows))
    session=Obj(add=lambda event:None)
    server.accept(session,job,'intent',{'operation_hash':'hash'})
    body={'operation_hash':'hash','result':{'result':[
        {'offer_id':'A','warehouse_id':10,'product_id':0,'updated':False,'errors':[]},
        {'offer_id':'A','warehouse_id':10,'product_id':101,'updated':True,'errors':[]},
        {'offer_id':'B','warehouse_id':10,'product_id':102,'updated':False,'errors':[{'code':'PRODUCT_IS_NOT_CREATED','message':'Product is not created'}]},
    ]}}
    server.accept(session,job,'result',body)
    assert all(c.status=='promotion_pending' and c.stock_status=='completed' for c in children[:2])
    assert all(json.loads(c.assets_json)['stock_confirmation']['quantity']==500 for c in children[:2])
    assert children[2].status=='failed' and children[2].stock_status=='failed'
    assert job.steps[2].status=='failed'
    assert server.accept(session,job,'result',body)['catalog_ozon_finalized']

def test_duplicate_ledger_stock_is_one_platform_item(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    from catalog_ozon_batch_worker import stock_requests
    entries=[{'offer_id':'SKU','params':{'warehouse_id':10,'stock':500}} for _ in range(2)]
    assert stock_requests(entries)==[{'offer_id':'SKU','warehouse_id':10,'stock':500}]
    entries[1]['params']['stock']=100
    with pytest.raises(ValueError):stock_requests(entries)


def test_updated_promotion_money_and_removed_is_not_success():
    adapter = OzonSellerAdapter({'currency': 'CNY'}, repository=Obj())
    calls = []
    def post(path, body):
        calls.append((path, body))
        return {'active_product_ids': [], 'deactivated_product_ids': ['99']}
    adapter.post = post
    with pytest.raises(OzonApiError): adapter.activate_promotion(5, 99, '124.50', 100)
    assert calls == [('/v1/actions/products/update', {'action_id': 5, 'products': [
        {'product_id': 99, 'action_price': {'amount': '124.50', 'currency': 'CNY'}, 'stock': '100'}]})]


def test_unlimited_promotion_omits_quantity_limit():
    from erp.schemas import PublicationPromotionRequest
    request=PublicationPromotionRequest(action_id=5,action_price=62.59)
    assert request.stock is None
    adapter=OzonSellerAdapter({'currency':'CNY'},repository=Obj())
    calls=[]
    adapter.post=lambda path,body:(calls.append((path,body)) or {'active_product_ids':['99']})
    adapter.activate_promotion(5,99,request.action_price,request.stock)
    assert calls[0][1]['products']==[{'product_id':99,'action_price':{'amount':'62.59','currency':'CNY'}}]


def test_candidates_use_opaque_cursor_and_stop_when_found():
    adapter = OzonSellerAdapter({}, repository=Obj())
    calls = []
    def post(path, body):
        calls.append((path, body))
        return {'products': [{'id': 1 if len(calls) == 1 else 99}], 'last_id': 'opaque-page'}
    adapter.post = post
    assert adapter.find_promotion_candidate(5, 99)['id'] == 99
    assert [body['last_id'] for _, body in calls] == ['', 'opaque-page']
    assert all(path == '/v2/actions/candidates' and 'offset' not in body for path, body in calls)


def test_grouping_uses_decrypted_configuration_not_random_ciphertext(monkeypatch):
    from erp.core.security import vault
    from erp.core.task_identity import task_identity
    children = [Obj(id=str(n), store_id='store', external_product_id=str(n), offer_id=f'SKU-{n}',
                    status='stock_pending', stock_status='pending', settings_snapshot_json=dumps({
                        'public': {'currency': 'CNY'}, 'secrets_encrypted': vault.encrypt({'store': {'api_key': 'test'}})}))
                for n in range(1, 103)]
    from erp.services.catalog_ozon_stock_scan import revision
    from erp.models import utc_now
    for child in children:
        child.external_task_id='10';child.assets_json='{}'
        child.assets_json=dumps({'stock_scan':{'outcome':'ready','observed_at':utc_now().isoformat(),'revision':revision(child)}})
    assert children[0].settings_snapshot_json != children[1].settings_snapshot_json
    publications = {str(n): Obj(id=str(n), product_snapshot_json=dumps({'sku':f'SKU-{n}'}), stores=[c]) for n, c in enumerate(children, 1)}
    cursor = iter(children); captured = []
    monkeypatch.setattr(server, 'get_publication', lambda session, identity: publications[identity])
    def enqueue(session, **kwargs):
        captured.append(kwargs)
        return Obj(id=f'job-{len(captured)}')
    monkeypatch.setattr(server, 'enqueue_job', enqueue)
    session = Obj(get=lambda *args: Obj(capabilities_json=dumps(['catalog_ozon_batch'])),
                  scalar=lambda query: next(cursor), commit=lambda: None)
    token = task_identity.set({'worker_id': 'pc'})
    try:
        result = server.enqueue(session, 'stock', [{'publication_id': p.id, 'store_id': 'store',
            'params': {'warehouse_id': 10 if index < 101 else 20, 'stock': 100}}
            for index, p in enumerate(publications.values())])
    finally: task_identity.reset(token)
    assert result['product_count'] == 102 and result['batch_count'] == 3
    assert [len(c['payload']['batch_entries']) for c in captured] == [100, 1, 1]
    assert len({entry['params']['warehouse_id'] for entry in captured[-1]['payload']['batch_entries']}) == 1


def test_100_real_publication_models_submit_without_sku_property(monkeypatch):
    from erp.models import Publication, PublicationStore
    from erp.core.task_identity import task_identity
    publications={}; children=[]; captured=[]
    for n in range(100):
        child=PublicationStore(id=f'child-{n}',store_id='store',offer_id=f'PREFIX-{n}',
            external_task_id='',external_product_id='',status='ready_for_submit',
            settings_snapshot_json=dumps({'public':{}}),
            assets_json=dumps({'package_ready':True,'import_item':{'offer_id':f'PREFIX-{n}'}}))
        publication=Publication(id=f'pub-{n}',product_snapshot_json=dumps({'sku':f'SKU-{n}'}),stores=[child])
        assert not hasattr(publication,'sku')
        publications[publication.id]=publication;children.append(child)
    cursor=iter(children)
    monkeypatch.setattr(server,'get_publication',lambda session,identity:publications[identity])
    monkeypatch.setattr(server,'enqueue_job',lambda session,**kwargs: captured.append(kwargs) or Obj(id='batch-job'))
    session=Obj(get=lambda *args:Obj(capabilities_json=dumps(['catalog_ozon_batch'])),
                scalar=lambda query:next(cursor),commit=lambda:None)
    token=task_identity.set({'worker_id':'pc'})
    try:
        result=server.enqueue(session,'submit',[{'publication_id':p.id,'store_id':'store'} for p in publications.values()])
    finally:task_identity.reset(token)
    assert result['batch_count']==1 and result['product_count']==100
    assert len(captured[0]['steps'])==100
    assert captured[0]['steps'][0]==('child-0','SKU-0 · submit')
    assert all(c.status=='submit_queued' for c in children)


@pytest.mark.parametrize('phase', ['submit', 'stock', 'promotion'])
@pytest.mark.parametrize('fault', ['', 'cloud', 'platform'])
def test_one_platform_write_for_100_products_and_safe_resume(tmp_path, monkeypatch, phase, fault):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client/desktop'))
    from catalog_ozon_batch_worker import execute
    cipher = Fernet(Fernet.generate_key())
    protect = lambda data, decrypt=False: cipher.decrypt(data) if decrypt else cipher.encrypt(data)
    entries = [{'offer_id': f'SKU-{n}', 'product_id': str(n), 'item': {'offer_id': f'SKU-{n}'},
                'params': {'warehouse_id': 1, 'stock': 100, 'action_id': 2, 'action_price': '124'}} for n in range(1, 101)]
    plan = {'job_id': 'a'*32, 'operation_hash': 'b'*64, 'phase': phase, 'entries': entries}
    writes = []; reads = []; receipts = []
    def write(path, body, **kwargs):
        writes.append((path, body))
        if fault == 'platform': raise OSError('lost response')
        return {'result': {'task_id': 77}} if phase == 'submit' else {'result': []}
    def promotions(action, products): return write('/v1/actions/products/update', {'products': products})
    def candidates(*args):
        reads.append(args)
        return {'products': [{'id': n} for n in range(1, 101)]}
    adapter = Obj(post=write, update_promotions=promotions, promotion_candidates=candidates,
                  wait_import=lambda task: {'result': {'items': []}})
    def post(action, body):
        if action == 'result':
            receipts.append(body)
            if fault == 'cloud' and len(receipts) == 1: raise OSError('lost cloud receipt')
        return {'catalog_ozon_finalized': True}
    if fault:
        with pytest.raises(OSError): execute(plan, tmp_path/"batches"/plan["job_id"], adapter, protect, post, lambda: None)
    else: execute(plan, tmp_path/"batches"/plan["job_id"], adapter, protect, post, lambda: None)
    if fault == 'platform':
        with pytest.raises(ValueError, match='禁止自动重发'): execute(plan, tmp_path/"batches"/plan["job_id"], adapter, protect, post, lambda: None)
    else: assert execute(plan, tmp_path/"batches"/plan["job_id"], adapter, protect, post, lambda: None)['catalog_ozon_finalized']
    assert len(writes) == 1
    assert len(writes[0][1]['items' if phase == 'submit' else 'stocks' if phase == 'stock' else 'products']) == 100
    assert len(reads) == (1 if phase == 'promotion' else 0)


@pytest.mark.parametrize('phase', ['stock', 'promotion'])
def test_server_keeps_per_product_partial_failure(monkeypatch, phase):
    children = [Obj(id=str(n), offer_id=f'SKU-{n}', external_product_id=str(n), status=phase+'_pending',
                    stock_status='processing', promotion_status='processing', last_error='', assets_json='{}') for n in (1, 2)]
    rows = [(Obj(stores=[c]), c, {'params': {'warehouse_id': 10, 'stock': 500}}) for c in children]
    steps = [Obj(key=c.id, status='pending', progress=0) for c in children]
    job = Obj(id='job', external_ref='', steps=steps, status='running')
    monkeypatch.setattr(server, 'snapshot', lambda *args: {'operation_hash': 'hash', 'phase': phase})
    monkeypatch.setattr(server, 'targets', lambda *args: (phase, rows))
    session = Obj(add=lambda event: None)
    server.accept(session, job, 'intent', {'operation_hash': 'hash'})
    result = {'result': [{'offer_id': 'SKU-1', 'warehouse_id': 10, 'updated': True},
                         {'offer_id': 'SKU-2', 'warehouse_id': 10, 'errors': [{'message': 'bad'}]}]} if phase == 'stock' else {
                         'active_product_ids': ['1'], 'deactivated_product_ids': ['2'], 'warnings': []}
    body = {'operation_hash': 'hash', 'result': result}
    server.accept(session, job, 'result', body)
    assert steps[0].status == 'completed' and steps[1].status == 'failed'
    assert job.status == 'action_required' and children[1].last_error
    assert server.accept(session, job, 'result', body)['catalog_ozon_finalized']
    with pytest.raises(ConflictError): server.accept(session, job, 'result', {**body, 'result': {}})


def test_promotions_dedup_by_platform_id_before_request(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    from catalog_ozon_batch_worker import promotion_requests
    entries=[{'product_id':'99','params':{'action_price':58.09,'stock':1}} for _ in range(2)]
    assert promotion_requests(entries)==[{'product_id':99,'action_price':58.09,'stock':1}]
    entries[1]['params']['action_price']=59
    with pytest.raises(ValueError):promotion_requests(entries)


@pytest.mark.parametrize('duplicate_error',[True,False])
def test_promotion_confirmed_rejection_is_saved_but_timeout_stays_unknown(monkeypatch,tmp_path,duplicate_error):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    from catalog_ozon_batch_worker import execute
    entries=[{'product_id':'99','offer_id':'SKU','params':{'action_id':7,'action_price':58.09,'stock':1}} for _ in range(2)]
    plan={'job_id':'job','phase':'promotion','operation_hash':'hash','entries':entries}
    writes=[];results=[]
    def update(action,rows):
        writes.append(rows);assert len(rows)==1
        if duplicate_error:raise OzonApiError('products have duplicate id',status_code=400,request_path='/v1/actions/products/update')
        raise OSError('timeout')
    adapter=Obj(promotion_candidates=lambda *a:{'products':[{'id':99}]},update_promotions=update)
    def post(action,body):
        if action=='result':results.append(body)
        return {'ok':True}
    protect=lambda data,**kw:data
    if duplicate_error:
        execute(plan,tmp_path/'batches'/'job',adapter,protect,post,lambda:None)
        execute(plan,tmp_path/'batches'/'job',adapter,protect,post,lambda:None)
        assert results[0]['result']['rejected'][0]['product_id']==99
        assert json.loads((tmp_path/'batches'/'job'/'batch-receipt.dpapi').read_bytes())['state']=='result_received'
    else:
        with pytest.raises(OSError):execute(plan,tmp_path/'batches'/'job',adapter,protect,post,lambda:None)
        with pytest.raises(ValueError):execute(plan,tmp_path/'batches'/'job',adapter,protect,post,lambda:None)
        assert not results
    assert len(writes)==1


def test_confirmed_legacy_receipt_replays_without_revalidating_conflicting_aliases(monkeypatch,tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    from catalog_ozon_batch_worker import execute
    entries=[{'product_id':'99','offer_id':'SKU','params':{'action_id':7,'action_price':price,'stock':1}} for price in (58.09,50.66)]
    plan={'job_id':'job','phase':'promotion','operation_hash':'hash','entries':entries}
    directory=tmp_path/'batches'/'job';directory.mkdir(parents=True)
    result={'active_product_ids':[],'rejected':[{'product_id':99,'reason':'explicit rejection'}]}
    (directory/'batch-receipt.dpapi').write_text(json.dumps({'job_id':'job','operation_hash':'hash','state':'result_received','result':result}))
    receipts=[]
    def post(action,body):
        if action=='result':receipts.append(body['result'])
        return {'ok':True}
    execute(plan,directory,Obj(),lambda data,**kw:data,post,lambda:None)
    assert receipts==[result]
