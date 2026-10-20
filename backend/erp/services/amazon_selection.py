"""Cloud-only product selection and atomic grouping; no marketplace calls."""
from copy import deepcopy
import re

from sqlalchemy import select, update, func, or_, exists
from sqlalchemy.orm import selectinload

from erp.core.errors import ConflictError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.core.task_identity import task_identity
from erp.domains.pod.product_categories import category_expression, category_label
from erp.models import (AmazonListingBatch, AmazonListingItem, CatalogProduct,
                        CatalogVariant, CatalogProductAsset, Store, SystemSetting)
from erp.serializers import catalog_variant_dict
from erp.services.amazon_listing_payload import digest


def ensure_available(session, store, items):
    skus=[item['sku'] for item in items]
    variants=[item['variant_id'] for item in items]
    if len(set(variants))!=len(variants):raise ConflictError('同一产品规格不能重复选择')
    prior=session.scalar(select(AmazonListingItem).where(AmazonListingItem.store_id==store.id,
        or_(AmazonListingItem.sku.in_(skus),AmazonListingItem.variant_id.in_(variants))))
    if prior:
        raise ConflictError(f'{prior.sku} 已在本店铺上架或进入上架批次，请继续原批次，不能重复上架')


def products(session, search='', page=1, page_size=50, store_id='', listing_status='available', category=''):
    from erp.services.amazon_listings import store as get_store
    target=get_store(session,store_id) if store_id else None
    query=select(CatalogProduct).where(CatalogProduct.source_type=='pod_amazon')
    if search:query=query.where(or_(CatalogProduct.sku.contains(search,autoescape=True),
                                   CatalogProduct.name.contains(search,autoescape=True)))
    occupied=exists(select(AmazonListingItem.id).where(AmazonListingItem.store_id==store_id,
        or_(AmazonListingItem.variant_id==CatalogVariant.id,
            AmazonListingItem.sku==(target.offer_prefix or '')+CatalogVariant.sku))) if target else None
    active=select(CatalogVariant.id).where(CatalogVariant.product_id==CatalogProduct.id,CatalogVariant.active.is_(True))
    if target and listing_status!='all':
        active=active.where(~occupied if listing_status=='available' else occupied)
    query=query.where(exists(active))
    # US imports initially used one broad library category. Until a real
    # subcategory is assigned, group by the original supplier product name.
    # Page reads never invent taxonomy or fetch a supplier/platform.
    product_category=category_expression()
    category_rows=session.execute(query.with_only_columns(product_category,func.count())
        .group_by(product_category).order_by(product_category)).all()
    if category:query=query.where(product_category==category)
    total=session.scalar(select(func.count()).select_from(query.subquery()))
    rows=session.scalars(query.options(selectinload(CatalogProduct.variants),
        selectinload(CatalogProduct.asset_links).selectinload(CatalogProductAsset.asset))
        .order_by(CatalogProduct.sku.desc(),CatalogProduct.id).offset((page-1)*page_size).limit(page_size)).all()
    variant_ids=[v.id for p in rows for v in p.variants]
    skus=[(target.offer_prefix or '')+v.sku for p in rows for v in p.variants] if target else []
    records=session.scalars(select(AmazonListingItem).where(AmazonListingItem.store_id==store_id,
        or_(AmazonListingItem.variant_id.in_(variant_ids),AmazonListingItem.sku.in_(skus)))).all() if target else []
    by_variant={r.variant_id:r for r in records};by_sku={r.sku:r for r in records}
    items=[]
    for p in rows:
        links=sorted((a for a in p.asset_links if a.is_current and a.role not in ('source','artwork','print','production')
            and a.asset.mirror_status!='deleted' and a.asset.oss_url.startswith('https://example-assets.oss-cn-chengdu.aliyuncs.com/')),
            key=lambda a:(a.role!='main',a.position,str(a.id)))
        assets=[{'id':a.id,'variant_id':a.variant_id,'role':a.role,'is_current':True,
                 'asset':{'name':a.asset.name,'oss_url':a.asset.oss_url,'mirror_status':a.asset.mirror_status}} for a in links]
        variants=[]
        for v in p.variants:
            if not v.active:continue
            record=by_variant.get(v.id) or by_sku.get((target.offer_prefix or '')+v.sku) if target else None
            variants.append({**catalog_variant_dict(v),'available':record is None,
                'listing':{'batch_id':record.batch_id,'status':record.status,'sku':record.sku,'asin':record.asin} if record else None})
        items.append({'id':p.id,'sku':p.sku,'name':p.name,'source_type':p.source_type,
            'category':category_label(p),
            'production':loads(p.production_json,{}),'variants':variants,'assets':assets,
            'thumbnail_url':assets[0]['asset']['oss_url'] if assets else '',
            'available_count':sum(v['available'] for v in variants),
            'occupied_count':sum(not v['available'] for v in variants)})
    return {'items':items,'total':total,'page':page,'page_size':page_size,'has_more':page*page_size<total,
            'category_options':[{'label':label,'count':count} for label,count in category_rows if label]}


def plan(session, request):
    from erp.services import amazon_listings as listings
    # One facts/pricing read per selected variant, reused by every child batch.
    snapshot=listings.preview(session,{**request,'parent_sku':''})
    ensure_available(session,listings.store(session,snapshot['store_id']),snapshot['items'])
    items=sorted(snapshot['items'],key=lambda i:i['library_sku'],reverse=True)
    size=request['merge_group_size'] if request['merge'] else 200
    buckets={}
    for item in items:buckets.setdefault(item['catalog_key'] if request['merge'] else '',[]).append(item)
    groups=[]
    for bucket in buckets.values():
        for offset in range(0,len(bucket),size):
            chunk=bucket[offset:offset+size]
            groups.append({'index':len(groups)+1,'count':len(chunk),'merged':request['merge'] and len(chunk)>1,
                           'catalog_key':chunk[0]['catalog_key'],'items':chunk})
    return snapshot,groups


def group_preview(session, request):
    snapshot,groups=plan(session,request)
    return {**snapshot,'items':[i for g in groups for i in g['items']],
        'groups':[{'index':g['index'],'count':g['count'],'merged':g['merged'],
                   'catalog_key':g['catalog_key'],'skus':[i['sku'] for i in g['items']]} for g in groups],
        'link_count':sum(1 if g['merged'] else g['count'] for g in groups),'batch_count':len(groups)}


def group_key(request_id):return 'amz_selection_group:'+request_id


def get_group(session, request_id):
    actor=task_identity.get() or {}
    entry=session.get(SystemSetting,group_key(request_id))
    if not entry:raise NotFoundError('Amazon 分组批次不存在')
    value=loads(entry.value_json,{})
    if value.get('created_by')!=actor.get('id') or value.get('worker_id')!=actor.get('worker_id'):
        raise ConflictError('请在原发起账号和电脑继续本组批次')
    rows={r.id:r for r in session.scalars(select(AmazonListingBatch).where(AmazonListingBatch.id.in_(value['batch_ids'])))}
    removed=[id for id in value['batch_ids'] if id not in rows or rows[id].status=='deleted']
    active=[{**b,'status':rows[b['batch_id']].status,'job_id':rows[b['batch_id']].job_id}
            for b in value['batches'] if b['batch_id'] not in removed]
    return {**value,'batch_ids':[b['batch_id'] for b in active],'batches':active,'removed_batch_ids':removed,
            'count':sum(b['count'] for b in active),'link_count':sum(1 if b['merged'] else b['count'] for b in active)}


def parent_sku(session, store_id, reserved):
    # Keep names readable across shops. A shared DB counter, rather than a
    # process-local counter or truncated UUID, protects concurrent allocations.
    if session.get_bind().dialect.name=='postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    key='amazon_parent_sku_sequence'
    session.execute(insert(SystemSetting).values(key=key,value_json='0')
                    .on_conflict_do_nothing(index_elements=['key']))
    counter=session.scalar(select(SystemSetting).where(SystemSetting.key==key)
                           .with_for_update().execution_options(populate_existing=True))
    number=loads(counter.value_json,None)
    if type(number) is not int or number<0:
        raise ConflictError('父 SKU 流水号异常，请检查配置；没有建立新批次')
    target=session.get(Store,store_id)
    prefix=re.sub(r'[^A-Za-z0-9_-]','',target.offer_prefix or target.name).strip('-_').upper()[:12] or 'AMZ'
    for _ in range(1000):
        number+=1
        candidate=f'{prefix}-P-{number:06d}'
        if len(candidate)>40:break
        if candidate not in reserved and not session.scalar(select(AmazonListingItem.id).where(
            AmazonListingItem.sku==candidate).limit(1)):
            counter.value_json=dumps(number)
            session.flush()
            reserved.add(candidate);return candidate
    raise ConflictError('父 SKU 分配失败，请稍后重试；没有建立新批次')


def prepare_group(session, request):
    try:return _prepare_group(session,request)
    except Exception:
        # Release SQLite's write lock before the middleware writes its audit
        # result; also remove every reservation if any later group fails.
        session.rollback()
        raise


def _prepare_group(session, request):
    from erp.services import amazon_listings as listings
    target=listings.store(session,request['store_id']);actor=task_identity.get() or {}
    # Serializes duplicate checking, idempotent replay and all reservations.
    session.execute(update(Store).where(Store.id==target.id).values(enabled=Store.enabled))
    fingerprint=digest(request);key=group_key(request['request_id'])
    entry=session.get(SystemSetting,key)
    if entry:
        value=get_group(session,request['request_id'])
        if value['removed_batch_ids']:raise ConflictError('原批次已被删除，请使用新的请求号重新建立批次')
        if value['request_digest']!=fingerprint:raise ConflictError('原分组请求已建立，不能用相同请求号更改商品或设置')
        session.commit()
        return {**value,'replayed':True}
    listings._lock_store(session,target)
    snapshot,groups=plan(session,request)
    reserved={i['sku'] for i in snapshot['items']};batch_ids=[];results=[]
    for group in groups:
        snap=deepcopy(snapshot);snap['items']=group['items']
        snap['parent_sku']=parent_sku(session,target.id,reserved) if group['merged'] else ''
        snap['variation_theme']=request['variation_theme'] if group['merged'] else ''
        snap['selection_group']={'id':request['request_id'],'index':group['index'],'total':len(groups),
                                 'merge_group_size':request['merge_group_size'],'merged':group['merged']}
        result=listings.prepare(session,request,commit=False,group_id=request['request_id'],snapshot_override=snap)
        batch_ids.append(result['batch_id'])
        results.append({**result,'count':group['count'],'parent_sku':snap['parent_sku'],'merged':group['merged']})
    value={'id':request['request_id'],'store_id':target.id,'created_by':actor.get('id',''),
           'worker_id':actor.get('worker_id',''),'request_digest':fingerprint,
           'batch_ids':batch_ids,'batches':results,'count':len(snapshot['items']),
           'link_count':sum(1 if g['merged'] else g['count'] for g in groups)}
    session.add(SystemSetting(key=key,value_json=dumps(value)))
    session.commit()
    return value
