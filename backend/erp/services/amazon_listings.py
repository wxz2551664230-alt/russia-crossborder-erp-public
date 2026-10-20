"""Cloud coordination and ledgers. Amazon/AI calls belong to desktop workers."""
from datetime import timezone
from uuid import uuid4
import re
from sqlalchemy import select, update
from erp.core.errors import AppError, ConflictError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.core.task_identity import task_identity
from erp.models import (AmazonListingBatch, AmazonListingItem, CatalogProduct, CatalogVariant,
                        Job, JobEvent, Store, SystemSetting, utc_now)
from erp.services import amazon_pricing as pricing
from erp.services.amazon_listing_payload import digest, schema_summary, make_body, validate_payload, validate_family
from erp.services.config_resolution import build_job_snapshot
from erp.services.publications import build_publication_snapshot
from erp.services.jobs import enqueue_job
from erp.domains.pod import us_freight

TYPES={'amazon_metadata','amazon_prepare','amazon_prepare_codex','amazon_validate','amazon_submit','amazon_reconcile'}
UNBRANDED_BRAND='generic'

def store(session,id):
    row=session.get(Store,id)
    if not row or row.platform!='amazon' or not row.enabled or row.currency!='USD':
        raise ConflictError('请选择已启用的 Amazon 美国站美元店铺')
    if not row.external_account_id:raise ConflictError('店铺未填写 Seller ID')
    cfg=loads(row.config_json,{})
    if cfg.get('marketplace_id') not in (None,'','ATVPDKIKX0DER'):
        raise ConflictError('当前功能只适用于美国站')
    return row

def scope(row):
    # Seller identity and credential changes invalidate seller-specific schemas.
    return digest({'id':row.id,'seller':row.external_account_id,'credentials':row.secrets_encrypted})

def cache_key(row,product_type,level):return 'amz_schema:'+digest([scope(row),product_type,level])

def cached_definition(session,row,product_type,level):
    item=session.get(SystemSetting,cache_key(row,product_type,level))
    if not item:raise ConflictError('请先读取并缓存所选类目的真实上架要求')
    return loads(item.value_json,{})

def definitions_for(session,row,checksums):
    snapshot=loads(row.snapshot_json,{})
    definitions={level:cached_definition(session,store(session,row.store_id),snapshot['product_type'],level) for level in checksums}
    if any(d['checksum']!=checksums[level] for level,d in definitions.items()):
        raise ConflictError('缓存类目已变化，请重新准备并预检')
    return definitions

def defaults(session):
    setting=session.get(SystemSetting,'amazon_us_pricing')
    return pricing.profile(loads(setting.value_json,{}) if setting else {})

def metadata(session,store_id,product_type=''):
    s=store(session,store_id)
    key='amz_types:'+scope(s)
    setting=session.get(SystemSetting,key)
    value=loads(setting.value_json,{}) if setting else {}
    definitions=[]
    if product_type:
        for level in ('NONE','PARENT','CHILD'):
            row=session.get(SystemSetting,cache_key(s,product_type,level))
            if row:definitions.append(schema_summary(loads(row.value_json,{})))
    return {'items':value.get('items',[]),'fetched_at':value.get('fetched_at'),'definitions':definitions}

def _lock_store(session,s,group_id=''):
    session.execute(update(Store).where(Store.id==s.id).values(enabled=Store.enabled))
    active=session.scalars(select(Job).where(Job.type.in_(TYPES),Job.status.in_(['queued','running','retrying','paused']))).all()
    for job in active:
        # Codex preparation never calls Amazon. Waiting material batches must not
        # block preparation or manual validation of another completed group.
        if job.type=='amazon_prepare_codex':continue
        if group_id and loads(job.payload_json,{}).get('selection_group_id')==group_id:continue
        if loads(job.payload_json,{}).get('store_id')==s.id:
            raise ConflictError('该店铺已有 Amazon 任务，请从原任务继续，避免并发请求')

def new_job(session,s,kind,payload,batch=None,*,commit=True,group_id=''):
    from erp.core.config import get_settings
    if not get_settings().multiuser_enabled:raise ConflictError('Amazon 业务只允许在已绑定的本机执行端运行')
    _lock_store(session,s,group_id)
    config=(build_publication_snapshot(session,s.id,{}) if kind=='amazon_prepare' else build_job_snapshot(session,store_id=s.id))
    job=enqueue_job(session,job_type=kind,payload={'store_id':s.id,'scope':scope(s),**payload},
        config_snapshot=config,idempotency_key=kind+':'+uuid4().hex,max_attempts=1,commit=False,
        steps=[('execute',{'amazon_metadata':'读取美国站类目与配送模板','amazon_prepare':'AI 准备英文文案与上架资料','amazon_prepare_codex':'Codex 准备英文文案与上架资料',
              'amazon_validate':'Amazon 预检（不发布）','amazon_submit':'提交美国站 FBM 商品','amazon_reconcile':'读取建品及变体状态'}[kind])])
    if batch:batch.job_id=job.id
    if kind=='amazon_prepare_codex':
        job.status='paused';job.pause_requested=True
        job.error_code='waiting_codex';job.error_message='请复制本批提示词，在原电脑完成 Codex 资料回填'
        if batch:batch.status='waiting_codex'
        session.add(JobEvent(job_id=job.id,message='等待 Codex 回填英文资料，不调用外部生成模型或平台'))
    if commit:session.commit()
    else:session.flush()
    return job

def metadata_job(session,store_id,request):
    s=store(session,store_id);product_type=request.get('product_type') or ''
    if product_type:
        if not re.fullmatch('[A-Z][A-Z0-9_]{0,79}',product_type):raise ConflictError('Amazon 类目编码无效')
        levels=['NONE','PARENT','CHILD'] if request.get('variations') else ['NONE']
        if not request.get('refresh'):
            missing=[]
            for level in levels:
                try:cached_definition(session,s,product_type,level)
                except ConflictError:missing.append(level)
            if not missing:
                return {'cached':True,'metadata':metadata(session,s.id,product_type)}
            # A manual request fills only missing parentage rules. Opening the
            # page remains cloud-cache-only; force refresh deliberately reads all.
            levels=missing
        payload={'operation':'definition','product_type':product_type,'levels':levels}
    else:
        keywords=str(request.get('keywords') or '').strip()
        if not keywords or len(keywords)>200:raise ConflictError('请填写类目搜索关键词')
        value=session.get(SystemSetting,'amz_types:'+scope(s))
        if value and not request.get('refresh') and loads(value.value_json,{}).get('keywords')==keywords:
            return {'cached':True,'metadata':metadata(session,s.id)}
        payload={'operation':'search','keywords':keywords}
    job=new_job(session,s,'amazon_metadata',payload)
    return {'cached':False,'job_id':job.id}

def accept_metadata(session,job,result):
    p=loads(job.payload_json,{});s=store(session,p['store_id'])
    if p['scope']!=scope(s):raise ConflictError('店铺授权已变化，请重新读取类目')
    if p['operation']=='test':
        connection=result.get('connection') or {}
        if connection.get('marketplace_id')!='ATVPDKIKX0DER' or connection.get('participating') is not True:
            raise ConflictError('未确认美国站参与状态')
        entries=[('amz_test:'+scope(s),{'connection':connection,'tested_at':utc_now().isoformat()})]
    elif p['operation']=='search':
        rows=result.get('items')
        if not isinstance(rows,list) or len(rows)>5000:raise ConflictError('类目结果无效')
        for row in rows:
            if not isinstance(row,dict) or not re.fullmatch('[A-Z][A-Z0-9_]{0,79}',str(row.get('name') or '')):raise ConflictError('类目编码无效')
        entries=[('amz_types:'+scope(s),{'items':rows,'keywords':p['keywords'],'fetched_at':utc_now().isoformat()})]
    else:
        definitions=result.get('definitions',{})
        if set(definitions)!=set(p['levels']):raise ConflictError('类目定义回执不完整')
        entries=[]
        for level,data in definitions.items():
            if data.get('product_type')!=p['product_type'] or data.get('parentage')!=level or not isinstance(data.get('schema'),dict) or not data.get('checksum'):raise ConflictError('类目定义与原任务不符')
            entries.append((cache_key(s,p['product_type'],level),{**data,'scope':scope(s),'fetched_at':utc_now().isoformat()}))
    for key,value in entries:
        item=session.get(SystemSetting,key)
        if not item:item=SystemSetting(key=key);session.add(item)
        item.value_json=dumps(value)

def product_facts(session,selection):
    product=session.get(CatalogProduct,selection['product_id']);variant=session.get(CatalogVariant,selection['variant_id'])
    if not product or product.source_type!='pod_amazon' or not variant or not variant.active or variant.product_id!=product.id:
        raise ConflictError('只能选择 Amazon 美国 POD 产品库的有效规格')
    production=loads(product.production_json,{})
    if production.get('market')!='US' or production.get('supplier')!='s2b' or not production.get('remote_product_id'):
        raise ConflictError('产品没有已核实的美国生产货源')
    key='s2b:'+str(production['product_type_id'])
    freight=us_freight.preview(session,key,int(variant.supplier_sku_id),selection.get('manual_freight_cny'))
    if not freight['available']:raise ConflictError(variant.sku+' 没有有效 GOFO 运费，请先在美国货盘读取')
    links=sorted(product.asset_links,key=lambda a:(a.position,a.created_at))
    images=[a.asset.oss_url for a in links if a.is_current and a.role not in ('source','artwork','print','production')
            and (a.variant_id in (None,variant.id)) and a.asset.mirror_status!='deleted'
            and a.asset.oss_url.startswith('https://example-assets.oss-cn-chengdu.aliyuncs.com/')]
    images=list(dict.fromkeys(images))
    order=selection.get('images')
    if order:
        if any(x not in images for x in order) or len(order)>10 or len(set(order))!=len(order):raise ConflictError('图片必须选自本产品已入 OSS 的展示图')
        images=order
    if not images:raise ConflictError(variant.sku+' 没有公开 OSS 展示图')
    return {'product_id':product.id,'variant_id':variant.id,'library_sku':variant.sku,'name':product.name,
        'catalog_key':key,'supplier_variant':variant.supplier_sku_id,'size':variant.size,'color':variant.color,
        'specification':variant.specification,'supplier_description':loads(product.source_snapshot_json,{}).get('product_description',{}),
        'images':images[:10],'freight':freight,'attributes':selection.get('attributes') or {},
        'facts_revision':digest([product.production_json,product.source_snapshot_json,variant.properties_json,images])}

def preview(session,request):
    s=store(session,request['store_id']);p=pricing.profile({**defaults(session),**(request.get('pricing') or {})})
    items=[];seen=set()
    for selection in request['items']:
        facts=product_facts(session,selection)
        sku=(s.offer_prefix or '')+facts['library_sku']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,39}',sku) or sku in seen:raise ConflictError('Amazon SKU 无效或重复（最多40字符）')
        seen.add(sku)
        facts.update(sku=sku,pricing=pricing.calculate(facts['freight']['purchase_cny'],facts['freight']['freight']['selected_cny'],request['fee_category'],p))
        items.append(facts)
    if not items:raise ConflictError('请选择具体上架规格')
    parent=str(request.get('parent_sku') or '').strip()
    if parent:
        if len(items)<2:raise ConflictError('合并链接至少选择两个可区分的子商品')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,39}',parent) or parent in seen:raise ConflictError('父 SKU 无效或与子商品相同')
        if len({x['catalog_key'] for x in items})!=1:raise ConflictError('只能合并同一美国货盘基础商品的不同图案或规格')
    return {'store_id':s.id,'store_name':s.name,'scope':scope(s),'generation_provider':request.get('generation_provider','ai'),'brand':UNBRANDED_BRAND,'gtin_exemption_declared':True,
        'product_type':request.get('product_type') or '', 'fee_category':request['fee_category'],
        'pricing':p,'items':items,'parent_sku':parent,'variation_theme':request.get('variation_theme') or '',
        'quantity':request.get('quantity',0),'handling_days':request.get('handling_days',3),
        'shipping_template':request.get('shipping_template') or loads(s.config_json,{}).get('merchant_shipping_group') or '',
        'attributes':request.get('attributes') or {},'keywords':request.get('keywords') or '',
        'free_shipping_confirmed':request.get('free_shipping_confirmed',False),
        'main_image_confirmed':request.get('main_image_confirmed',False)}

def get_batch(session,id,control=False):
    row=session.scalar(select(AmazonListingBatch).where(AmazonListingBatch.id==id).with_for_update())
    if not row:raise NotFoundError('Amazon 上架批次不存在')
    if row.status=='deleted':raise ConflictError('Amazon 批次已删除，请重新选择产品建立新批次')
    if control:
        actor=task_identity.get() or {}
        if row.created_by!=actor.get('id') or row.worker_id!=actor.get('worker_id'):
            raise ConflictError('请在发起本批次的账号和原电脑继续')
    return row

def edit_facts(session,id,request):
    row=get_batch(session,id,True)
    can_repair=repairable(session,row)
    if row.status not in {'needs_facts','ready','validated','validation_failed'} and not can_repair:
        raise ConflictError('只能修正尚未提交的上架资料')
    s=store(session,row.store_id);_lock_store(session,s)
    snap=loads(row.snapshot_json,{});draft=loads(row.draft_json,{})
    if snap['scope']!=scope(s):raise ConflictError('店铺授权已变化，请重新准备')
    unknown=set(request['item_attributes'])-{x['sku'] for x in snap['items']}
    if unknown:raise ConflictError('规格属性包含其他批次的 SKU')
    snap['attributes']=request['attributes'];snap['main_image_confirmed']=request['main_image_confirmed']
    for item in snap['items']:
        if item['sku'] in request['item_attributes']:item['attributes']=request['item_attributes'][item['sku']]
    row.snapshot_json=dumps(snap)
    content={**draft.get('content',{}),**request['content']}
    if can_repair:
        for item in session.scalars(select(AmazonListingItem).where(AmazonListingItem.batch_id==row.id)):
            old=loads(item.receipt_json,{})
            item.receipt_json=dumps({'history':old.get('history',[])+[{k:v for k,v in old.items() if k!='history'}]}) if old.get('intent') else '{}'
            item.status='draft'
    # Original preparation identity and definitions stay bound to the batch.
    from types import SimpleNamespace
    result=accept_prepared(session,SimpleNamespace(id=row.job_id,payload_json=dumps({
        'batch_id':row.id,'scope':snap['scope'],'definition_checksums':draft['definition_checksums']})),{'content':content})
    row.result_json='{}';session.commit();return result

def repairable(session,row):
    if row.status!='issues':return False
    # Only explicit rejected/non-started creates may be corrected and submitted.
    # Accepted or unknown writes retain their original intent and require reads.
    items=session.scalars(select(AmazonListingItem).where(AmazonListingItem.batch_id==row.id)).all()
    return bool(items) and all(i.status=='draft' or
        (i.status=='issues' and not i.asin and loads(i.receipt_json,{}).get('response',{}).get('status')=='INVALID') for i in items)

def refresh_basis(session,id):
    row=get_batch(session,id,True)
    if row.status not in {'needs_facts','ready','validation_failed','validated'}:
        raise ConflictError('已提交或结果未知的商品只能核对原提交，不能替换定价依据')
    s=store(session,row.store_id);_lock_store(session,s)
    old=loads(row.snapshot_json,{});draft=loads(row.draft_json,{})
    request={k:old[k] for k in ('store_id','product_type','fee_category','pricing','parent_sku','variation_theme',
        'quantity','handling_days','shipping_template','attributes','keywords','free_shipping_confirmed','main_image_confirmed')}
    request['items']=[{'product_id':i['product_id'],'variant_id':i['variant_id'],'images':i['images'],'attributes':i['attributes'],
        'manual_freight_cny':i['freight']['freight']['selected_cny'] if i['freight']['freight']['manual'] else None} for i in old['items']]
    snap=preview(session,request)
    snap['generation_provider']=old.get('generation_provider','ai')
    if [i['sku'] for i in snap['items']]!=[i['sku'] for i in old['items']]:
        raise ConflictError('店铺 SKU 前缀已变化，不能替换原批次身份')
    for new,previous in zip(snap['items'],old['items']):new['previous_copy']=previous.get('previous_copy',[])
    levels=['PARENT','CHILD'] if snap['parent_sku'] else ['NONE']
    checksums={level:cached_definition(session,s,snap['product_type'],level)['checksum'] for level in levels}
    row.snapshot_json=dumps(snap)
    for item in session.scalars(select(AmazonListingItem).where(AmazonListingItem.batch_id==row.id)):
        facts=next((i for i in snap['items'] if i['sku']==item.sku),None)
        if facts:item.pricing_json=dumps(facts['pricing'])
    from types import SimpleNamespace
    accepted=accept_prepared(session,SimpleNamespace(id=row.job_id,payload_json=dumps({
        'batch_id':row.id,'scope':snap['scope'],'definition_checksums':checksums})),{'content':draft.get('content',{})})
    row.result_json='{}';session.commit();return accepted

def record_intent(session,job,value):
    p=loads(job.payload_json,{})
    row=get_batch(session,p['batch_id'])
    draft=loads(row.draft_json,{})
    if row.job_id!=job.id or p['draft_digest']!=digest(draft):raise ConflictError('写入依据已变化')
    body=next((x for x in draft['bodies'] if x['sku']==value['sku']),None)
    if not body or digest(body['body'])!=value['body_digest']:raise ConflictError('提交内容与已核验资料不一致')
    item=session.scalar(select(AmazonListingItem).where(AmazonListingItem.batch_id==row.id,AmazonListingItem.sku==value['sku']))
    if item.status in {'processing','published','unknown','issues'}:raise ConflictError('该 SKU 已提交或结果待核对，不允许重发')
    receipt=loads(item.receipt_json,{})
    if receipt.get('intent'):
        raise ConflictError('该 SKU 已记录写入意图，请先读取原任务平台结果，不能重复提交')
    item.receipt_json=dumps({**receipt,'intent':{'job_id':job.id,'body_digest':value['body_digest'],'created_at':utc_now().isoformat()}})
    item.status='submitting'
    return {'authorized':True,'sku':item.sku}

def batch_dict(session,row):
    job=session.get(Job,row.job_id) if row.job_id else None
    items=session.scalars(select(AmazonListingItem).where(AmazonListingItem.batch_id==row.id).order_by(AmazonListingItem.created_at)).all()
    return {'id':row.id,'store_id':row.store_id,'status':row.status,'job_id':row.job_id,
        'job_status':job.status if job else None,'error':row.last_error or (job.error_message if job and job.status=='failed' else ''),
        'created_at':row.created_at.isoformat(),'repairable':repairable(session,row),'snapshot':loads(row.snapshot_json,{}),
        'draft':loads(row.draft_json,{}),'result':loads(row.result_json,{}),
        'items':[{'id':i.id,'sku':i.sku,'parent_sku':i.parent_sku,'status':i.status,'asin':i.asin,
                  'body':loads(i.body_json,{}),'pricing':loads(i.pricing_json,{}),'receipt':loads(i.receipt_json,{})} for i in items]}

def prepare(session,request,*,commit=True,group_id='',snapshot_override=None):
    try:return _prepare(session,request,commit=commit,group_id=group_id,snapshot_override=snapshot_override)
    except Exception:
        session.rollback()
        raise


def _prepare(session,request,*,commit=True,group_id='',snapshot_override=None):
    snapshot=snapshot_override if snapshot_override is not None else preview(session,request)
    s=store(session,snapshot['store_id'])
    session.execute(update(Store).where(Store.id==s.id).values(enabled=Store.enabled))
    from erp.services.amazon_selection import ensure_available
    ensure_available(session,s,snapshot['items'])
    if not snapshot['product_type'] or not snapshot['shipping_template']:
        raise ConflictError('请人工选定类目及店铺真实配送模板')
    if not snapshot['free_shipping_confirmed']:
        raise ConflictError('请确认配送模板的买家运费与本次定价参数一致')
    levels=['PARENT','CHILD'] if snapshot['parent_sku'] else ['NONE']
    definitions={level:cached_definition(session,s,snapshot['product_type'],level) for level in levels}
    if snapshot['parent_sku'] and any(snapshot['variation_theme'] not in schema_summary(v)['variation_themes'] for v in definitions.values()):
        raise ConflictError('请选择当前类目允许的变体主题')
    templates=schema_summary(definitions['CHILD' if snapshot['parent_sku'] else 'NONE'])['shipping_templates']
    if templates and snapshot['shipping_template'] not in templates:raise ConflictError('配送模板不属于该店铺，请重新选择')
    for item in snapshot['items']:
        prior=session.execute(select(AmazonListingItem.sku,AmazonListingBatch.draft_json)
            .join(AmazonListingBatch,AmazonListingBatch.id==AmazonListingItem.batch_id)
            .where(AmazonListingItem.product_id==item['product_id'],AmazonListingItem.variant_id==item['variant_id'])
            .order_by(AmazonListingItem.created_at.desc()).limit(10)).all()
        item['previous_copy']=[loads(raw,{}).get('content',{}).get(sku,{}) for sku,raw in prior]
    actor=task_identity.get() or {}; row=AmazonListingBatch(store_id=s.id,created_by=actor.get('id',''),worker_id=actor.get('worker_id',''),snapshot_json=dumps(snapshot))
    session.add(row);session.flush()
    requested=[i['sku'] for i in snapshot['items']]+([snapshot['parent_sku']] if snapshot['parent_sku'] else [])
    session.execute(update(Store).where(Store.id==s.id).values(enabled=Store.enabled))
    if session.scalar(select(AmazonListingItem).where(AmazonListingItem.store_id==s.id,AmazonListingItem.sku.in_(requested))):
        raise ConflictError('本店铺已有这些 SKU 的上架记录，请继续原批次')
    for item in snapshot['items']:
        session.add(AmazonListingItem(batch_id=row.id,store_id=s.id,product_id=item['product_id'],variant_id=item['variant_id'],
            sku=item['sku'],parent_sku=snapshot['parent_sku'],pricing_json=dumps(item['pricing'])))
    if snapshot['parent_sku']:session.add(AmazonListingItem(batch_id=row.id,store_id=s.id,sku=snapshot['parent_sku']))
    kind='amazon_prepare_codex' if snapshot['generation_provider']=='codex' else 'amazon_prepare'
    job=new_job(session,s,kind,{'batch_id':row.id,'generation_provider':snapshot['generation_provider'],
        'definition_checksums':{k:v['checksum'] for k,v in definitions.items()},
        **({'selection_group_id':group_id} if group_id else {})},row,commit=commit,group_id=group_id)
    return {'batch_id':row.id,'job_id':job.id}

def operation(session,id,kind,expected_digest=''):
    row=get_batch(session,id,True);s=store(session,row.store_id);snap=loads(row.snapshot_json,{})
    if snap['scope']!=scope(s):raise ConflictError('店铺身份或授权已变，请重新准备')
    if kind in {'amazon_validate','amazon_submit'}:
        if row.status not in {'ready','validation_failed','validated'}:raise ConflictError('请先完成资料准备及修正')
        if kind=='amazon_submit':
            receipt=loads(row.result_json,{})
            if row.status!='validated' or receipt.get('validated_digest')!=expected_digest or expected_digest!=digest(loads(row.draft_json,{})):
                raise ConflictError('提交资料已变化或尚未通过平台预检，请重新预检')
            if not snap['main_image_confirmed']:raise ConflictError('请核验主图符合 Amazon 图片规则后再提交')
        for item in snap['items']:
            if item['pricing']['fee_version']!=pricing.VERSION:raise ConflictError('官方费率表已更新，请更新定价依据并重新预检')
            current=product_facts(session,{'product_id':item['product_id'],'variant_id':item['variant_id'],'images':item['images'],
                                          'manual_freight_cny':float(item['freight']['freight']['selected_cny']) if item['freight']['freight']['manual'] else None})
            if current['facts_revision']!=item['facts_revision'] or digest(current['freight'])!=digest(item['freight']):
                raise ConflictError('产品资料或运费规则已变化，请重新准备，不能沿用旧售价')
    if kind=='amazon_reconcile' and row.status not in {'submitted','processing','reviewing','published','created_no_stock','issues','unknown','submitting'}:
        raise ConflictError('本批次还未提交，不能核对发布状态')
    job=new_job(session,s,kind,{'batch_id':row.id,'draft_digest':digest(loads(row.draft_json,{}))},row)
    if kind=='amazon_submit':row.status='submitting';session.commit()
    return {'job_id':job.id,'batch_id':row.id}

def accept_prepared(session,job,result):
    p=loads(job.payload_json,{});row=get_batch(session,p['batch_id']);snapshot=loads(row.snapshot_json,{})
    if row.job_id!=job.id or p['scope']!=scope(store(session,row.store_id)):raise ConflictError('准备结果与当前任务不符')
    content=result.get('content') or {};bodies=[];errors=[]
    definitions=definitions_for(session,row,p['definition_checksums'])
    for item in snapshot['items']:
        definition=definitions['CHILD' if snapshot['parent_sku'] else 'NONE']
        value=content.get(item['sku']) or {}
        normalize=lambda text:' '.join(str(text or '').lower().split())
        if any(normalize(value.get('description')) and normalize(value.get('description'))==normalize(old.get('description')) for old in item.get('previous_copy',[])):
            errors.append({'sku':item['sku'],'path':'description','message':'新上架文案与历史店铺文案相同，请重新编写'})
        try:body=make_body(snapshot,item,value,definition)
        except AppError as exc:
            errors.append({'sku':item['sku'],'path':'content','message':str(exc)});continue
        errors.extend([{'sku':item['sku'],**e} for e in validate_payload(body,definition)])
        bodies.append({'sku':item['sku'],'body':body})
    if snapshot['parent_sku']:
        try:
            body=make_body(snapshot,{},content.get(snapshot['parent_sku']) or {},definitions['PARENT'],parent=True)
            errors.extend([{'sku':snapshot['parent_sku'],**e} for e in validate_payload(body,definitions['PARENT'])])
            bodies.insert(0,{'sku':snapshot['parent_sku'],'body':body})
            validate_family(snapshot,bodies)
        except AppError as exc:errors.append({'sku':snapshot['parent_sku'],'path':'variation','message':str(exc)})
    row.draft_json=dumps({'content':content,'bodies':bodies,'errors':errors,'definition_checksums':p['definition_checksums']})
    row.status='needs_facts' if errors else 'ready'
    for item in session.scalars(select(AmazonListingItem).where(AmazonListingItem.batch_id==row.id)):
        item.body_json=dumps(next((x['body'] for x in bodies if x['sku']==item.sku),{}))
    return {'batch_id':row.id,'status':row.status,'errors':errors}

def accept_operation(session,job,result):
    p=loads(job.payload_json,{});row=get_batch(session,p['batch_id']);draft=loads(row.draft_json,{})
    if row.job_id!=job.id or p['draft_digest']!=digest(draft):raise ConflictError('回执与当前提交资料不一致')
    expected={i.sku:i for i in session.scalars(select(AmazonListingItem).where(AmazonListingItem.batch_id==row.id))}
    rows=result.get('items',[])
    if not isinstance(rows,list) or {x.get('sku') for x in rows}!=set(expected) or len(rows)!=len(expected):raise ConflictError('Amazon 回执项目不完整或重复')
    if job.type=='amazon_validate':
        # VALIDATION_PREVIEW returns VALID; ACCEPTED means a create was accepted
        # for processing and is not a successful validation-preview receipt.
        success=all(x.get('response',{}).get('status')=='VALID' and not x.get('rejected')
                    and not any(i.get('severity')=='ERROR' for i in x['response'].get('issues',[])) for x in rows)
        row.status='validated' if success else 'validation_failed'
        row.result_json=dumps({'validation':rows,'validated_digest':digest(draft) if success else ''})
    else:
        for result_item in rows:
            item=expected[result_item['sku']];response=result_item.get('response') or {}
            previous=loads(item.receipt_json,{})
            item.receipt_json=dumps({**result_item,**{k:previous[k] for k in ('intent','history') if k in previous}})
            if result_item.get('not_started'):item.status='draft';continue
            if result_item.get('unknown'):item.status='unknown';continue
            if job.type=='amazon_reconcile' and not response:
                item.status='unknown' if previous.get('intent') else 'draft';continue
            if job.type=='amazon_reconcile' and (previous.get('unknown') or item.status=='unknown'):
                submitted=loads(item.body_json,{}).get('attributes',{})
                if not submitted or any(response.get('attributes',{}).get(k)!=v for k,v in submitted.items()):
                    item.status='unknown';continue
            errors=[x for x in response.get('issues',[]) if x.get('severity')=='ERROR']
            # Amazon explicitly marks its asynchronous listing review as ERROR.
            # It asks the seller to wait, not to correct or repeat the create.
            if job.type=='amazon_reconcile' and response.get('status')!='INVALID' and errors and all(x.get('code')=='100521' for x in errors):
                item.status='reviewing';continue
            if response.get('status')=='INVALID' or errors:
                item.status='issues';continue
            summaries=response.get('summaries') or []
            summary=next((x for x in summaries if x.get('marketplaceId')=='ATVPDKIKX0DER'),{})
            item.asin=summary.get('asin') or item.asin
            # Real platform status and relationship, not HTTP acceptance, closes a listing.
            is_parent=item.sku==loads(row.snapshot_json,{}).get('parent_sku')
            live='DISCOVERABLE' if is_parent else 'BUYABLE'
            parents=set()
            for group in response.get('relationships',[]):
                for relation in group.get('relationships',[]):parents.update(relation.get('parentSkus',[]))
            relation_ok=not item.parent_sku or item.parent_sku in parents
            item.status=('published' if item.asin and live in summary.get('status',[]) and relation_ok else
                'created_no_stock' if item.asin and not is_parent and loads(row.snapshot_json,{})['quantity']==0
                and 'DISCOVERABLE' in summary.get('status',[]) and relation_ok else 'processing')
        states={i.status for i in expected.values()}
        row.status='unknown' if 'unknown' in states else 'issues' if states&{'issues','draft'} else 'reviewing' if 'reviewing' in states else 'published' if states=={'published'} else 'created_no_stock' if states<={'published','created_no_stock'} else 'processing'
        row.result_json=dumps({'items':rows,'checked_at':utc_now().isoformat()})
    return {'batch_id':row.id,'status':row.status}
