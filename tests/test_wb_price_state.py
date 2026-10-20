import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps
from erp.models import Job, WbPublicationItem, utc_now
from erp.domains.pod.wb_export import list_publications
from erp.domains.pod.wb_price_state import release_interrupted
from test_wb_shop_sync import shop
from test_personal_job_controls import team
from test_wb_shop_sync import card, finish


def seed(shop, status='running', error='', *, n=0, row_status='price_processing'):
    clients, worker, store, _, variants = shop
    user = clients['owner'].get('/api/v1/auth/session').json()['user']['id']
    with SessionLocal() as db:
        source = Job(type='pod_wb_api_publish', status='completed', idempotency_key=uuid.uuid4().hex)
        db.add(source); db.flush()
        variant, product = variants[n]
        row = WbPublicationItem(store_id=store, product_id=product, variant_id=variant, offer_id='W-'+str(n),
                                job_id=source.id, mode='api', status=row_status, external_product_id=str(n+100))
        db.add(row); db.flush()
        source.payload_json = dumps({'rows': [{'id': row.id, 'price': 124, 'discount': 30}]})
        job = Job(type='pod_wb_price', status=status, created_by=user, target_worker_id=worker,
                  attempts=1, max_attempts=1, idempotency_key=uuid.uuid4().hex,
                  payload_json=dumps({'store_id': store, 'ledger_ids': [row.id]}),
                  error_code=error, error_message='original operation interrupted',
                  lease_owner=worker if status=='running' else '',
                  lease_expires_at=utc_now()-timedelta(minutes=2) if status=='running' else None)
        db.add(job); db.commit()
        return row.id, source.id, job.id


def test_expired_price_job_moves_processing_rows_to_verification_without_retry(shop):
    clients, worker, *_ = shop
    row_id, source_id, job_id = seed(shop)
    response = clients['owner'].post(f'/api/v1/execution-devices/local/{worker}/claim')
    assert response.status_code == 200 and response.json() is None
    with SessionLocal() as db:
        job, row = db.get(Job, job_id), db.get(WbPublicationItem, row_id)
        assert job.status == 'action_required' and job.error_code == 'wb_price_verification_pending'
        assert job.attempts == job.max_attempts == 1
        assert row.status == 'price_failed' and '先只读核对' in row.last_error
        assert row.job_id == source_id and row.external_product_id == '100'
        assert release_interrupted(db, job) == []
    pending = clients['owner'].get('/api/v1/pod/wb/publications?stage=pending_price').json()
    assert pending['total'] == 0
    exceptions = clients['owner'].get('/api/v1/pod/wb/publications?stage=exceptions').json()['items']
    assert exceptions[0]['operation_job_id'] == job_id
    assert exceptions[0]['price'] == 124
    # The interrupted operation must not silently become a new price upload.
    result = clients['owner'].post('/api/v1/pod/wb/publications/price',
                                  json={'ledger_ids': [row_id], 'price': 124, 'discount_percent': 30})
    assert result.status_code == 409 and '先只读核对' in result.text


@pytest.mark.parametrize('status', ['queued', 'running', 'paused', 'retrying', 'action_required', 'completed'])
def test_nonfailed_operations_are_not_released(shop, status):
    row_id, _, job_id = seed(shop, status=status)
    with SessionLocal() as db:
        assert release_interrupted(db, db.get(Job, job_id)) == []
        assert db.get(WbPublicationItem, row_id).status == 'price_processing'


def test_old_failure_does_not_override_newer_operation_or_other_store(shop):
    row_id, _, old_id = seed(shop, status='failed')
    with SessionLocal() as db:
        old = db.get(Job, old_id)
        new = Job(type=old.type, status='running', payload_json=old.payload_json,
                  idempotency_key=uuid.uuid4().hex, created_at=utc_now()+timedelta(seconds=1))
        db.add(new); db.commit()
        assert release_interrupted(db, old) == []
        assert db.get(WbPublicationItem, row_id).status == 'price_processing'


def test_pending_items_sorted_before_processing_and_real_price_job_link(shop):
    processing, _, phase = seed(shop, status='queued', n=0)
    pending, _, _ = seed(shop, status='completed', n=1, row_status='pending_price')
    with SessionLocal() as db:
        row = db.get(WbPublicationItem, processing); row.updated_at=utc_now()+timedelta(seconds=1); db.commit()
        result = list_publications(db, 'pending_price', page_size=1)
        assert result['total'] == 2 and result['items'][0]['id'] == pending
        next_page = list_publications(db, 'pending_price', page=2, page_size=1)['items'][0]
        assert next_page['id'] == processing and next_page['operation_job_id'] == phase
        assert '等待原电脑' in next_page['price_submit_blocked_reason']


def test_confirmed_upload_remains_blocked_from_duplicate_submission(shop):
    row_id, _, job_id = seed(shop, status='action_required', row_status='pending_price')
    with SessionLocal() as db:
        row=db.get(WbPublicationItem,row_id);row.last_error='WB 上传编号 123 已保存';db.commit()
        item=list_publications(db,'pending_price')['items'][0]
        assert item['operation_job_id']==job_id
        assert '不能重复提交' in item['price_submit_blocked_reason']


def test_explicit_rate_limit_stays_pending_for_manual_submission(shop):
    row_id, _, job_id = seed(shop, status='failed', error='wb_price_rate_limited')
    with SessionLocal() as db:
        assert release_interrupted(db,db.get(Job,job_id))==[row_id]
        assert db.get(WbPublicationItem,row_id).status=='pending_price'


def forced_read(shop, warnings=None, force=True):
    clients, worker, store, *_ = shop
    client=clients['owner']
    response=client.post(f'/api/v1/pod/wb/stores/{store}/sync?refresh={str(force).lower()}')
    assert response.status_code==202,response.text
    jid=response.json()['job_id'];base=f'/api/v1/execution-devices/local/{worker}'
    claimed=client.post(base+'/claim').json();assert claimed['id']==jid
    observed=card(1);observed['nmID']=100
    finish((client,jid,base+'/jobs/'+jid,{'X-Worker-Attempt':str(claimed['attempts'])}),[observed],warnings)
    return jid


def test_complete_forced_refresh_releases_old_task_and_classifies_from_receipt(shop):
    row_id, source_id, old_id=seed(shop,status='queued')
    refresh_id=forced_read(shop)
    with SessionLocal() as db:
        from erp.core.jsonutil import loads
        row,old=db.get(WbPublicationItem,row_id),db.get(Job,old_id)
        assert old.status=='cancelled' and old.lease_owner==''
        assert loads(old.payload_json,{})['wb_shop_sync_released_by']==refresh_id
        assert row.status=='pending_price' and row.job_id==source_id
    result=shop[0]['owner'].post(f'/api/v1/jobs/{old_id}/retry')
    assert result.status_code==409 and '强制刷新释放' in result.text
    result=shop[0]['owner'].get(f'/api/v1/pod/wb/stores/{shop[2]}/sync-status').json()
    assert result['summary']['released_tasks']==1


@pytest.mark.parametrize('force,warnings',[(False,None),(True,['部分读取失败'])])
def test_normal_or_incomplete_refresh_keeps_old_operation(shop,force,warnings):
    row_id,_,old_id=seed(shop,status='queued')
    forced_read(shop,warnings,force)
    with SessionLocal() as db:
        assert db.get(Job,old_id).status=='queued'
        assert db.get(WbPublicationItem,row_id).status=='price_processing'


def test_forced_refresh_does_not_release_valid_running_writer(shop):
    row_id,_,old_id=seed(shop,status='running')
    with SessionLocal() as db:
        db.get(Job,old_id).lease_expires_at=utc_now()+timedelta(hours=1);db.commit()
    forced_read(shop)
    with SessionLocal() as db:
        assert db.get(Job,old_id).status=='running'
        assert db.get(WbPublicationItem,row_id).status=='price_processing'


@pytest.mark.parametrize('protected',['other_owner','newer_task'])
def test_forced_refresh_does_not_release_other_owner_or_newer_task(shop,protected):
    row_id,_,old_id=seed(shop,status='queued')
    with SessionLocal() as db:
        job=db.get(Job,old_id)
        if protected=='other_owner':
            job.created_by=shop[0]['other'].get('/api/v1/auth/session').json()['user']['id']
        else:
            job.created_at=utc_now()+timedelta(days=1)
        db.commit()
    forced_read(shop)
    with SessionLocal() as db:
        assert db.get(Job,old_id).status=='queued'
        assert db.get(WbPublicationItem,row_id).status=='price_processing'
