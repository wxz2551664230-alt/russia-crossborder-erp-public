from erp.core.database import SessionLocal
from erp.core.security import vault
from erp.models import Store
from erp.domains.orders import desktop
from test_personal_job_controls import team
from test_pod_orders import orders, posting


def test_order_sync_device_capability_lease_and_private_return(team,orders,monkeypatch):
    monkeypatch.setattr(desktop,'manager',lambda _:orders)
    clients,worker=team;owner=clients['owner']
    with SessionLocal() as db:
        store=Store(platform='ozon',name='test',enabled=True,external_account_id='test-client',secrets_encrypted=vault.encrypt({'api_key':'private-token'}))
        db.add(store);db.commit();store_id=store.id
    body={'store_id':store_id,'days':3}
    assert owner.post('/api/v1/orders/pod/sync',json=body).status_code==409
    registered=owner.post('/api/v1/execution-devices/local/register',json={'id':worker,'name':'PC','order_read_version':1})
    assert 'pod_order_sync' in registered.json()['capabilities']
    queued=owner.post('/api/v1/orders/pod/sync',json=body)
    assert queued.status_code==200,queued.text
    identity=queued.json()['job_id'];base=f'/api/v1/execution-devices/local/{worker}'
    assert owner.post(base+'/claim').json()['id']==identity
    endpoint=base+'/jobs/'+identity;headers={'X-Worker-Attempt':'1'}
    for name in ('other','admin','viewer'):
        assert clients[name].post(endpoint+'/order-snapshot',headers=headers).status_code==403
    assert owner.post(endpoint+'/order-snapshot',headers={'X-Worker-Attempt':'2'}).status_code==409
    snapshot=owner.post(endpoint+'/order-snapshot',headers=headers)
    assert snapshot.status_code==200 and snapshot.headers['cache-control']=='no-store'
    assert snapshot.json()['config']['ozon_api_key']=='private-token'
    assert owner.post(endpoint+'/complete',headers=headers,json={'result':{}}).status_code==409
    data={'index':0,'postings':[posting()],'final':True}
    received=owner.post(endpoint+'/order-batch',headers=headers,json=data)
    assert received.status_code==200,received.text
    assert owner.post(endpoint+'/order-batch',headers=headers,json=data).json()==received.json()
    image_result=owner.post(endpoint+'/order-images',headers=headers,json={'images':{}})
    assert image_result.status_code==200,image_result.text
    result={'sync':received.json(),'images':image_result.json()}
    done=owner.post(endpoint+'/complete',headers=headers,json={'result':result})
    assert done.status_code==200,done.text
    assert done.json()['status']=='completed'
    assert owner.post(endpoint+'/complete',headers=headers,json={'result':result}).status_code==200
    assert owner.post(endpoint+'/order-batch',headers=headers,json=data).status_code==409


def test_scoped_sync_rejects_old_client_before_snapshot(team,orders,monkeypatch):
    monkeypatch.setattr(desktop,'manager',lambda _:orders)
    clients,worker=team;owner=clients['owner']
    with SessionLocal() as db:
        store=Store(platform='ozon',name='scope',enabled=True,external_account_id='test-client',secrets_encrypted=vault.encrypt({'api_key':'test'}))
        db.add(store);db.commit();store_id=store.id
    owner.post('/api/v1/execution-devices/local/register',json={'id':worker,'name':'PC','order_read_version':1})
    queued=owner.post('/api/v1/orders/pod/sync',json={'store_id':store_id,'days':3,'scope_version':1,'sync_images':False})
    assert queued.status_code==200,queued.text
    base=f'/api/v1/execution-devices/local/{worker}'
    identity=queued.json()['job_id']
    assert owner.post(base+'/claim').json()['id']==identity
    endpoint=base+'/jobs/'+identity+'/order-snapshot'
    assert owner.post(endpoint,headers={'X-Worker-Attempt':'1'}).status_code==409
    response=owner.post(endpoint,headers={'X-Worker-Attempt':'1','X-Order-Scope-Version':'1'})
    assert response.status_code==200
    payload=response.json()['payload']
    assert payload['scope_version']==1 and not payload['sync_images']
    assert payload['date_from'] and payload['date_to']
