from datetime import timedelta
from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps
from erp.models import Job, utc_now


def add(db, job_type, status, **kwargs):
    job = Job(type=job_type, status=status, config_snapshot_json='{}',
              idempotency_key=kwargs.pop('key', job_type + status), **kwargs)
    db.add(job)
    return job


def test_unfinished_old_tasks_are_not_hidden_by_200_recent_completed(client):
    with SessionLocal() as db:
        old = add(db, 'pod_wb_price', 'failed', created_at=utc_now()-timedelta(days=2))
        add(db, 'pod_wb_stock', 'queued')
        add(db, 'pod_order_sync', 'paused')
        for n in range(205): add(db, 'pod_native_composition', 'completed', key=f'done-{n}')
        db.commit(); identity = old.id
    result = client.get('/api/v1/jobs', params={'module':'wb','status':'unfinished','page_size':1}).json()
    assert result['total'] == 2
    assert result['status_counts'] == {'failed':1, 'queued':1}
    second = client.get('/api/v1/jobs', params={'module':'wb','status':'unfinished','page_size':1,'page':2}).json()
    assert second['items'][0]['id'] == identity
    assert second['items'][0]['business_module'] == 'wb'
    assert client.get('/api/v1/jobs/'+identity).json()['id'] == identity


def test_module_store_search_and_counts_are_filtered_before_pagination(client):
    with SessionLocal() as db:
        one = add(db,'catalog_ozon_followup','running',payload_json=dumps({'store_id':'shop-one'}), error_message='unique marker')
        add(db,'pod_wb_price','running',payload_json=dumps({'store_id':'shop-one'}))
        add(db,'catalog_ozon_prepare','failed',payload_json=dumps({'store_id':'shop-two'}))
        add(db,'podXwbXprice','failed')
        db.commit(); identity=one.id
    result = client.get('/api/v1/jobs',params={'module':'ozon','store_id':'shop-one','search':'unique marker'}).json()
    assert result['total']==1 and result['items'][0]['id']==identity
    assert result['status_counts']=={'running':1}
    other=client.get('/api/v1/jobs',params={'module':'other'}).json()
    assert other['total']==1 and other['items'][0]['type']=='podXwbXprice'
    assert client.get('/api/v1/jobs',params={'module':'invalid'}).status_code==400
