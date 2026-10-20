from datetime import timedelta
from erp.core.database import SessionLocal
from erp.models import Job, utc_now
import pytest


def test_overview_filters_counts_search_and_priority(client):
    with SessionLocal() as db:
        old=utc_now()-timedelta(days=10)
        entries=[Job(type='pod_order_sync',status='running',created_at=old,updated_at=old),
                 Job(type='pod_purchase_submit',status='action_required'),
                 Job(type='pod_order_sync',status='failed',payload_json='{"sku":"TASK-SKU-99"}'),
                 Job(type='pod_order_sync',status='paused'),
                 Job(type='pod_order_sync',status='queued'),
                 Job(type='pod_order_sync',status='completed'),
                 Job(type='catalog_ozon_prepare',status='failed')]
        for index,job in enumerate(entries):job.idempotency_key=f'overview-{index}'
        db.add_all(entries);db.commit()
    data=client.get('/api/v1/jobs?module=orders&status=unfinished&page_size=2').json()
    assert data['total']==5
    assert [r['status'] for r in data['items']]==['running','action_required']
    assert data['status_counts']['completed']==1
    active=client.get('/api/v1/jobs?module=orders&status=active').json()
    assert active['total']==1 and active['items'][0]['status']=='running'
    attention=client.get('/api/v1/jobs?module=orders&status=attention').json()
    assert attention['total']==3
    assert {r['status'] for r in attention['items']}=={'failed','paused','action_required'}
    search=client.get('/api/v1/jobs?search=TASK-SKU-99').json()
    assert search['total']==1 and search['items'][0]['status']=='failed'


def test_batch_grouping_before_pagination_preserves_finished_items_and_identity(client):
    import json
    with SessionLocal() as db:
        for index in range(100):
            db.add(Job(type='catalog_ozon_prepare',status='completed' if index<5 else 'action_required',
                idempotency_key=f'batch-{index}',target_worker_id='original',created_by='owner',
                payload_json=json.dumps({'batch_id':'batch-one','sku':f'SKU-{index}'})))
        db.add(Job(type='catalog_ozon_prepare',status='paused',idempotency_key='other-computer',
            target_worker_id='other',created_by='owner',payload_json=json.dumps({'batch_id':'batch-one'})))
        db.commit()
    data=client.get('/api/v1/jobs/overview?module=ozon&status=unfinished&page_size=1').json()
    assert data['total']==2
    row=data['items'][0]
    assert row['total_items']==100 and row['job_count']==100
    assert row['counts']['completed']==5 and row['counts']['action_required']==95
    assert row['progress']==5 and row['status']=='action_required'
    detail=client.get('/api/v1/jobs/overview/'+row['id']).json()
    assert len(detail['children'])==100
    assert client.get('/api/v1/jobs/overview?search=SKU-99').json()['items'][0]['total_items']==100
    assert client.get('/api/v1/jobs?module=ozon').json()['total']==101


def test_chunked_batch_progress_is_weighted_by_items(client):
    import json
    from erp.models import JobStep
    with SessionLocal() as db:
        job=Job(type='catalog_ozon_publication',status='action_required',idempotency_key='chunk',
            payload_json=json.dumps({'read_group_id':'read','batch_phase':'promotion','batch_entries':[{'child_id':str(i),'store_id':'shop'} for i in range(10)]}))
        db.add(job);db.flush()
        db.add_all([JobStep(job_id=job.id,key=str(i),name='SKU',position=i,status='completed' if i<4 else 'failed') for i in range(10)])
        db.commit()
    row=client.get('/api/v1/jobs/overview?store_id=shop').json()['items'][0]
    assert row['total_items']==10 and row['counts']['completed']==4
    assert row['counts']['action_required']==6 and row['progress']==40


def test_computer_filter_applies_to_totals_counts_and_complete_batch(client):
    import json
    with SessionLocal() as db:
        for worker, states in [('pc-a', ['running', 'completed']), ('pc-b', ['queued', 'failed'])]:
            for status in states:
                db.add(Job(type='catalog_ozon_prepare', status=status, target_worker_id=worker,
                           created_by='same-owner', idempotency_key=worker+status,
                           payload_json=json.dumps({'batch_id': 'same-batch'})))
        db.commit()
    own = client.get('/api/v1/jobs/overview?worker_id=pc-a&status=unfinished').json()
    assert own['total'] == 1 and own['status_counts'] == {'running': 1}
    row = own['items'][0]
    assert row['target_worker_id'] == 'pc-a' and row['total_items'] == 2
    assert row['counts']['completed'] == 1 and row['progress'] == 50
    detail = client.get('/api/v1/jobs/overview/' + row['id']).json()
    assert len(detail['children']) == 2
    raw = client.get('/api/v1/jobs?worker_id=pc-b&status=unfinished&page_size=1').json()
    assert raw['total'] == 2 and raw['status_counts'] == {'queued': 1, 'failed': 1}
    assert all(j['target_worker_id'] == 'pc-b' for j in raw['items'])
    assert client.get('/api/v1/jobs/overview').json()['total'] == 2


@pytest.mark.parametrize('kind,field',[
    ('amazon_prepare_codex','selection_group_id'),
    ('catalog_ozon_prepare','batch_id'),
    ('pod_wb_excel_export','codex_request_id'),
    ('pod_wb_api_publish','codex_request_id'),
])
def test_manual_codex_requests_group_without_merging_accounts_or_computers(client,kind,field):
    from erp.core.jsonutil import dumps
    with SessionLocal() as db:
        for index in range(3):
            db.add(Job(type=kind,status='completed' if index==0 else 'paused',
                idempotency_key=f'codex-{index}',created_by='owner',target_worker_id='pc-a',
                payload_json=dumps({'generation_provider':'codex','batch_id':f'child-{index}',
                                   field:'manual-request','product_ids':[f'p-{index}']})))
        for index,(owner,worker,request) in enumerate([
            ('owner','pc-a','another-request'),('other','pc-a','manual-request'),('owner','pc-b','manual-request')]):
            db.add(Job(type=kind,status='paused',idempotency_key=f'isolated-{index}',
                created_by=owner,target_worker_id=worker,payload_json=dumps({
                    'generation_provider':'codex','batch_id':f'isolated-{index}',field:request})))
        db.commit()
    rows=client.get('/api/v1/jobs/overview?job_type='+kind).json()['items']
    assert len(rows)==4
    combined=next(row for row in rows if row['job_count']==3)
    assert combined['counts']=={'completed':1,'paused':2} and combined['progress']==33
    assert len(client.get('/api/v1/jobs/overview/'+combined['id']).json()['children'])==3


def test_wb_recent_limit_does_not_cut_a_codex_request_in_half(client):
    from erp.core.jsonutil import dumps
    from erp.domains.pod.wb_export import list_exports
    with SessionLocal() as db:
        for index in range(3):
            db.add(Job(type='pod_wb_excel_export',status='paused',idempotency_key=f'sibling-{index}',
                created_by='owner',target_worker_id='pc-a',created_at=utc_now()-timedelta(minutes=index),
                payload_json=dumps({'generation_provider':'codex','codex_request_id':'common','batch_id':str(index)})))
        db.add(Job(type='pod_wb_excel_export',status='paused',idempotency_key='foreign-scope',
            created_by='other',target_worker_id='pc-b',created_at=utc_now()-timedelta(minutes=5),
            payload_json=dumps({'generation_provider':'codex','codex_request_id':'common'})))
        db.commit()
        assert len(list_exports(db,limit=1))==1
        complete=list_exports(db,limit=1,include_codex_siblings=True)
        assert len(complete)==3 and all(row['created_by']=='owner' for row in complete)
