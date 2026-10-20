from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as Obj
import pytest
from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps,loads
from erp.models import utc_now
from erp.services import catalog_ozon_stock_scan as scan

def child(**changes):
    values=dict(id='child',publication_id='pub',store_id='store',offer_id='A',external_task_id='10',external_product_id='',assets_json='{}',settings_snapshot_json='{}',status='failed',stock_status='pending',last_error='original error')
    return Obj(**(values|changes))

def ready(**changes):
    return dict(offer_id='A',id=123,statuses={'is_created':True,'validation_status':'success','moderate_status':'approved'},errors=[],observed_at=utc_now().isoformat())|changes

def accept(monkeypatch,c,row):
    p=Obj(stores=[c]);session=Obj(scalar=lambda *a:c,add=lambda *a:None)
    job=Obj(id='job',payload_json=dumps({'store_id':'store','targets':[{'child_id':c.id,'offer_id':c.offer_id,'revision':scan.revision(c)}]}))
    monkeypatch.setattr(scan,'get_publication',lambda *a:p)
    return scan.accept(session,job,[] if row is None else [row])

def test_manually_fixed_product_is_recovered_without_new_import(monkeypatch):
    c=child();assert accept(monkeypatch,c,ready())=={'ready':1}
    assert c.status=='stock_pending' and c.external_product_id=='123' and c.external_task_id=='10'
    scan.require_fresh(c)

@pytest.mark.parametrize('row,outcome',[ (None,'not_found'),(ready(is_archived=True),'unavailable'),(ready(errors=[{'code':'BANNED'}]),'blocked'),(ready(statuses={'is_created':True,'validation_status':'success','moderate_status':'moderating'}),'pending')])
def test_non_ready_product_cannot_enter_stock(monkeypatch,row,outcome):
    c=child(status='stock_pending');assert accept(monkeypatch,c,row)=={outcome:1}
    assert c.status!='stock_pending'
    with pytest.raises(ConflictError):scan.require_fresh(c)

@pytest.mark.parametrize('stock,status',[('completed','promotion_pending'),('unknown','stock_pending'),('processing','stock_pending'),('skipped','completed')])
def test_scan_never_resets_completed_or_unknown_inventory(monkeypatch,stock,status):
    c=child(stock_status=stock,status=status);assert accept(monkeypatch,c,ready())=={'skipped':1}
    assert c.stock_status==stock and c.status==status

def test_changed_platform_id_requires_manual_check(monkeypatch):
    c=child(external_product_id='999');assert accept(monkeypatch,c,ready())=={'id_conflict':1}
    assert c.external_product_id=='999'

def test_expired_query_and_foreign_offer_rejected(monkeypatch):
    for row in [ready(observed_at=(utc_now()-timedelta(minutes=10)).isoformat()),ready(offer_id='OTHER')]:
        c=child()
        with pytest.raises(ConflictError):accept(monkeypatch,c,row)
        assert c.status=='failed'

def test_same_offer_many_ledger_rows_one_read_request(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    from catalog_ozon_options_worker import scan_items
    calls=[]
    adapter=Obj(post=lambda path,body:(calls.append((path,body)) or {'items':[ready()]}))
    rows=scan_items(adapter,[{'offer_id':'A'}]*100,Obj(is_set=lambda:False))
    assert calls==[('/v3/product/info/list',{'offer_id':['A']})]
    assert len(rows)==1

def test_store_batch_does_not_split_on_pricing_or_ai_settings(monkeypatch):
    monkeypatch.setattr(scan,'resolve_publication_snapshot',lambda text:{'store_id':'store','platform':'ozon','secrets':{'api_key':'test'},'pricing_profile':text})
    assert scan.connection_key(child(settings_snapshot_json='pricing-one'))==scan.connection_key(child(settings_snapshot_json='pricing-two'))

def test_pending_scan_is_reused_and_stays_on_original_pc(monkeypatch):
    from erp.core.task_identity import task_identity
    a=child();b=child(id='b',offer_id='B')
    pubs={'p1':Obj(stores=[a]),'p2':Obj(stores=[b])}
    monkeypatch.setattr(scan,'get_publication',lambda session,key:pubs[key])
    monkeypatch.setattr(scan,'connection_key',lambda c:'same-store-key')
    jobs=[]
    session=Obj(get=lambda *args:Obj(owner_user_id='owner',capabilities_json=dumps(['catalog_ozon_stock_scan'])),scalars=lambda query:jobs,commit=lambda:None)
    def queue(session,**kwargs):
        j=Obj(id='scan',created_by='owner',target_worker_id='pc',payload_json=dumps(kwargs['payload']))
        jobs.append(j);return j
    monkeypatch.setattr(scan,'enqueue_job',queue)
    token=task_identity.set({'id':'owner','worker_id':'pc'})
    try:
        entries=[{'publication_id':p,'store_id':'store'} for p in pubs]
        assert scan.enqueue(session,entries)['job_ids']==['scan']
        assert scan.enqueue(session,entries)['job_ids']==['scan']
        assert len(jobs)==1 and len(loads(jobs[0].payload_json,{})['targets'])==2
    finally:task_identity.reset(token)

def test_leased_scan_completion_is_atomic_and_retransmittable(monkeypatch):
    from erp.core.database import SessionLocal
    from erp.models import Store,CatalogProduct,Publication,PublicationStore,Job,JobStep
    from erp.api.routes import execution_devices as routes
    from erp.api.routes.workers import WorkerJobResult
    with SessionLocal() as session:
        store=Store(platform='ozon',name='test');product=CatalogProduct(sku='A')
        session.add_all([store,product]);session.flush()
        pub=Publication(batch_id='test',product_id=product.id)
        session.add(pub);session.flush()
        c=PublicationStore(publication_id=pub.id,store_id=store.id,offer_id='A',external_task_id='10',status='failed',idempotency_key='scan-child')
        session.add(c);session.flush()
        payload={'kind':'listing_status','store_id':store.id,'targets':[{'child_id':c.id,'offer_id':'A','revision':scan.revision(c)}]}
        j=Job(type='catalog_ozon_options',idempotency_key='scan-job',status='running',payload_json=dumps(payload),attempts=1,lease_owner='pc',lease_expires_at=utc_now()+timedelta(minutes=5))
        session.add(j);session.flush();session.add(JobStep(job_id=j.id,key='read',name='scan',position=1));session.commit()
        monkeypatch.setattr(routes,'local_job',lambda *a:j)
        result={'store_id':store.id,'kind':'listing_status','items':[ready()]}
        done=routes.local_complete('pc',j.id,WorkerJobResult(result=result),Obj(),session,1)
        assert done['status']=='completed' and c.status=='stock_pending'
        assert loads(j.external_ref,{})=={'stock_scan_summary':{'ready':1}}
        assert loads(j.steps[0].details_json,{})==result
        assert routes.local_complete('pc',j.id,WorkerJobResult(result=result),Obj(),session,1)['status']=='completed'
        with pytest.raises(ConflictError):routes.local_complete('pc',j.id,WorkerJobResult(result=result|{'items':[]}),Obj(),session,1)
