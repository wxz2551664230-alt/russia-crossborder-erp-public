from datetime import timedelta
import hashlib
import uuid

import pytest
from sqlalchemy import event, select

from erp.core.database import SessionLocal, engine
from erp.core.jsonutil import dumps, loads
from erp.domains.pod.wb_price_recovery import freeze_plan, price_rows
from erp.domains.pod.wb_export import _wb_store_snapshot
from erp.models import Asset, CatalogProduct, CatalogVariant, Job, JobArtifact, JobEvent, WbPublicationItem, utc_now
from test_personal_job_controls import team
from test_wb_shop_sync import shop, card, finish
from test_wb_price_state import seed


def operation(shop):
    clients, worker, store, *_ = shop
    client = clients['owner']
    client.post(f'/api/v1/execution-devices/local/register', json={
        'id': worker, 'name': 'original PC', 'wb_price_version': 1, 'wb_history_version': 3})
    rid, source, jid = seed(shop)
    with SessionLocal() as db:
        job = db.get(Job, jid)
        job.payload_json = dumps({**loads(job.payload_json, {}), 'price': 124, 'discount_percent': 30})
        job.config_snapshot_json = _wb_store_snapshot(db, store)[1]
        freeze_plan(db, job)
        db.commit()
    base = f'/api/v1/execution-devices/local/{worker}'
    return client, rid, jid, base, base+'/jobs/'+jid


def expire_and_claim(shop):
    client, rid, jid, base, path = operation(shop)
    # First poll schedules one delayed read. It must not dispatch a write or a new task.
    assert client.post(base+'/claim').json() is None
    with SessionLocal() as db:
        job = db.get(Job, jid)
        assert job.status == 'retrying' and job.max_attempts == job.attempts == 1
        assert loads(job.payload_json, {})['verify_only'] is True
        assert job.error_code == 'wb_price_recovery_queued'
        job.heartbeat_at = utc_now()-timedelta(seconds=61)
        db.commit()
    claimed = client.post(base+'/claim').json()
    assert claimed['id'] == jid and claimed['attempts'] == 2
    assert claimed['target_worker_id'] == shop[1]
    assert len(client.get('/api/v1/jobs?job_type=pod_wb_price').json()['items']) == 1
    return client, rid, jid, base, path


def test_original_task_recovers_readonly_and_completes_after_exact_receipt(shop):
    client, rid, jid, base, path = expire_and_claim(shop)
    stale = {'result': {'verified': True, 'verified_existing': True, 'upload_id': 0, 'rows': []}}
    assert client.post(path+'/complete', headers={'X-Worker-Attempt':'1'}, json=stale).status_code == 409
    snapshot = client.post(path+'/wb-price-snapshot', headers={'X-Worker-Attempt':'2'}).json()
    assert snapshot['verify_only'] is True
    assert snapshot['rows'] == [{'id':rid, 'nm_id':100, 'price':124, 'discount':30}]
    result = {**stale['result'], 'rows': snapshot['rows']}
    response = client.post(path+'/complete', headers={'X-Worker-Attempt':'2'}, json={'result':result})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'completed'
    with SessionLocal() as db:
        assert db.get(WbPublicationItem,rid).status == 'completed'
        job = db.get(Job,jid)
        assert not job.error_code and loads(job.payload_json,{})['wb_price_recovery']['state']=='verified'
        assert any('执行连接中断' in e.message for e in db.scalars(select(JobEvent).where(JobEvent.job_id==jid)))
    overview = client.get('/api/v1/jobs/overview?job_type=pod_wb_price').json()['items'][0]
    assert overview['status']=='completed' and overview['wb_price_recovery']['state']=='verified'


@pytest.mark.parametrize('failure',['lease','mismatch','rate_limit'])
def test_failed_verification_stops_without_any_automatic_repetition(shop,failure):
    client,rid,jid,base,path=expire_and_claim(shop)
    if failure=='lease':
        with SessionLocal() as db:
            db.get(Job,jid).lease_expires_at=utc_now()-timedelta(seconds=1);db.commit()
        assert client.post(base+'/claim').json() is None
    else:
        response=client.post(path+'/fail',headers={'X-Worker-Attempt':'2'},json={
            'error_code':'wb_price_outcome_unknown',
            'error_message':'WB 实际价格或折扣与计划不一致；本次未写入' if failure=='mismatch' else 'WB 价格只读核对失败，HTTP 429'})
        assert response.status_code==200,response.text
    with SessionLocal() as db:
        job=db.get(Job,jid)
        assert job.status=='action_required' and job.attempts==2
        assert loads(job.payload_json,{})['verify_only'] is True
        assert db.get(WbPublicationItem,rid).status==('pending_price' if failure=='mismatch' else 'price_failed')
        if failure=='mismatch':assert loads(job.payload_json,{})['wb_price_recovery']['state']=='not_applied'
    assert client.post(base+'/claim').json() is None
    response=client.post('/api/v1/jobs/'+jid+'/retry')
    assert response.status_code==200,response.text
    assert response.json()['payload']['verify_only'] is True
    assert client.post(base+'/claim').json()['attempts']==3


def test_recovery_never_accepts_a_new_upload_receipt(shop):
    client,rid,jid,_,path=expire_and_claim(shop)
    result={'verified':True,'upload_id':777,'rows':[{'id':rid,'nm_id':100,'price':124,'discount':30}]}
    response=client.post(path+'/complete',headers={'X-Worker-Attempt':'2'},json={'result':result})
    assert response.status_code==409 and '只读' in response.text
    with SessionLocal() as db:
        assert db.get(Job,jid).status=='running'
        assert db.get(WbPublicationItem,rid).status=='price_processing'


def test_verified_mismatch_returns_to_manual_price_queue_without_uploading(shop):
    client,rid,jid,base,path=expire_and_claim(shop)
    response=client.post(path+'/fail',headers={'X-Worker-Attempt':'2'},json={
        'error_code':'wb_price_outcome_unknown',
        'error_message':'WB 实际价格或折扣与计划不一致；本次未写入'})
    assert response.json()['error_code']=='wb_price_manual_submit_required'
    pending=client.get('/api/v1/pod/wb/publications?stage=pending_price').json()['items']
    assert pending[0]['id']==rid and not pending[0]['price_submit_blocked_reason']
    assert client.post(base+'/claim').json() is None
    # A new write is possible only after this explicit human submit action.
    created=client.post('/api/v1/pod/wb/publications/price',json={
        'ledger_ids':[rid],'price':124,'discount_percent':30})
    assert created.status_code==202,created.text
    assert created.json()['job']['id']!=jid


def test_explicit_wb_rejection_does_not_schedule_automatic_read(shop):
    client,rid,jid,base,path=operation(shop)
    with SessionLocal() as db:
        db.get(Job,jid).lease_expires_at=utc_now()+timedelta(seconds=45);db.commit()
    response=client.post(path+'/fail',headers={'X-Worker-Attempt':'1'},json={
        'error_code':'wb_price_rate_limited','error_message':'WB 429，人工提交'})
    assert response.json()['status']=='failed'
    assert client.post(base+'/claim').json() is None
    with SessionLocal() as db:
        assert 'wb_price_recovery' not in loads(db.get(Job,jid).payload_json,{})
        assert db.get(WbPublicationItem,rid).status=='pending_price'


@pytest.mark.parametrize('control',['pause','cancel'])
def test_user_control_prevents_automatic_recovery(shop,control):
    client,_,jid,base,_=operation(shop)
    client.post('/api/v1/jobs/'+jid+'/'+control)
    assert client.post(base+'/claim').json() is None
    with SessionLocal() as db:
        assert db.get(Job,jid).status==('paused' if control=='pause' else 'cancelled')
        assert 'wb_price_recovery' not in loads(db.get(Job,jid).payload_json,{})


@pytest.mark.parametrize('currency',['CNY',''])
def test_complete_sync_resolves_old_task_and_retains_original_interruption(shop,currency):
    client,rid,jid,base,_=operation(shop)
    with SessionLocal() as db:
        old=db.get(Job,jid);old.status='failed';old.error_code='worker_lease_expired';old.lease_owner='';old.lease_expires_at=None
        db.commit()
    response=client.post(f'/api/v1/pod/wb/stores/{shop[2]}/sync?refresh=true')
    sync_id=response.json()['job_id'];claimed=client.post(base+'/claim').json()
    assert claimed['id']==sync_id
    observed=card(1,price=124);observed['nmID']=100
    observed['price']['currency']=currency
    finish((client,sync_id,base+'/jobs/'+sync_id,{'X-Worker-Attempt':str(claimed['attempts'])}),[observed])
    with SessionLocal() as db:
        old=db.get(Job,jid)
        assert old.status=='completed' and old.progress==100
        recovery=loads(old.payload_json,{})['wb_price_recovery']
        assert recovery['original_error_code']=='worker_lease_expired' and recovery['reference']==sync_id
        assert db.get(WbPublicationItem,rid).status=='completed'
    assert client.get(f'/api/v1/pod/wb/stores/{shop[2]}/sync-status').json()['summary']['resolved_price_tasks']==1
    assert client.post('/api/v1/jobs/'+jid+'/retry').status_code==409


@pytest.mark.parametrize('fault',['price','discount','currency','missing','partial','owner','unknown'])
def test_sync_does_not_claim_success_without_full_exact_original_prices(shop,fault):
    client,rid,jid,base,_=operation(shop)
    with SessionLocal() as db:
        old=db.get(Job,jid);old.status='failed';old.error_code='worker_lease_expired';old.lease_owner='';old.lease_expires_at=None
        if fault=='owner':old.created_by=shop[0]['other'].get('/api/v1/auth/session').json()['user']['id']
        db.commit()
    response=client.post(f'/api/v1/pod/wb/stores/{shop[2]}/sync?refresh=true')
    sync_id=response.json()['job_id'];claimed=client.post(base+'/claim').json()
    observed=card(1,price=124);observed['nmID']=100
    if fault=='price':observed['price']['price']=125
    if fault=='discount':observed['price']['discount']=29
    if fault=='currency':observed['price']['currency']='RUB'
    if fault=='unknown':observed['price']={'state':'unknown','price':None,'discount':None,'currency':''}
    finish((client,sync_id,base+'/jobs/'+sync_id,{'X-Worker-Attempt':str(claimed['attempts'])}),
           [] if fault=='missing' else [observed],['partial read'] if fault=='partial' else None)
    with SessionLocal() as db:assert db.get(Job,jid).status!='completed'


def test_batch_plan_does_not_issue_queries_per_product_or_recalculate_frozen_prices(shop):
    _,rid,jid,_,_=operation(shop)
    with SessionLocal() as db:
        job=db.get(Job,jid);payload=loads(job.payload_json,{})
        payload.pop('price_plan');payload['price']=None
        first=db.get(WbPublicationItem,rid)
        db.get(Job,first.job_id).payload_json=dumps({'pricing':{'manual_price_rub':124}})
        for n in range(1,201):
            product=CatalogProduct(sku='BATCH-'+str(n),source_type='pod',name='batch')
            db.add(product);db.flush()
            variant=CatalogVariant(product_id=product.id,sku=product.sku+'-01');db.add(variant);db.flush()
            row=WbPublicationItem(store_id=first.store_id,product_id=product.id,variant_id=variant.id,
                offer_id=variant.sku,job_id=first.job_id,mode='api',status='price_processing',external_product_id=str(100+n))
            db.add(row);db.flush();payload['ledger_ids'].append(row.id)
        job.payload_json=dumps(payload);db.commit()
        calls=[]
        def count(conn,cursor,statement,params,context,many):
            if statement.lstrip().upper().startswith('SELECT'):calls.append(statement)
        event.listen(engine,'before_cursor_execute',count)
        try:plan=freeze_plan(db,job)
        finally:event.remove(engine,'before_cursor_execute',count)
        assert len(plan)==201 and len(calls)<=5
        db.get(Job,first.job_id).payload_json=dumps({'pricing':{'manual_price_rub':999}})
        assert price_rows(db,job)==plan


def test_newer_price_operation_blocks_old_snapshot_and_result(shop):
    client,rid,jid,_,path=operation(shop)
    with SessionLocal() as db:
        old=db.get(Job,jid);old.lease_expires_at=utc_now()+timedelta(seconds=45)
        newer=Job(type='pod_wb_price',status='queued',payload_json=old.payload_json,
                  idempotency_key=uuid.uuid4().hex,created_at=utc_now()+timedelta(seconds=1))
        db.add(newer);db.commit()
    response=client.post(path+'/wb-price-snapshot',headers={'X-Worker-Attempt':'1'})
    assert response.status_code==409 and '较新' in response.text


def test_manual_task_uses_new_price_for_sync_classification_instead_of_old_publish_price(shop):
    client,rid,jid,base,_=operation(shop)
    with SessionLocal() as db:
        row=db.get(WbPublicationItem,rid)
        db.get(Job,row.job_id).payload_json=dumps({'rows':[{'id':rid,'price':999,'discount':20}]})
        old=db.get(Job,jid);old.status='failed';old.error_code='worker_lease_expired';old.lease_owner='';old.lease_expires_at=None
        db.commit()
    sync_id=client.post(f'/api/v1/pod/wb/stores/{shop[2]}/sync?refresh=true').json()['job_id']
    claimed=client.post(base+'/claim').json()
    observed=card(1,price=124);observed['nmID']=100
    finish((client,sync_id,base+'/jobs/'+sync_id,{'X-Worker-Attempt':str(claimed['attempts'])}),[observed])
    with SessionLocal() as db:
        assert db.get(Job,jid).status=='completed'
        assert db.get(WbPublicationItem,rid).status=='completed'


@pytest.mark.parametrize('fault',['','hash','missing'])
def test_legacy_plan_uses_original_export_receipt_not_mutable_product_cost(shop,fault,tmp_path,monkeypatch):
    from erp.core.errors import ConflictError
    from erp.domains.pod import wb_export
    monkeypatch.setattr(wb_export,'_roots',lambda:(tmp_path,tmp_path))
    _,rid,jid,_,_=operation(shop)
    with SessionLocal() as db:
        job=db.get(Job,jid);payload=loads(job.payload_json,{})
        payload.pop('price_plan');payload['price']=None;job.payload_json=dumps(payload)
        row=db.get(WbPublicationItem,rid)
        db.get(Job,row.job_id).payload_json=dumps({'pricing':{'mode':'profit'}})
        # This current cost must never be used to reconstruct historical success.
        db.get(CatalogProduct,row.product_id).purchase_price_cny=9999
        data=dumps({'payload':{'store_id':shop[2]},'groups':[{'variants':[{'vendorCode':row.offer_id,'price':124}]}]}).encode()
        path=tmp_path/'package.json'
        if fault!='missing':path.write_bytes(data if fault!='hash' else b'changed')
        asset=Asset(kind='file',name='package.json',source='pod_wb_export',local_path=str(path),
                    size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        db.add(asset);db.flush();db.add(JobArtifact(job_id=row.job_id,asset_id=asset.id,role='wb_package'));db.commit()
        if fault:
            with pytest.raises(ConflictError):price_rows(db,job,immutable_only=True)
        else:
            assert price_rows(db,job,immutable_only=True)==[{'id':rid,'nm_id':100,'price':124,'discount':30}]


def test_sync_preserves_manual_cancel_after_interruption(shop):
    client,rid,jid,base,_=expire_and_claim(shop)
    client.post('/api/v1/jobs/'+jid+'/cancel')
    with SessionLocal() as db:
        old=db.get(Job,jid);old.lease_expires_at=utc_now()-timedelta(seconds=1);db.commit()
    assert client.post(base+'/claim').json() is None
    sync_id=client.post(f'/api/v1/pod/wb/stores/{shop[2]}/sync?refresh=true').json()['job_id']
    claimed=client.post(base+'/claim').json()
    observed=card(1,price=124);observed['nmID']=100
    finish((client,sync_id,base+'/jobs/'+sync_id,{'X-Worker-Attempt':str(claimed['attempts'])}),[observed])
    with SessionLocal() as db:assert db.get(Job,jid).status=='cancelled'
