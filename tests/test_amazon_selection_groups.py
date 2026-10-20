from uuid import uuid4

import pytest
from sqlalchemy import select, func

from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps, loads
from erp.models import (AmazonListingBatch, AmazonListingItem, CatalogProduct,
                        CatalogVariant, CatalogProductAsset, Job, Store, SystemSetting)
from erp.services import amazon_listings as listings
from erp.integrations.amazon import AmazonUSClient
from test_personal_job_controls import team
from test_amazon_listing_flow import setup_flow, definition


def setup_groups(team, monkeypatch, count=100, missing_none=False):
    owner,worker,store_id,request=setup_flow(team)
    monkeypatch.setattr(AmazonUSClient,'request',lambda *a,**k:pytest.fail('Grouping must not call Amazon'))
    r=owner.post('/api/v1/execution-devices/local/register',json={
        'id':worker,'name':'PC','amazon_version':1,'codex_generation_version':4})
    assert r.status_code==200,r.text
    with SessionLocal() as db:
        source=db.get(CatalogProduct,request['items'][0]['product_id'])
        asset_id=db.scalar(select(CatalogProductAsset.asset_id).where(CatalogProductAsset.product_id==source.id))
        for n in range(2,count+1):
            sku=f'S2B-{n:06d}-01'
            p=CatalogProduct(sku=sku,name='US printed mouse pad',source_type='pod_amazon',production_json=source.production_json)
            db.add(p);db.flush()
            v=CatalogVariant(product_id=p.id,sku=sku,size='40x80cm',color=f'Pattern {n}',supplier_sku_id='3')
            db.add(v);db.flush();db.add(CatalogProductAsset(product_id=p.id,variant_id=v.id,asset_id=asset_id,role='main'))
            request['items'].append({'product_id':p.id,'variant_id':v.id})
        for level in ['PARENT','CHILD']+([] if missing_none else ['NONE']):
            db.add(SystemSetting(key=listings.cache_key(listings.store(db,store_id),'MOUSE_PAD',level),
                value_json=dumps(definition(level,True))))
        db.commit()
    request.update(generation_provider='codex',merge=True,merge_group_size=20,
                   variation_theme='SIZE/COLOR',request_id=uuid4().hex)
    return owner,worker,store_id,request


def counts():
    with SessionLocal() as db:
        return [db.scalar(select(func.count()).select_from(model)) for model in [AmazonListingBatch,AmazonListingItem]]


def test_product_categories_filter_before_paging_and_keep_other_options(team,monkeypatch):
    owner,worker,store_id,request=setup_groups(team,monkeypatch,count=23)
    with SessionLocal() as db:
        for n,item in enumerate(request['items']):
            product=db.get(CatalogProduct,item['product_id'])
            product.category='浴帘' if n<12 else 'Amazon 美国 POD'
            product.name='美国鼠标垫' if n>=12 else '美国浴帘'
        domestic=CatalogProduct(sku='CN-ONLY',source_type='pod',category='国内专用',name='国内商品')
        db.add(domestic);db.commit()
    params={'store_id':store_id,'category':'浴帘','page_size':10}
    a=owner.get('/api/v1/amazon/products',params=params).json()
    b=owner.get('/api/v1/amazon/products',params={**params,'page':2}).json()
    assert a['total']==b['total']==12
    assert len(a['items'])==10 and len(b['items'])==2
    assert a['category_options']==[{'label':'浴帘','count':12},{'label':'美国鼠标垫','count':11}]
    assert all(p['category']=='浴帘' and p['thumbnail_url'] for p in a['items'])
    fallback=owner.get('/api/v1/amazon/products',params={'category':'美国鼠标垫'}).json()
    assert fallback['total']==11 and all(p['category']=='美国鼠标垫' for p in fallback['items'])
    assert owner.get('/api/v1/amazon/products',params={'category':'不存在'}).json()['total']==0
    assert owner.get('/api/v1/amazon/products',params={'category':'国内专用'}).json()['total']==0
    # Classification never weakens same-store occupied-variant filtering.
    r=owner.post('/api/v1/amazon/batch-groups',json=request);assert r.status_code==201,r.text
    assert owner.get('/api/v1/amazon/products',params=params).json()['total']==0
    occupied=owner.get('/api/v1/amazon/products',params={**params,'listing_status':'occupied'}).json()
    assert occupied['total']==12 and all(p['available_count']==0 for p in occupied['items'])


def test_100_in_20_produces_5_unique_parents_and_replays_original_group(team,monkeypatch):
    owner,worker,store_id,request=setup_groups(team,monkeypatch)
    preview=owner.post('/api/v1/amazon/group-preview',json={k:v for k,v in request.items() if k!='request_id'})
    assert preview.status_code==200,preview.text
    assert preview.json()['link_count']==5 and [g['count'] for g in preview.json()['groups']]==[20]*5
    assert preview.json()['items'][0]['library_sku']=='S2B-000100-01'
    result=owner.post('/api/v1/amazon/batch-groups',json=request)
    assert result.status_code==201,result.text
    group=result.json();parents=[b['parent_sku'] for b in group['batches']]
    assert parents==[f'US-P-{n:06d}' for n in range(1,6)]
    assert counts()==[5,105]
    for batch in group['batches']:
        detail=owner.get('/api/v1/amazon/batches/'+batch['batch_id']).json()
        assert detail['status']=='waiting_codex' and len(detail['snapshot']['items'])==20
        assert detail['snapshot']['selection_group']['id']==request['request_id']
        assert len(detail['items'])==21
        bundle=owner.get('/api/v1/codex-handoffs/batches/'+batch['batch_id'])
        assert bundle.status_code==200 and batch['job_id'] in bundle.json()['prompt']
    replay=owner.post('/api/v1/amazon/batch-groups',json=request)
    assert replay.status_code==201 and replay.json()['replayed'] is True
    assert replay.json()['batch_ids']==group['batch_ids'] and counts()==[5,105]
    assert [b['parent_sku'] for b in replay.json()['batches']]==parents
    with SessionLocal() as db:
        assert loads(db.get(SystemSetting,'amazon_parent_sku_sequence').value_json,None)==5
    assert owner.get('/api/v1/amazon/batch-groups/'+request['request_id']).json()['batch_ids']==group['batch_ids']
    assert team[0]['other'].get('/api/v1/amazon/batch-groups/'+request['request_id']).status_code==409
    changed={**request,'quantity':123}
    assert owner.post('/api/v1/amazon/batch-groups',json=changed).status_code==409
    duplicate={**request,'request_id':uuid4().hex}
    assert owner.post('/api/v1/amazon/batch-groups',json=duplicate).status_code==409
    assert counts()==[5,105]


def test_pages_images_store_filter_and_changed_prefix_cannot_duplicate(team,monkeypatch):
    owner,worker,store_id,request=setup_groups(team,monkeypatch,count=23)
    path='/api/v1/amazon/products'
    a=owner.get(path,params={'store_id':store_id,'page_size':20}).json()
    b=owner.get(path,params={'store_id':store_id,'page_size':20,'page':2}).json()
    assert a['total']==23 and len(a['items'])==20 and a['has_more']
    assert b['total']==23 and len(b['items'])==3 and not b['has_more']
    assert a['items'][0]['sku']=='S2B-000023-01'
    assert a['items'][0]['thumbnail_url'].startswith('https://example-assets.')
    assert a['items'][0]['assets'][0]['asset']['name']=='display.jpg'
    assert not {p['id'] for p in a['items']} & {p['id'] for p in b['items']}
    assert 'source_snapshot' not in a['items'][0]
    r=owner.post('/api/v1/amazon/batch-groups',json=request);assert r.status_code==201,r.text
    assert owner.get(path,params={'store_id':store_id}).json()['total']==0
    occupied=owner.get(path,params={'store_id':store_id,'listing_status':'occupied'}).json()
    assert occupied['total']==23 and all(p['available_count']==0 for p in occupied['items'])
    with SessionLocal() as db:
        s=db.get(Store,store_id);s.offer_prefix='CHANGED-'
        other=Store(platform='amazon',name='Other US',external_account_id='OTHER',currency='USD',offer_prefix='OTHER-')
        db.add(other);db.commit();other_id=other.id
    assert owner.get(path,params={'store_id':other_id}).json()['total']==23
    changed={**request,'request_id':uuid4().hex}
    bad=owner.post('/api/v1/amazon/batch-groups',json=changed)
    assert bad.status_code==409 and '不能重复上架' in bad.text
    legacy={k:v for k,v in changed.items() if k not in {'merge','merge_group_size','request_id'}}
    assert owner.post('/api/v1/amazon/batches',json=legacy).status_code==409
    assert owner.get(path,params={'store_id':store_id}).json()['total']==0
    assert owner.get(path,params={'page':0}).status_code==422
    assert owner.get(path,params={'page_size':101}).status_code==422
    with SessionLocal() as db:
        p=db.get(CatalogProduct,request['items'][0]['product_id'])
        db.add(CatalogVariant(product_id=p.id,sku=p.sku+'-EXTRA',supplier_sku_id='3',size='another size'))
        db.commit()
    partial=owner.get(path,params={'store_id':store_id}).json()
    assert partial['total']==1 and partial['items'][0]['available_count']==1
    assert partial['items'][0]['occupied_count']==1


def test_last_singleton_is_standalone_and_late_failure_is_atomic(team,monkeypatch):
    owner,worker,store_id,request=setup_groups(team,monkeypatch,count=41,missing_none=True)
    preview=owner.post('/api/v1/amazon/group-preview',json={k:v for k,v in request.items() if k!='request_id'}).json()
    assert [g['count'] for g in preview['groups']]==[20,20,1]
    assert [g['merged'] for g in preview['groups']]==[True,True,False]
    failed=owner.post('/api/v1/amazon/batch-groups',json=request)
    assert failed.status_code==409,failed.text
    assert counts()==[0,0]
    with SessionLocal() as db:
        assert not db.scalar(select(Job).where(Job.type=='amazon_prepare_codex'))
        assert db.get(SystemSetting,'amazon_parent_sku_sequence') is None
        db.add(SystemSetting(key=listings.cache_key(listings.store(db,store_id),'MOUSE_PAD','NONE'),value_json=dumps(definition())))
        db.commit()
    created=owner.post('/api/v1/amazon/batch-groups',json=request)
    assert created.status_code==201,created.text
    assert [b['count'] for b in created.json()['batches']]==[20,20,1]
    assert created.json()['batches'][-1]['parent_sku']==''
    assert [b['parent_sku'] for b in created.json()['batches'][:2]]==['US-P-000001','US-P-000002']
    assert counts()==[3,43]


def test_parent_allocator_skips_occupied_skus_and_current_children(team,monkeypatch):
    from erp.services import amazon_selection
    owner,worker,store_id,request=setup_groups(team,monkeypatch,count=40)
    r=owner.post('/api/v1/amazon/batch-groups',json=request)
    assert r.status_code==201,r.text
    with SessionLocal() as db:
        # An older/imported SKU may collide with a new sequence. Never rename
        # or overwrite it, including if it belongs to another shop.
        other=Store(platform='amazon',name='Other',offer_prefix='US-')
        db.add(other);db.flush()
        db.get(SystemSetting,'amazon_parent_sku_sequence').value_json='0'
        reserved={'US-P-000003'}
        assert amazon_selection.parent_sku(db,other.id,reserved)=='US-P-000004'
        assert loads(db.get(SystemSetting,'amazon_parent_sku_sequence').value_json,None)==4
        assert 'US-P-000004' in reserved
        assert sorted(db.scalars(select(AmazonListingItem.sku).where(
            AmazonListingItem.sku.like('US-P-%'))))==['US-P-000001','US-P-000002']


@pytest.mark.parametrize('value',['"invalid"','-1','true'])
def test_invalid_parent_counter_aborts_without_partial_reservations(team,monkeypatch,value):
    owner,worker,store_id,request=setup_groups(team,monkeypatch,count=40)
    with SessionLocal() as db:
        db.add(SystemSetting(key='amazon_parent_sku_sequence',value_json=value));db.commit()
    r=owner.post('/api/v1/amazon/batch-groups',json=request)
    assert r.status_code==409 and '父 SKU 流水号异常' in r.text
    assert counts()==[0,0]
    with SessionLocal() as db:
        assert not db.scalar(select(Job).where(Job.type=='amazon_prepare_codex'))
        assert db.get(SystemSetting,'amazon_parent_sku_sequence').value_json==value


def test_same_products_can_be_grouped_again_for_another_store(team,monkeypatch):
    owner,worker,store_id,request=setup_groups(team,monkeypatch,count=40)
    first=owner.post('/api/v1/amazon/batch-groups',json=request)
    assert first.status_code==201,first.text
    with SessionLocal() as db:
        source=db.get(Store,store_id)
        other=Store(platform='amazon',name='Other US',external_account_id='OTHER',
                    currency='USD',offer_prefix='ZYH-',secrets_encrypted=source.secrets_encrypted)
        db.add(other);db.flush();other_id=other.id
        for level in ['PARENT','CHILD','NONE']:
            db.add(SystemSetting(key=listings.cache_key(listings.store(db,other_id),'MOUSE_PAD',level),
                                 value_json=dumps(definition(level,True))))
        db.commit()
    second_request={**request,'store_id':other_id,'request_id':uuid4().hex}
    second=owner.post('/api/v1/amazon/batch-groups',json=second_request)
    assert second.status_code==201,second.text
    assert [b['parent_sku'] for b in second.json()['batches']]==['ZYH-P-000003','ZYH-P-000004']
    assert counts()==[4,84]
    with SessionLocal() as db:
        # Four child groups reference the same forty library variants while
        # keeping each shop's publication records and Codex tasks separate.
        for target in [store_id,other_id]:
            variant_ids=set(db.scalars(select(AmazonListingItem.variant_id).where(
                AmazonListingItem.store_id==target,AmazonListingItem.variant_id.is_not(None))))
            assert variant_ids=={i['variant_id'] for i in request['items']}
        assert len(set(db.scalars(select(AmazonListingItem.sku).where(
            AmazonListingItem.variant_id.is_(None)))))==4
    for value in [request,second_request]:
        duplicate={**value,'request_id':uuid4().hex}
        bad=owner.post('/api/v1/amazon/batch-groups',json=duplicate)
        assert bad.status_code==409 and '不能重复上架' in bad.text
    assert counts()==[4,84]


def test_grouping_separates_sources_and_rejects_duplicate_selections(team,monkeypatch):
    owner,worker,store_id,request=setup_groups(team,monkeypatch,count=5)
    from erp.services import amazon_selection
    # Facts boundary is independently tested; here the planner must not merge
    # different supplier base products, even when quantity alone permits it.
    original=listings.preview
    def facts(db,value):
        snap=original(db,value)
        snap['items'][-1]['catalog_key']='s2b:another'
        return snap
    monkeypatch.setattr(listings,'preview',facts)
    with SessionLocal() as db:
        value=amazon_selection.group_preview(db,request)
    assert sorted(g['count'] for g in value['groups'])==[1,4] and value['link_count']==2
    request['items'].append(request['items'][0])
    assert owner.post('/api/v1/amazon/batch-groups',json=request).status_code==409
