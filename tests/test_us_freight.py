import base64
import gzip
import json
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from erp.core.database import SessionLocal
from erp.core.errors import AppError, ConflictError
from erp.core.jsonutil import dumps, loads
from erp.domains.pod.models import PodSupplierCatalogSnapshot
from erp.domains.pod.us_freight import DEFAULT_RULE, JOB_TYPE, estimate, select_gofo
from erp.models import IntegrationConnection, Job, utc_now
from test_personal_job_controls import team
from test_us_composition import seed
from test_us_supplier_catalog import body


OPTIONS = [
    {'name':'GOFO官方物流-28（包邮分区收费）','logistics_id':28,'min_amount':'24.46','max_amount':'41.85'},
    {'name':'USPS','min_amount':'41.32','max_amount':'93.80'},
    {'name':'GOFO Temu 在线下单','min_amount':'.10','max_amount':'.10'},
]
URL='/api/v1/pod/us-catalog/products/s2b:7'


def prepare(clients, worker, version=3):
    owner=clients['owner'];seed(owner,worker)
    assert owner.post('/api/v1/execution-devices/local/register',json={
        'id':worker,'name':'PC','supplier_catalog_version':version}).status_code==200
    return owner


def read_job(owner, worker):
    response=owner.post(URL+'/freight',json={'variant_id':3});assert response.status_code==202,response.text
    job=response.json()['job']
    assert job['type']==JOB_TYPE and job['target_worker_id']==worker and job['max_attempts']==1
    endpoint=f'/api/v1/execution-devices/local/{worker}'
    assert owner.post(endpoint+'/claim').json()['id']==job['id']
    return job,endpoint+'/jobs/'+job['id'],{'X-Worker-Attempt':'1'}


def result(job, options=OPTIONS):
    return {'market':'US','kind':'freight','fingerprint':job['payload']['fingerprint'],'options':options}


def test_decimal_rule_uses_each_interval_and_manual_zero():
    assert estimate('24.46','41.85',DEFAULT_RULE)['calculated_cny']=='35.0'
    assert estimate('10','20',DEFAULT_RULE)['calculated_cny']=='16.0'
    value=estimate('10','30',{'range_position_percent':25,'buffer_percent':10,'buffer_cny':2,'rounding_cny':.5},0)
    assert value['calculated_cny']=='18.5' and value['selected_cny']=='0' and value['manual']
    assert estimate('24.46','24.46',{'buffer_percent':0,'rounding_cny':.01})['calculated_cny']=='24.46'


@pytest.mark.parametrize('low,high,profile',[
    ('nan',20,{}),(10,'Infinity',{}),(21,20,{}),(-1,20,{}),
    (10,20,{'range_position_percent':101}),(10,20,{'rounding_cny':0}),
    (10,20,{'buffer_percent':True}),(10,20,{'extra':1}),
])
def test_invalid_money_and_rule_rejected(low,high,profile):
    with pytest.raises(AppError):estimate(low,high,profile)


def test_no_substitution_for_missing_ambiguous_or_unpriced_gofo():
    assert select_gofo(OPTIONS)['channel_id']=='28'
    with pytest.raises(ConflictError):select_gofo(OPTIONS[1:])
    with pytest.raises(ConflictError):select_gofo([OPTIONS[0],OPTIONS[0]])
    with pytest.raises(AppError):select_gofo([{'name':'GOFO','min_amount':None,'max_amount':30}])
    with pytest.raises(ConflictError):select_gofo([{**OPTIONS[0],'currency':'USD'}])


def test_origin_pc_quote_is_cached_and_replayed_without_vendor_calls(team,monkeypatch):
    clients,worker=team;owner=prepare(clients,worker)
    from erp.domains.pod.integrations.s2b import S2BClient
    monkeypatch.setattr(S2BClient,'_request',lambda *a,**kw:pytest.fail('Cloud must not call supplier'))
    assert not owner.post(URL+'/freight-preview',json={'variant_id':3}).json()['available']
    job,endpoint,headers=read_job(owner,worker)
    assert owner.post(URL+'/freight',json={'variant_id':3}).json()['job']['id']==job['id']
    assert clients['other'].get(endpoint+'/supplier-catalog-snapshot',headers=headers).status_code==403
    assert owner.post(endpoint+'/complete',json={'result':{}},headers=headers).status_code==409
    snapshot=owner.get(endpoint+'/supplier-catalog-snapshot',headers=headers).json()
    assert snapshot['request']['params']=={'basic_product_id':7,'platform':99,'country':'US','num':1,
                                          'weight':'500','length':'20','width':'30','height':'2'}
    value=body(result(job))
    assert owner.post(endpoint+'/supplier-catalog-result',json=value,headers={'X-Worker-Attempt':'2'}).status_code==409
    receipt=owner.post(endpoint+'/supplier-catalog-result',json=value,headers=headers)
    assert receipt.status_code==200,receipt.text
    assert owner.post(endpoint+'/supplier-catalog-result',json=value,headers=headers).json()==receipt.json()
    assert owner.post(endpoint+'/complete',json={'result':receipt.json()},headers=headers).status_code==200
    cached=owner.post(URL+'/freight',json={'variant_id':3}).json()
    assert cached['cached'] and cached['job'] is None
    assert cached['preview']['purchase_and_freight_cny']=='60.9'
    assert owner.get('/api/v1/pod/us-catalog').json()['product_count']==1
    assert clients['owner'].put('/api/v1/settings/amazon_us_freight',json={'value':DEFAULT_RULE}).status_code==403
    assert clients['admin'].put('/api/v1/settings/amazon_us_freight',json={'value':{'buffer_percent':0}}).status_code==200
    changed=owner.post(URL+'/freight-preview',json={'variant_id':3}).json()
    assert changed['freight']['calculated_cny']=='34.0'
    assert owner.post(URL+'/freight-preview',json={'variant_id':3,'manual_freight_cny':40}).json()['purchase_and_freight_cny']=='65.9'
    with SessionLocal() as db:
        snapshot=db.query(PodSupplierCatalogSnapshot).filter_by(provider='amazon_us',is_current=True).one()
        data=loads(snapshot.payload_json,{})
        assert len(data['freight_quotes'])==1 and db.query(Job).filter_by(type=JOB_TYPE).count()==1
        data['freight_quotes'][job['payload']['fingerprint']]['fetched_at']=(utc_now()-timedelta(days=8)).isoformat()
        snapshot.payload_json=dumps(data);db.commit()
    assert not owner.post(URL+'/freight-preview',json={'variant_id':3}).json()['available']
    with SessionLocal() as db:assert db.query(Job).filter_by(type=JOB_TYPE).count()==1


def test_old_client_cannot_claim_or_queue_new_quote(team):
    clients,worker=team;owner=prepare(clients,worker,2)
    assert owner.post(URL+'/freight',json={'variant_id':3}).status_code==409
    with SessionLocal() as db:assert db.query(Job).filter_by(type=JOB_TYPE).count()==0


@pytest.mark.parametrize('change',['price','credentials','fingerprint','secret'])
def test_stale_or_invalid_quote_cannot_overwrite_shared_facts(team,change):
    clients,worker=team;owner=prepare(clients,worker);job,endpoint,headers=read_job(owner,worker)
    value=result(job)
    if change in {'price','credentials'}:
        with SessionLocal() as db:
            if change=='price':
                snapshot=db.query(PodSupplierCatalogSnapshot).filter_by(provider='amazon_us',is_current=True).one()
                data=loads(snapshot.payload_json,{});data['details']['s2b:7']['items'][0]['price']=30
                snapshot.payload_json=dumps(data)
            else:
                from erp.core.security import vault
                db.query(IntegrationConnection).filter_by(provider='s2b').one().secrets_encrypted=vault.encrypt({'app_key':'another','app_secret':'new'})
            db.commit()
    elif change=='fingerprint':value['fingerprint']='0'*64
    else:value['api_key']='private'
    assert owner.post(endpoint+'/supplier-catalog-result',json=body(value),headers=headers).status_code==409
    with SessionLocal() as db:
        assert db.get(Job,job['id']).status=='running'
        assert not loads(db.query(PodSupplierCatalogSnapshot).filter_by(provider='amazon_us',is_current=True).one().payload_json,{}).get('freight_quotes')


def test_quote_worker_upload_resume_reads_vendor_once(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    from us_catalog_worker import run
    from erp.domains.pod.integrations.s2b import S2BClient
    calls=[]
    def request(self,method,path,**kw):
        assert method=='GET' and path=='/open/v1/logisticsCalculation'
        assert kw['params']['country']=='US' and kw['params']['num']==1
        calls.append(path);return {'data':OPTIONS}
    monkeypatch.setattr(S2BClient,'_request',request)
    payload={'market':'US','kind':'freight','fingerprint':'f'*64,'params':{'country':'US','num':1}}
    class Client:
        attempts=0
        def request(self,method,path,value=None,**kw):
            if path.endswith('snapshot'):
                return {'request':payload,'sources':{'s2b':{'connection':{'provider':'s2b','connection_id':'b'*32,
                    'base_url':'https://supplier.invalid','config':{},'secrets':{'app_key':'private','app_secret':'private-secret'}}}}},{}
            self.attempts+=1
            if self.attempts==1:raise ConnectionError('isolated upload interruption')
            assert json.loads(gzip.decompress(base64.b64decode(value['content'])))['options']==OPTIONS
            return {'quote':{'minimum_cny':'24.46'},'errors':[]},{}
    client=Client();job={'id':'a'*32,'type':JOB_TYPE,'attempts':1,'payload':payload}
    with pytest.raises(ConnectionError):run(client,'/job',job,tmp_path,threading.Event())
    assert run(client,'/job',job,tmp_path,threading.Event())['quote']['minimum_cny']=='24.46'
    assert len(calls)==1 and b'private-secret' not in (tmp_path/'supplier-us-catalog-tasks'/job['id']/'result.json.gz').read_bytes()
