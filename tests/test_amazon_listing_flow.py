import base64
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from erp.core.database import SessionLocal
from erp.core.errors import AppError, ConflictError
from erp.core.jsonutil import dumps, loads
from erp.integrations.amazon import AmazonUSClient, AmazonWriteUnknown, AmazonRequestRejected, US_MARKETPLACE
from erp.models import Asset, CatalogProduct, CatalogVariant, CatalogProductAsset, Job, AmazonListingBatch, AmazonListingItem
from erp.services import amazon_pricing as pricing, amazon_listings as listings
from erp.services.amazon_listing_payload import digest, field, make_body, validate_payload, validate_family, schema_summary
from test_amazon_foundation import CONFIG
from test_personal_job_controls import team
from test_us_freight import prepare, read_job, result
from test_us_supplier_catalog import body as freight_body


def definition(level='NONE', theme=False):
    props={k:{'type':'array'} for k in ['brand','item_name','bullet_point','product_description','size','color',
        'condition_type','supplier_declared_has_product_identifier_exemption','purchasable_offer',
        'fulfillment_availability','merchant_shipping_group','main_product_image_locator','item_length_width','country_of_origin']}
    props['merchant_shipping_group']={'type':'array','items':{'type':'object','properties':{
        'value':{'type':'string','enum':['free-template'],'enumNames':['专用包邮']}}}}
    if theme:
        props['variation_theme']={'type':'array','items':{'type':'object','properties':{
            'name':{'type':'string','enum':['SIZE/COLOR','OLD'],'$lifecycle':{'enumDeprecated':['OLD']}}}}}
        props.update(parentage_level={'type':'array'},child_parent_sku_relationship={'type':'array'})
    return {'product_type':'MOUSE_PAD','parentage':level,'checksum':'schema-'+level,
            'schema':{'type':'object','required':['brand','item_name'],'properties':props}}


def content():
    return {'title':'Extended Desk Mouse Pad 40 x 80 cm','bullet_points':['One printed mouse pad'],
            'description':'A printed desk mouse pad measuring 40 x 80 cm.','attributes':{}}


def test_final_price_real_quote_and_cost_markup():
    value=pricing.calculate('25.90','35','office')
    assert value['price']=='20.92' and value['base_cost_cny']=='60.90'
    assert value['referral_fee_usd']=='3.14' and value['expected_commission_usd']=='2.89'
    assert value['expected_profit_usd']=='4.62' and Decimal(value['cost_profit_percent_actual'])>=50
    assert pricing.DEFAULTS['buyer_shipping_usd']==0
    zero=pricing.calculate(25.9,35,'office',{'return_rate_percent':0,'advertising_percent':0,'cost_profit_percent':0})
    assert Decimal(zero['price'])<Decimal(value['price'])
    shipping=pricing.calculate(25.9,35,'office',{'buyer_shipping_usd':4})
    assert shipping['price']=='16.92' and shipping['customer_total_usd']=='20.92'
    assert shipping['referral_fee_usd']=='3.14'


def test_official_fee_tiers_and_minimum():
    assert pricing.referral('clothing',15)==Decimal('.75')
    assert pricing.referral('clothing','15.01')==Decimal('1.50')
    assert pricing.referral('clothing','20.01')==Decimal('3.40')
    assert pricing.referral('electronics_accessories',200)==Decimal('23.00')
    assert pricing.referral('office',1)==Decimal('.30')
    with pytest.raises(AppError):pricing.calculate(10,10,'unreviewed')
    with pytest.raises(AppError):pricing.calculate(10,10,'office',{'return_rate_percent':80,'advertising_percent':50})


@pytest.mark.parametrize('change',[{'commission':1},{'advertising_percent':True},{'usd_cny':0},
    {'return_rate_percent':100},{'other_cost_cny':'nan'},{'return_shipping_cny':'Infinity'}])
def test_invalid_pricing_not_silently_defaulted(change):
    with pytest.raises(AppError):pricing.calculate(25.9,35,'office',change)


def test_body_binds_variant_finances_and_human_facts():
    snap={'brand':'Generic','quantity':7,'handling_days':3,'shipping_template':'free-template',
          'parent_sku':'','product_type':'MOUSE_PAD','attributes':{'country_of_origin':field('US')}}
    item={'size':'40x80cm','pricing':{'price':'20.92'},'images':['https://image.example/main.jpg']}
    value=make_body(snap,item,content(),definition())
    a=value['attributes']
    assert a['item_length_width'][0]['length']['value']==31.4961
    assert a['fulfillment_availability'][0]['quantity']==7
    assert a['purchasable_offer'][0]['currency']=='USD'
    assert not validate_payload(value,definition())
    assert schema_summary(definition())['variation_themes']==[]
    for name in ('purchasable_offer','country_of_origin','manufacturer'):
        with pytest.raises(AppError):make_body(snap,item,{**content(),'attributes':{name:field('AI guess')}},definition())


def test_custom_utf8_limit_and_family_axes():
    d=definition();d['schema']['properties']['item_name']={'type':'string','maxUtf8ByteLength':4}
    errors=validate_payload({'productType':'MOUSE_PAD','requirements':'LISTING','attributes':{'brand':[],'item_name':'汉字'}},d)
    assert errors and errors[0]['path']=='item_name'
    snap={'parent_sku':'parent','variation_theme':'SIZE/COLOR'}
    parent={'sku':'parent','body':{'attributes':{}}}
    child=lambda sku,size,color:{'sku':sku,'body':{'attributes':{'size':field(size),'color':field(color)}}}
    assert validate_family(snap,[parent,child('a','M','white'),child('b','L','white')])==[]
    with pytest.raises(AppError):validate_family(snap,[parent,child('a','M','white'),child('b','M','white')])
    parent['body']['attributes']['purchasable_offer']=[]
    with pytest.raises(AppError):validate_family(snap,[parent,child('a','M','white'),child('b','L','white')])


def test_transport_preview_read_cache_and_unknown_write(monkeypatch):
    monkeypatch.setattr('erp.integrations.amazon.time.sleep',lambda _:None)
    calls=[];raw=json.dumps({'type':'object','properties':{}}).encode()
    checksum=base64.b64encode(hashlib.md5(raw).digest()).decode()
    def handler(r):
        calls.append(r)
        if r.url.host=='api.amazon.com':return httpx.Response(200,json={'access_token':'access','expires_in':3600})
        if r.url.host.endswith('amazonaws.com'):
            assert 'x-amz-access-token' not in r.headers
            return httpx.Response(200,content=raw)
        if 'definitions' in r.url.path:return httpx.Response(200,json={'schema':{'link':{'resource':'https://bucket.s3.amazonaws.com/schema'},'checksum':checksum}})
        if r.url.params.get('mode')=='VALIDATION_PREVIEW':return httpx.Response(200,json={'status':'VALID'})
        return httpx.Response(503)
    api=AmazonUSClient(CONFIG,transport=httpx.MockTransport(handler))
    try:
        assert api.definition('MOUSE_PAD')['checksum']==checksum
        assert api.put_listing('SKU',{},preview=True)['status']=='VALID'
        with pytest.raises(AmazonWriteUnknown):api.put_listing('SKU',{})
        assert sum(r.url.host=='api.amazon.com' for r in calls)==1
        assert len(calls)==5
    finally:api.close()


def setup_flow(team):
    clients,worker=team;owner=prepare(clients,worker)
    job,endpoint,headers=read_job(owner,worker)
    r=owner.post(endpoint+'/supplier-catalog-result',json=freight_body(result(job)),headers=headers)
    assert r.status_code==200,r.text
    assert owner.post(endpoint+'/complete',json={'result':r.json()},headers=headers).status_code==200
    assert owner.post('/api/v1/execution-devices/local/register',json={'id':worker,'name':'PC','amazon_version':1}).status_code==200
    s=clients['admin'].post('/api/v1/stores',json={'platform':'amazon','name':'Test Amazon US',
        'external_account_id':'SELLER','offer_prefix':'US-','currency':'USD','secrets':{k:v for k,v in CONFIG.items() if k!='seller_id'}})
    assert s.status_code==201,s.text
    store_id=s.json()['id']
    with SessionLocal() as db:
        product=CatalogProduct(sku='S2B-000001-01',source_type='pod_amazon',name='US printed mouse pad',
            production_json=dumps({'market':'US','supplier':'s2b','product_type_id':7,'remote_product_id':100}))
        db.add(product);db.flush()
        variant=CatalogVariant(product_id=product.id,sku=product.sku,size='40x80cm',supplier_sku_id='3')
        db.add(variant);db.flush()
        asset=Asset(name='display.jpg',oss_url='https://example-assets.oss-cn-chengdu.aliyuncs.com/display.jpg')
        db.add(asset);db.flush();db.add(CatalogProductAsset(product_id=product.id,variant_id=variant.id,asset_id=asset.id,role='main'))
        db.commit();product_id=product.id;variant_id=variant.id
    request={'store_id':store_id,'items':[{'product_id':product_id,'variant_id':variant_id}],
        'product_type':'MOUSE_PAD','fee_category':'office','shipping_template':'free-template',
        'free_shipping_confirmed':True,'main_image_confirmed':True,'quantity':5}
    return owner,worker,store_id,request


def lease(owner,worker,job_id):
    job=owner.post(f'/api/v1/execution-devices/local/{worker}/claim').json()
    assert job['id']==job_id
    return f'/api/v1/execution-devices/local/{worker}/jobs/{job_id}',{'X-Worker-Attempt':str(job['attempts'])}


def finish(owner,endpoint,headers,data):
    r=owner.post(endpoint+'/amazon-result',json={'result':data},headers=headers)
    assert r.status_code==200,r.text
    assert owner.post(endpoint+'/amazon-result',json={'result':data},headers=headers).json()==r.json()
    complete={'result':{'amazon':r.json(),'folder':'isolated'}}
    assert owner.post(endpoint+'/complete',json=complete,headers=headers).status_code==200
    assert owner.post(endpoint+'/complete',json=complete,headers=headers).status_code==200
    return r.json()


def prepared_flow(team,monkeypatch):
    owner,worker,store_id,request=setup_flow(team)
    monkeypatch.setattr(AmazonUSClient,'request',lambda *a,**k:pytest.fail('Amazon must never run in cloud'))
    r=owner.post(f'/api/v1/amazon/stores/{store_id}/catalog',json={'product_type':'MOUSE_PAD'})
    assert r.status_code==200,r.text
    endpoint,headers=lease(owner,worker,r.json()['job_id'])
    assert team[0]['other'].get(endpoint+'/amazon-snapshot',headers=headers).status_code==403
    assert owner.post(endpoint+'/complete',json={'result':{}},headers=headers).status_code==409
    finish(owner,endpoint,headers,{'definitions':{'NONE':definition()}})
    assert owner.post(f'/api/v1/amazon/stores/{store_id}/catalog',json={'product_type':'MOUSE_PAD'}).json()['cached']
    assert owner.post('/api/v1/amazon/preview',json=request).json()['items'][0]['pricing']['price']=='20.92'
    r=owner.post('/api/v1/amazon/batches',json=request);assert r.status_code==201,r.text
    batch=r.json()['batch_id'];endpoint,headers=lease(owner,worker,r.json()['job_id'])
    snap=owner.get(endpoint+'/amazon-snapshot',headers=headers).json()
    assert snap['config']['secrets']['lwa_client_secret']=='private'
    assert 'private' not in owner.get('/api/v1/jobs/'+r.json()['job_id']).text
    sku=snap['batch']['snapshot']['items'][0]['sku']
    assert finish(owner,endpoint,headers,{'content':{sku:content()}})['status']=='ready'
    return owner,worker,batch,sku


def validated_flow(team,monkeypatch):
    owner,worker,batch,sku=prepared_flow(team,monkeypatch)
    url=f'/api/v1/amazon/batches/{batch}'
    assert team[0]['admin'].post(url+'/validate').status_code==409
    assert owner.post(url+'/submit',json={'draft_digest':'0'*64}).status_code==409
    r=owner.post(url+'/validate');assert r.status_code==200,r.text
    endpoint,headers=lease(owner,worker,r.json()['job_id'])
    assert finish(owner,endpoint,headers,{'items':[{'sku':sku,'response':{'status':'VALID'}}]})['status']=='validated'
    return owner,worker,batch,sku


def test_cloud_full_receipt_fencing_and_live_status(team,monkeypatch):
    owner,worker,batch,sku=validated_flow(team,monkeypatch);url=f'/api/v1/amazon/batches/{batch}'
    value=owner.get(url).json();dg=value['result']['validated_digest']
    r=owner.post(url+'/submit',json={'draft_digest':dg});assert r.status_code==200,r.text
    endpoint,headers=lease(owner,worker,r.json()['job_id'])
    body=value['draft']['bodies'][0]['body']
    assert owner.post(endpoint+'/amazon-intent',json={'sku':sku,'body_digest':digest(body)},headers={'X-Worker-Attempt':'2'}).status_code==409
    intent={'sku':sku,'body_digest':digest(body)}
    assert owner.post(endpoint+'/amazon-intent',json=intent,headers=headers).status_code==200
    assert owner.post(endpoint+'/amazon-intent',json=intent,headers=headers).status_code==409
    assert finish(owner,endpoint,headers,{'items':[{'sku':sku,'response':{'status':'ACCEPTED','submissionId':'receipt'}}]})['status']=='processing'
    assert owner.post(url+'/submit',json={'draft_digest':dg}).status_code==409
    r=owner.post(url+'/reconcile');endpoint,headers=lease(owner,worker,r.json()['job_id'])
    response={'summaries':[{'marketplaceId':US_MARKETPLACE,'asin':'TESTASIN','status':['BUYABLE']}],'issues':[]}
    assert finish(owner,endpoint,headers,{'items':[{'sku':sku,'response':response}]})['status']=='published'
    assert owner.get(url).json()['items'][0]['receipt']['intent']['body_digest']==digest(body)


def test_known_rejection_can_be_fixed_unknown_cannot(team,monkeypatch):
    owner,worker,batch,sku=validated_flow(team,monkeypatch);url=f'/api/v1/amazon/batches/{batch}'
    dg=owner.get(url).json()['result']['validated_digest']
    r=owner.post(url+'/submit',json={'draft_digest':dg});endpoint,headers=lease(owner,worker,r.json()['job_id'])
    data=owner.get(url).json();body=data['draft']['bodies'][0]['body']
    assert owner.post(endpoint+'/amazon-intent',json={'sku':sku,'body_digest':digest(body)},headers=headers).status_code==200
    finish(owner,endpoint,headers,{'items':[{'sku':sku,'rejected':True,'response':{'status':'INVALID','http_status':429,'issues':[]}}]})
    assert owner.get(url).json()['repairable']
    r=owner.patch(url+'/facts',json={'content':{sku:content()},'main_image_confirmed':True})
    assert r.status_code==200,r.text
    assert owner.get(url).json()['status']=='ready'
    with SessionLocal() as db:
        row=db.query(AmazonListingBatch).filter_by(id=batch).one();row.status='unknown'
        db.query(AmazonListingItem).filter_by(batch_id=batch).one().status='unknown';db.commit()
    assert owner.patch(url+'/facts',json={'content':{sku:content()}}).status_code==409


def test_basis_refresh_and_edits_invalidate_platform_preview(team,monkeypatch):
    owner,worker,batch,sku=validated_flow(team,monkeypatch);url=f'/api/v1/amazon/batches/{batch}'
    dg=owner.get(url).json()['result']['validated_digest']
    value=owner.post(url+'/refresh-basis')
    assert value.status_code==200,value.text
    assert value.json()['status']=='ready'
    assert not owner.get(url).json()['result']
    assert owner.post(url+'/submit',json={'draft_digest':dg}).status_code==409
    edited=content();edited['attributes']={'purchasable_offer':[{'value':'fake'}]}
    value=owner.patch(url+'/facts',json={'content':{sku:edited}})
    assert value.status_code==200 and value.json()['status']=='needs_facts'
    assert owner.get(url).json()['draft']['errors'][0]['path']=='content'


def test_parent_submission_is_nonbuyable_and_unknown_get_requires_exact_body():
    d=definition('PARENT',True)
    snap={'brand':'Generic','product_type':'MOUSE_PAD','parent_sku':'family','variation_theme':'SIZE/COLOR'}
    body=make_body(snap,{},content(),d,parent=True)
    assert 'purchasable_offer' not in body['attributes'] and 'fulfillment_availability' not in body['attributes']
    assert 'parent_sku' not in body['attributes']['child_parent_sku_relationship'][0]
    assert schema_summary(d)['variation_themes']==['SIZE/COLOR']


def test_unknown_read_cannot_mark_an_unrelated_existing_sku_published(team,monkeypatch):
    owner,worker,batch,sku=validated_flow(team,monkeypatch);url=f'/api/v1/amazon/batches/{batch}'
    dg=owner.get(url).json()['result']['validated_digest']
    r=owner.post(url+'/submit',json={'draft_digest':dg});endpoint,headers=lease(owner,worker,r.json()['job_id'])
    finish(owner,endpoint,headers,{'items':[{'sku':sku,'unknown':True,'response':{}}]})
    r=owner.post(url+'/reconcile');endpoint,headers=lease(owner,worker,r.json()['job_id'])
    unrelated={'summaries':[{'marketplaceId':US_MARKETPLACE,'asin':'OTHER','status':['BUYABLE']}],'attributes':{}}
    assert finish(owner,endpoint,headers,{'items':[{'sku':sku,'response':unrelated}]})['status']=='unknown'


def test_worker_replays_and_does_not_retry_unknown_write(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    from amazon_worker import execute_platform
    # Isolated encryption adapter; production uses Windows DPAPI.
    protect=lambda raw,**kw:raw
    body={'productType':'MOUSE_PAD','attributes':{'brand':field('Generic')}}
    snap={'operation':'amazon_submit','request':{'draft_digest':'a'*64},
          'batch':{'draft':{'bodies':[{'sku':'a','body':body},{'sku':'b','body':body}]}}}
    calls=[]
    class API:
        def listing(self,sku):calls.append(('GET',sku));return None
        def put_listing(self,sku,body):calls.append(('PUT',sku));raise AmazonWriteUnknown()
    intents=[]
    first=execute_platform(snap,API(),tmp_path,lambda:None,protect,lambda *a:intents.append(a))
    assert first['items'][0]['unknown'] and first['items'][1]['not_started']
    assert execute_platform(snap,API(),tmp_path,lambda:None,protect,lambda *a:intents.append(a))==first
    assert calls==[('GET','a'),('PUT','a')] and len(intents)==1


def test_manual_retry_grants_only_one_attempt_on_original_pc(team):
    owner,worker,store_id,_=setup_flow(team)
    r=owner.post(f'/api/v1/amazon/stores/{store_id}/catalog',json={'keywords':'mouse pad'})
    id=r.json()['job_id'];endpoint,headers=lease(owner,worker,id)
    failed=owner.post(endpoint+'/fail',json={'error_code':'local_execution_failed','error_message':'isolated failure','retryable':False},headers=headers)
    assert failed.status_code==200,failed.text
    assert owner.post(f'/api/v1/execution-devices/local/{worker}/claim').json() is None
    assert owner.post('/api/v1/jobs/'+id+'/retry').json()['target_worker_id']==worker
    job=owner.post(f'/api/v1/execution-devices/local/{worker}/claim').json()
    assert job['attempts']==2 and job['max_attempts']==2
