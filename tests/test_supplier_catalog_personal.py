"""Original supplier catalog paging executes on the initiating PC."""
import base64
import gzip
import hashlib
import json
import threading
from pathlib import Path

from erp.core.database import SessionLocal
from erp.core.security import vault
from erp.domains.pod.models import PodSupplierCatalogSnapshot
from erp.domains.pod.supplier_catalog import catalog_view
from erp.models import IntegrationConnection, Job
from deploy.pilot_gateway import allowed
from test_personal_job_controls import team


def test_catalog_sync_merges_success_and_preserves_failed_provider(team):
    assert allowed('POST','/api/v1/pod/catalog/sync')
    assert allowed('GET','/api/v1/execution-devices/local/pc-'+'a'*32+'/jobs/'+'b'*32+'/supplier-catalog-snapshot')
    clients,worker=team
    owner=clients['owner']
    assert owner.post('/api/v1/execution-devices/local/register',json={
        'id':worker,'name':'original PC','supplier_catalog_version':1}).status_code==200
    with SessionLocal() as db:
        for supplier in ('hicustom','s2b'):
            db.add(IntegrationConnection(provider=supplier,name=supplier+'-catalog-test',
                   base_url='https://supplier.example.test',enabled=True,
                   secrets_encrypted=vault.encrypt({'app_key':'private-key','app_secret':'private-secret'})))
        old={'version':2,'synced_at':'2026-09-23T00:00:00+00:00',
             'products':[{'supplier':'s2b','source_id':'9','catalog_key':'s2b:9','name':'keep'}],
             'categories':[{'supplier':'s2b','id':'old'}],'previews':{},'details':{},
             'provider_sync':{'s2b':{'product_count':1,'error':''}}}
        snapshot=PodSupplierCatalogSnapshot(provider='all',is_current=True,
             product_count=1,category_count=1,fetched_at=old['synced_at'],
             payload_json=json.dumps(old))
        db.add(snapshot);db.commit()
        previous=snapshot.id
    response=owner.post('/api/v1/pod/catalog/sync')
    assert response.status_code==202,response.text
    job_id=response.json()['job']['id']
    base=f'/api/v1/execution-devices/local/{worker}'
    assert owner.post(base+'/claim').json()['id']==job_id
    endpoint=base+'/jobs/'+job_id
    headers={'X-Worker-Attempt':'1'}
    assert clients['other'].get(endpoint+'/supplier-catalog-snapshot',headers=headers).status_code==403
    source=owner.get(endpoint+'/supplier-catalog-snapshot',headers=headers)
    assert source.status_code==200,source.text
    assert source.json()['base_snapshot_id']==previous
    assert source.json()['sources']['hicustom']['connection']['secrets']['app_key']=='private-key'
    providers=[{'supplier':'hicustom','products':[{'supplier':'hicustom',
                'source_id':'7','catalog_key':'hicustom:7','name':'new'}],
                'categories':[{'supplier':'hicustom','id':'new'}],'error':''},
               {'supplier':'s2b','products':None,'categories':None,'error':'temporarily unavailable'}]
    packed=gzip.compress(json.dumps({'providers':providers}).encode(),mtime=0)
    body={'sha256':hashlib.sha256(packed).hexdigest(),
          'content':base64.b64encode(packed).decode()}
    assert clients['other'].post(endpoint+'/supplier-catalog-result',json=body,headers=headers).status_code==403
    assert owner.post(endpoint+'/supplier-catalog-result',json=body,
                      headers={'X-Worker-Attempt':'2'}).status_code==409
    result=owner.post(endpoint+'/supplier-catalog-result',json=body,headers=headers)
    assert result.status_code==200,result.text
    assert result.json()['product_count']==2
    assert result.json()['errors']==['S2B：temporarily unavailable']
    assert owner.post(endpoint+'/supplier-catalog-result',json=body,headers=headers).json()==result.json()
    assert owner.post(endpoint+'/complete',json={'result':result.json()},headers=headers).status_code==200
    with SessionLocal() as db:
        assert db.get(Job,job_id).status=='completed'
        view=catalog_view(db)
        assert view['provider_counts']=={'hicustom':1,'s2b':1}
        assert any(item['source_id']=='9' and item['supplier']=='s2b' for item in view['products'])


def test_catalog_rejects_foreign_supplier_item(team):
    clients,worker=team
    owner=clients['owner']
    assert owner.post('/api/v1/execution-devices/local/register',json={
        'id':worker,'name':'original PC','supplier_catalog_version':1}).status_code==200
    response=owner.post('/api/v1/pod/catalog/sync')
    assert response.status_code==202,response.text
    job_id=response.json()['job']['id']
    base=f'/api/v1/execution-devices/local/{worker}'
    assert owner.post(base+'/claim').json()['id']==job_id
    providers=[{'supplier':'hicustom','products':[{'supplier':'s2b','source_id':'1',
               'catalog_key':'s2b:1'}],'categories':[],'error':''},
               {'supplier':'s2b','products':None,'categories':None,'error':'missing'}]
    packed=gzip.compress(json.dumps({'providers':providers}).encode(),mtime=0)
    response=owner.post(base+'/jobs/'+job_id+'/supplier-catalog-result',json={
        'sha256':hashlib.sha256(packed).hexdigest(),
        'content':base64.b64encode(packed).decode()},headers={'X-Worker-Attempt':'1'})
    assert response.status_code==409
    with SessionLocal() as db:assert db.get(Job,job_id).status=='running'


def test_catalog_worker_runs_original_paging_once_and_journals_no_secret(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client'/'desktop'))
    from supplier_catalog_worker import run
    from erp.domains.pod.integrations.hicustom import HicustomClient
    calls=[]
    monkeypatch.setattr(HicustomClient,'product_categories',lambda self:[{'id':'category'}])
    def product_types(self,page,size):
        calls.append((page,size))
        return {'items':[{'id':'7','name':'sample'}],'total':1}
    monkeypatch.setattr(HicustomClient,'product_types',product_types)
    class Client:
        def request(self,method,path,body=None,*,extra_headers=None):
            if path.endswith('/supplier-catalog-snapshot'):
                return {'base_snapshot_id':None,'sources':{
                    'hicustom':{'connection':{'provider':'hicustom','connection_id':'b'*32,
                        'base_url':'https://supplier.example.test','config':{},
                        'secrets':{'app_key':'private-key','app_secret':'private-secret'}},'error':''},
                    's2b':{'connection':None,'error':'unavailable'}}},{}
            assert path.endswith('/supplier-catalog-result')
            payload=json.loads(gzip.decompress(base64.b64decode(body['content'])))
            assert payload['providers'][0]['products'][0]['catalog_key']=='hicustom:7'
            return {'snapshot_id':'c'*32,'product_count':1,
                    'errors':['S2B：unavailable']},{}
    endpoint='/execution-devices/local/pc-'+'d'*32+'/jobs/'+'a'*32
    job={'id':'a'*32,'type':'pod_supplier_catalog_sync','attempts':1}
    assert run(Client(),endpoint,job,tmp_path,threading.Event())['product_count']==1
    assert run(Client(),endpoint,job,tmp_path,threading.Event())['product_count']==1
    assert calls==[(1,100)]
    packed=(tmp_path/'supplier-catalog-tasks'/('a'*32)/'catalog-result.json.gz').read_bytes()
    assert b'private-secret' not in packed
    assert 'private-secret' not in gzip.decompress(packed).decode()


def test_failed_personal_catalog_can_retry_same_job_on_owner_pc(team):
    clients,worker=team
    owner=clients['owner']
    assert owner.post('/api/v1/execution-devices/local/register',json={
        'id':worker,'name':'original PC','supplier_catalog_version':1}).status_code==200
    created=owner.post('/api/v1/pod/catalog/sync')
    assert created.status_code==202,created.text
    job_id=created.json()['job']['id']
    base=f'/api/v1/execution-devices/local/{worker}'
    assert owner.post(base+'/claim').json()['id']==job_id
    failed=owner.post(base+'/jobs/'+job_id+'/fail',json={
        'error_code':'upload_rejected','error_message':'HTTP 413'},
        headers={'X-Worker-Attempt':'1'})
    assert failed.status_code==200,failed.text
    # Automatic retry may occur before max_attempts; force the exhausted state
    # to exercise manual recovery of an already journaled read-only result.
    with SessionLocal() as db:
        row=db.get(Job,job_id);row.status='failed';row.attempts=row.max_attempts;db.commit()
    assert clients['other'].post('/api/v1/jobs/'+job_id+'/retry').status_code==403
    retried=owner.post('/api/v1/jobs/'+job_id+'/retry')
    assert retried.status_code==200,retried.text
    assert retried.json()['target_worker_id']==worker
    claimed=owner.post(base+'/claim').json()
    assert claimed['id']==job_id and claimed['attempts']==3
