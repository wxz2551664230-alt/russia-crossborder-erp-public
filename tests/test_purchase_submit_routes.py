from sqlalchemy import select
from erp.core.database import SessionLocal
from erp.domains.orders import api, desktop_purchase_submit as bridge, service
from erp.domains.pod.models import PodRemoteGalleryAsset
from test_personal_job_controls import team
from test_pod_orders import orders, posting
from test_purchase_submit_bridge import setup
from contextlib import closing
import pytest


def start(team,orders,monkeypatch):
    setup(orders,monkeypatch)
    monkeypatch.setattr(api,'manager',lambda _:orders)
    monkeypatch.setattr(service,'manager',lambda _:orders)
    clients,worker=team;owner=clients['owner']
    body={'target_type':'group','target_id':'group','confirmation':'CONFIRM_REAL_PURCHASE'}
    assert owner.post('/api/v1/orders/pod/purchase/submit',json=body).status_code==409
    assert owner.post('/api/v1/execution-devices/local/register',json={'id':worker,'name':'PC','purchase_submit_version':1}).status_code==200
    queued=owner.post('/api/v1/orders/pod/purchase/submit',json=body)
    assert queued.status_code==200,queued.text
    identity=queued.json()['job_id'];base=f'/api/v1/execution-devices/local/{worker}'
    assert owner.post(base+'/claim').json()['id']==identity
    return clients,identity,base+'/jobs/'+identity,{'X-Worker-Attempt':'1'}


def test_private_submit_routes_complete_only_from_shared_state(team,orders,monkeypatch):
    clients,identity,endpoint,headers=start(team,orders,monkeypatch);owner=clients['owner']
    for role in ('other','admin','viewer'):
        assert clients[role].post(endpoint+'/purchase-submit-snapshot',headers=headers).status_code==403
    assert owner.post(endpoint+'/purchase-submit-snapshot',headers={'X-Worker-Attempt':'2'}).status_code==409
    snap=owner.post(endpoint+'/purchase-submit-snapshot',headers=headers)
    assert snap.status_code==200 and snap.headers['cache-control']=='no-store'
    assert owner.post(endpoint+'/purchase-submit-result',headers=headers).status_code==409
    request={'method':'update_batch_group','args':['group'],'kwargs':{
        'submission_status':'submitted','hicustom_order_id':'supplier-id','fulfillment_gallery_codes':'["gallery-code"]'}}
    assert owner.post(endpoint+'/purchase-operation',headers=headers,json={**request,'args':['foreign']}).status_code==409
    updated=owner.post(endpoint+'/purchase-operation',headers=headers,json=request)
    assert updated.status_code==200,updated.text
    assert updated.headers['cache-control']=='no-store'
    result=owner.post(endpoint+'/purchase-submit-result',headers=headers)
    assert result.status_code==200,result.text
    assert owner.post(endpoint+'/complete',headers=headers,json={'result':{'submitted':999}}).status_code==409
    done=owner.post(endpoint+'/complete',headers=headers,json={'result':result.json()})
    assert done.status_code==200,done.text
    assert done.json()['status']=='completed'
    with SessionLocal() as db:
        item=db.scalar(select(PodRemoteGalleryAsset).where(PodRemoteGalleryAsset.gallery_code=='gallery-code'))
        assert item and item.protected and item.usage=='ordered' and not item.cleanup_candidate
    assert owner.post(endpoint+'/purchase-operation',headers=headers,json=request).status_code==409


def test_unknown_purchase_retry_requires_verified_shared_result(team,orders,monkeypatch):
    clients,identity,endpoint,headers=start(team,orders,monkeypatch);owner=clients['owner']
    assert owner.post(endpoint+'/purchase-submit-snapshot',headers=headers).status_code==200
    orders.update_batch_group('group',submission_status='unknown')
    failed=owner.post(endpoint+'/fail',headers=headers,json={'error_code':'purchase_write_outcome_unknown','error_message':'unknown'})
    assert failed.status_code==200,failed.text
    assert owner.post('/api/v1/jobs/'+identity+'/retry').status_code==409
    orders.update_batch_group('group',submission_status='submitted',hicustom_order_id='verified')
    assert owner.post('/api/v1/jobs/'+identity+'/retry').status_code==200


def test_page_resubmit_preserves_original_task_and_owner(team,orders,monkeypatch):
    clients,identity,endpoint,headers=start(team,orders,monkeypatch);owner=clients['owner']
    assert owner.post(endpoint+'/purchase-submit-snapshot',headers=headers).status_code==200
    body={'target_type':'group','target_id':'group','confirmation':'CONFIRM_REAL_PURCHASE'}
    assert owner.post('/api/v1/orders/pod/purchase/submit',json=body).json()['job_id']==identity
    assert clients['other'].post('/api/v1/orders/pod/purchase/submit',json=body).status_code==403
    assert owner.post(endpoint+'/fail',headers=headers,json={'error_code':'connection_error','error_message':'offline'}).status_code==200
    resumed=owner.post('/api/v1/orders/pod/purchase/submit',json=body)
    assert resumed.status_code==200,resumed.text
    assert resumed.json()['job_id']==identity


def test_stuck_purchase_group_exposes_only_owners_failed_original_task(team,orders,monkeypatch):
    clients,identity,endpoint,headers=start(team,orders,monkeypatch)
    owner=clients['owner']
    assert owner.post(endpoint+'/purchase-submit-snapshot',headers=headers).status_code==200
    orders.import_postings([posting()],store_id='A')
    key=orders.list_orders()['orders'][0]['order_key']
    with closing(orders._connect()) as db:
        db.execute('INSERT INTO batch_order_sources(group_id,posting_number,quantity) VALUES (?,?,?)',('group',key,1))
        db.commit()
    orders.update_batch_group('group',submission_status='preparing_product')
    assert owner.post(endpoint+'/fail',headers=headers,json={'error_code':'local_execution_failed','error_message':'offline'}).status_code==200
    path=f'/api/v1/orders/pod/{key}/purchase-groups'
    result=owner.get(path)
    assert result.status_code==200,result.text
    assert result.json()[0]['recovery_job']=={'id':identity,'status':'failed'}
    assert clients['other'].get(path).json()[0]['recovery_job'] is None
    assert owner.post(f'/api/v1/jobs/{identity}/retry').json()['status']=='queued'


def test_page_resubmit_blocks_unknown_external_write(team,orders,monkeypatch):
    clients,identity,endpoint,headers=start(team,orders,monkeypatch);owner=clients['owner']
    assert owner.post(endpoint+'/purchase-submit-snapshot',headers=headers).status_code==200
    assert owner.post(endpoint+'/purchase-write-intent',headers=headers).status_code==200
    orders.update_batch_group('group',submission_status='unknown')
    assert owner.post(endpoint+'/fail',headers=headers,json={'error_code':'purchase_write_outcome_unknown','error_message':'unknown'}).status_code==200
    body={'target_type':'group','target_id':'group','confirmation':'CONFIRM_REAL_PURCHASE'}
    assert owner.post('/api/v1/orders/pod/purchase/submit',json=body).status_code==409


@pytest.mark.parametrize('began_write',[False,True])
def test_cancel_only_releases_targets_before_external_write(team,orders,monkeypatch,began_write):
    clients,identity,endpoint,headers=start(team,orders,monkeypatch);owner=clients['owner']
    assert owner.post(endpoint+'/purchase-submit-snapshot',headers=headers).status_code==200
    if began_write:
        assert owner.post(endpoint+'/purchase-write-intent',headers=headers).status_code==200
    assert owner.post('/api/v1/jobs/'+identity+'/cancel').status_code==200
    assert owner.post(endpoint+'/heartbeat',headers=headers).status_code==409
    assert owner.post(endpoint+'/purchase-write-intent',headers=headers).status_code==409
    with closing(orders._connect()) as db:
        assert db.execute('SELECT count(*) FROM erp_purchase_reservations WHERE job_id=?',(identity,)).fetchone()[0]==int(began_write)
