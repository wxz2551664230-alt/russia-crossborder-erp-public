from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps, loads
from erp.models import Job, AmazonListingBatch
from erp.services.amazon_listing_payload import digest, field, make_body, validate_payload
from test_amazon_listing_flow import definition, content, setup_flow, lease, finish, prepared_flow, validated_flow
from test_personal_job_controls import team


def test_parentage_cache_fills_only_missing_rules_and_force_refresh_reads_all(team):
    owner, worker, store_id, _ = setup_flow(team)
    url = f'/api/v1/amazon/stores/{store_id}/catalog'
    first = owner.post(url, json={'product_type':'MOUSE_PAD'}).json()
    endpoint, headers = lease(owner, worker, first['job_id'])
    finish(owner, endpoint, headers, {'definitions':{'NONE':definition()}})
    second = owner.post(url, json={'product_type':'MOUSE_PAD','variations':True})
    assert second.status_code == 200, second.text
    endpoint, headers = lease(owner, worker, second.json()['job_id'])
    snap = owner.get(endpoint+'/amazon-snapshot',headers=headers).json()
    assert snap['request']['levels'] == ['PARENT','CHILD']
    finish(owner,endpoint,headers,{'definitions':{p:definition(p,True) for p in ['PARENT','CHILD']}})
    cached = owner.post(url,json={'product_type':'MOUSE_PAD','variations':True}).json()
    assert cached['cached'] and not cached.get('job_id')
    rows = cached['metadata']['definitions']
    assert next(d for d in rows if d['parentage']=='NONE')['variation_themes']==[]
    assert all(d['variation_themes']==['SIZE/COLOR'] for d in rows if d['parentage']!='NONE')
    refreshed = owner.post(url,json={'product_type':'MOUSE_PAD','variations':True,'refresh':True}).json()
    with SessionLocal() as db:
        assert loads(db.get(Job,refreshed['job_id']).payload_json,{})['levels']==['NONE','PARENT','CHILD']


def test_parent_required_copy_is_retained_without_a_buyable_offer():
    d=definition('PARENT',True)
    d['schema']['required']+=['bullet_point','product_description']
    snap={'brand':'Generic','product_type':'MOUSE_PAD','parent_sku':'family','variation_theme':'SIZE/COLOR'}
    body=make_body(snap,{},content(),d,parent=True)
    assert not validate_payload(body,d)
    assert body['attributes']['bullet_point'] and body['attributes']['product_description']
    assert not {'purchasable_offer','fulfillment_availability','merchant_shipping_group','main_product_image_locator'} & body['attributes'].keys()


def test_parent_omits_child_only_fields_disallowed_by_its_real_schema():
    d=definition('PARENT',True)
    del d['schema']['properties']['brand']
    d['schema']['required']=['item_name','bullet_point','product_description']
    d['schema']['additionalProperties']=False
    snap={'brand':'Generic','product_type':'MOUSE_PAD','parent_sku':'family','variation_theme':'SIZE/COLOR',
          'attributes':{'material':field('Polyester'),'country_of_origin':field('US')}}
    body=make_body(snap,{},content(),d,parent=True)
    assert not validate_payload(body,d)
    assert 'material' not in body['attributes'] and 'brand' not in body['attributes']
    assert body['attributes']['country_of_origin']==field('US')


def test_real_theme_schema_excludes_marketplace_id_and_unknown_msrp_is_zero():
    d=definition('CHILD',True)
    theme=d['schema']['properties']['variation_theme']['items']
    theme.update(required=['name'],additionalProperties=False)
    d['schema']['properties']['list_price']={'type':'array','items':{
        'type':'object','required':['currency','value'],'additionalProperties':False,
        'properties':{'currency':{'enum':['USD']},'value':{'type':'number','minimum':0,
            'description':'If you are unable to provide a value, enter 0.'}}}}
    d['schema']['allOf']=[{'if':{'required':['purchasable_offer']},'then':{'required':['list_price']}}]
    snap={'brand':'Generic','quantity':100,'handling_days':3,'shipping_template':'free-template',
          'parent_sku':'family','product_type':'MOUSE_PAD','variation_theme':'SIZE/COLOR'}
    item={'pricing':{'price':'27.43'},'images':['https://image.example/main.jpg']}
    body=make_body(snap,item,content(),d)
    assert not validate_payload(body,d)
    assert body['attributes']['variation_theme']==[{'name':'SIZE/COLOR'}]
    assert body['attributes']['list_price']==[{'currency':'USD','value':0}]
    assert body['attributes']['purchasable_offer'][0]['our_price'][0]['schedule'][0]['value_with_tax']==27.43
    # Explicit human MSRP remains possible, but Codex cannot write a price.
    snap['attributes']={'list_price':[{'currency':'USD','value':35}]}
    assert make_body(snap,item,content(),d)['attributes']['list_price'][0]['value']==35
    import pytest
    from erp.core.errors import AppError
    with pytest.raises(AppError):make_body(snap,item,{**content(),'attributes':{'list_price':[{'value':35}]}},d)


def test_preview_valid_receipt_succeeds_but_acceptance_or_errors_do_not(team,monkeypatch):
    owner,worker,batch,sku=prepared_flow(team,monkeypatch)
    url=f'/api/v1/amazon/batches/{batch}'
    for status,issues,expected in [('VALID',[],'validated'),('ACCEPTED',[],'validation_failed'),
                                 ('VALID',[{'severity':'ERROR','code':'8560'}],'validation_failed')]:
        result=owner.post(url+'/validate').json()
        endpoint,headers=lease(owner,worker,result['job_id'])
        assert finish(owner,endpoint,headers,{'items':[{'sku':sku,'response':{'status':status,'issues':issues}}]})['status']==expected


def test_refresh_unsubmitted_legacy_brand_preserves_copy_and_invalidates_preview(team,monkeypatch):
    owner,worker,batch,sku=prepared_flow(team,monkeypatch)
    url=f'/api/v1/amazon/batches/{batch}'
    before=owner.get(url).json()
    assert before['snapshot']['brand']=='generic'
    assert owner.get('/api/v1/amazon/pricing').json()['brand']=='generic'
    # A batch prepared by the previous version used capitalized Generic.
    with SessionLocal() as db:
        row=db.get(AmazonListingBatch,batch)
        snapshot=loads(row.snapshot_json,{})
        snapshot['brand']='Generic'
        row.snapshot_json=dumps(snapshot)
        draft=loads(row.draft_json,{})
        draft['bodies'][0]['body']['attributes']['brand']=field('Generic',language_tag='en_US')
        row.draft_json=dumps(draft)
        row.status='validated'
        row.result_json=dumps({'validated_digest':digest(draft)})
        db.commit()
    old_digest=owner.get(url).json()['result']['validated_digest']
    result=owner.post(url+'/refresh-basis')
    assert result.status_code==200,result.text
    after=owner.get(url).json()
    assert after['status']=='ready' and after['snapshot']['brand']=='generic'
    assert after['draft']['bodies'][0]['body']['attributes']['brand'][0]['value']=='generic'
    assert after['draft']['content']==before['draft']['content']
    assert after['snapshot']['quantity']==before['snapshot']['quantity']
    assert after['items'][0]['pricing']==before['items'][0]['pricing']
    assert not after['result']
    assert owner.post(url+'/submit',json={'draft_digest':old_digest}).status_code==409


def test_amazon_async_review_waits_without_allowing_edits_or_resubmission(team,monkeypatch):
    owner,worker,batch,sku=validated_flow(team,monkeypatch)
    url=f'/api/v1/amazon/batches/{batch}'
    draft_digest=owner.get(url).json()['result']['validated_digest']
    job=owner.post(url+'/submit',json={'draft_digest':draft_digest}).json()
    endpoint,headers=lease(owner,worker,job['job_id'])
    finish(owner,endpoint,headers,{'items':[{'sku':sku,'response':{'status':'ACCEPTED'}}]})
    review={'severity':'ERROR','code':'100521','message':'We are reviewing this listing. Please allow up to 48 hours.'}
    for issues,expected in [([review],'reviewing'),([review,{'severity':'ERROR','code':'8560'}],'issues')]:
        job=owner.post(url+'/reconcile')
        assert job.status_code==200,job.text
        endpoint,headers=lease(owner,worker,job.json()['job_id'])
        value=finish(owner,endpoint,headers,{'items':[{'sku':sku,'response':{'summaries':[],'issues':issues}}]})
        assert value['status']==expected
        state=owner.get(url).json()
        assert state['items'][0]['status']==expected and not state['repairable']
        assert owner.post(url+'/submit',json={'draft_digest':draft_digest}).status_code==409
        assert owner.post(url+'/refresh-basis').status_code==409
        assert owner.patch(url+'/facts',json={'content':{sku:content()}}).status_code==409
    job=owner.post(url+'/reconcile').json()
    endpoint,headers=lease(owner,worker,job['job_id'])
    value=finish(owner,endpoint,headers,{'items':[{'sku':sku,'response':{
        'summaries':[{'marketplaceId':'ATVPDKIKX0DER','asin':'TESTASIN','status':['BUYABLE']}],'issues':[]}}]})
    assert value['status']=='published'
