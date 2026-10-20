import base64
import gzip
import hashlib
import json
import threading
from pathlib import Path

import pytest
from erp.core.database import SessionLocal
from erp.core.security import vault
from erp.core.jsonutil import dumps, loads
from erp.domains.pod.models import PodSupplierCatalogSnapshot
from erp.domains.pod.supplier_catalog import catalog_view, _save_catalog
from erp.domains.pod.us_supplier_catalog import read
from erp.models import IntegrationConnection, Job
from test_personal_job_controls import team


def prepare(clients, worker, version=2):
    owner = clients['owner']
    owner.headers['X-ERP-Client-Version']='0.6.83'
    assert owner.post('/api/v1/execution-devices/local/register', json={'id':worker,'name':'PC','supplier_catalog_version':version}).status_code == 200
    with SessionLocal() as db:
        db.add(IntegrationConnection(provider='s2b',name='test',base_url='https://supplier.example.test',
            enabled=True,secrets_encrypted=vault.encrypt({'app_key':'private','app_secret':'private-secret'})))
        _save_catalog(db, {'products':[{'id':9,'supplier':'hicustom','cn_name':'domestic'}], 'categories':[], 'synced_at':'old'})
    return owner


def body(value):
    packed=gzip.compress(json.dumps(value).encode(),mtime=0)
    return {'content':base64.b64encode(packed).decode(),'sha256':hashlib.sha256(packed).hexdigest()}


def result(country='US'):
    return {'market':'US','kind':'sync','products':[{'id':7,'source_id':'7','catalog_key':'s2b:7','supplier':'s2b','produce_country':country,'name':'US product','sizes':[{'id':2,'name':'Large'}]}], 'categories':[]}


def submit(owner, worker):
    created=owner.post('/api/v1/pod/us-catalog/sync');assert created.status_code==202,created.text
    job=created.json()['job']
    base=f'/api/v1/execution-devices/local/{worker}'
    assert owner.post(base+'/claim').json()['id']==job['id']
    return job,base+'/jobs/'+job['id'],{'X-Worker-Attempt':'1'}


def test_us_sync_isolated_cached_replayed_and_owner_bound(team,monkeypatch):
    clients,worker=team;owner=prepare(clients,worker)
    job,endpoint,headers=submit(owner,worker)
    assert job['target_worker_id']==worker and job['max_attempts']==1
    duplicate=owner.post('/api/v1/pod/us-catalog/sync').json()
    assert duplicate['reused'] and duplicate['job']['id']==job['id']
    assert clients['other'].get(endpoint+'/supplier-catalog-snapshot',headers=headers).status_code==403
    source=owner.get(endpoint+'/supplier-catalog-snapshot',headers=headers).json()
    assert set(source['sources'])=={'s2b'} and source['request']['market']=='US'
    assert owner.post(endpoint+'/complete',json={'result':{}},headers=headers).status_code==409
    value=body(result())
    assert owner.post(endpoint+'/supplier-catalog-result',json=value,headers={'X-Worker-Attempt':'2'}).status_code==409
    receipt=owner.post(endpoint+'/supplier-catalog-result',json=value,headers=headers)
    assert receipt.status_code==200,receipt.text
    assert owner.post(endpoint+'/supplier-catalog-result',json=value,headers=headers).json()==receipt.json()
    assert owner.post(endpoint+'/complete',json={'result':receipt.json()},headers=headers).status_code==200
    from erp.domains.pod.integrations.s2b import S2BClient
    monkeypatch.setattr(S2BClient,'_request',lambda *a,**kw:pytest.fail('Cached GET must not call supplier'))
    assert owner.get('/api/v1/pod/us-catalog').json()['items'][0]['source_id']=='7'
    assert owner.get('/api/v1/pod/us-catalog/products/s2b:7').json()['detail'] is None
    with SessionLocal() as db:
        assert catalog_view(db)['products'][0]['cn_name']=='domestic'
        _save_catalog(db,{'products':[{'id':10,'cn_name':'next domestic'}], 'synced_at':'new'})
        assert read(db)[1]['products'][0]['id']==7


@pytest.mark.parametrize('invalid',['country','duplicate','market','secret'])
def test_us_result_rejects_unverified_or_mismatched_facts(team,invalid):
    clients,worker=team;owner=prepare(clients,worker)
    job,endpoint,headers=submit(owner,worker)
    value=result()
    if invalid=='country':value['products'][0]['produce_country']='CN'
    if invalid=='duplicate':value['products']*=2
    if invalid=='market':value['market']='CN'
    if invalid=='secret':value['products'][0]['secrets']={'app_secret':'private'}
    assert owner.post(endpoint+'/supplier-catalog-result',json=body(value),headers=headers).status_code==409
    with SessionLocal() as db:
        assert db.get(Job,job['id']).status=='running'
        assert read(db)[0] is None


def test_old_worker_cannot_receive_us_catalog(team):
    clients,worker=team;owner=prepare(clients,worker,version=1)
    assert owner.post('/api/v1/pod/us-catalog/sync').status_code==409
    with SessionLocal() as db:assert db.query(Job).filter_by(type='pod_supplier_us_catalog_sync').count()==0


def test_us_detail_on_demand_and_no_snapshot_duplication(team):
    clients,worker=team;owner=prepare(clients,worker)
    job,endpoint,headers=submit(owner,worker)
    receipt=owner.post(endpoint+'/supplier-catalog-result',json=body(result()),headers=headers).json()
    created=owner.post('/api/v1/pod/us-catalog/products/s2b:7/refresh')
    assert created.status_code==202,created.text
    job=created.json()['job']
    base=f'/api/v1/execution-devices/local/{worker}'
    assert owner.post(base+'/claim').json()['id']==job['id']
    detail={'market':'US','kind':'detail','product_key':'s2b:7', 'detail':{'id':7,'produce_country':'US','stock_info':[{'id':71,'price':'12.5'}]}}
    response=owner.post(base+'/jobs/'+job['id']+'/supplier-catalog-result',json=body(detail),headers=headers)
    assert response.status_code==200,response.text
    assert response.json()['snapshot_id']==receipt['snapshot_id']
    saved=owner.get('/api/v1/pod/us-catalog/products/s2b:7').json()
    assert saved['detail']['stock_info'][0]['price']=='12.5' and saved['detail_cached']
    with SessionLocal() as db:assert db.query(PodSupplierCatalogSnapshot).filter_by(provider='amazon_us').count()==1


def test_us_worker_resume_upload_has_no_new_supplier_calls(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client'/'desktop'))
    from us_catalog_worker import run
    from erp.domains.pod.integrations.s2b import S2BClient
    calls=[]
    monkeypatch.setattr(S2BClient,'product_categories',lambda self:[])
    def products(self,page,size,*,country):
        calls.append((page,country));return {'data':result()['products'],'_page_item_count':1}
    monkeypatch.setattr(S2BClient,'product_types',products)
    request={'market':'US','kind':'sync'}
    class Client:
        attempts=0
        def request(self,method,path,value=None,**kwargs):
            if path.endswith('snapshot'):
                return {'request':request,'sources':{'s2b':{'error':'','connection':{
                    'provider':'s2b','connection_id':'b'*32,'base_url':'https://supplier.example.test','config':{},'secrets':{'app_key':'private','app_secret':'private-secret'}}}}},{}
            self.attempts+=1
            if self.attempts==1:raise ConnectionError('isolated upload outage')
            assert 'private-secret' not in gzip.decompress(base64.b64decode(value['content'])).decode()
            return {'snapshot_id':'c'*32,'product_count':1,'errors':[]},{}
    client=Client();job={'id':'a'*32,'type':'pod_supplier_us_catalog_sync','attempts':1,'payload':request}
    with pytest.raises(ConnectionError):run(client,'/job',job,tmp_path,threading.Event())
    assert run(client,'/job',job,tmp_path,threading.Event())['product_count']==1
    assert calls==[(1,'US')]
