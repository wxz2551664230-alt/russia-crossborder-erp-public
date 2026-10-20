from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Header, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session
from erp.core.database import get_db
from erp.core.errors import ConflictError
from erp.core.jsonutil import loads
from erp.models import AmazonListingBatch, CatalogProduct
from erp.services import amazon_listings as service, amazon_pricing as pricing

router=APIRouter(prefix='/amazon',tags=['amazon-us-fbm'])
local_router=APIRouter(prefix='/execution-devices',tags=['amazon-local-worker'])
KEYWORD_REFERENCE_MAX_CHARS=20000

class Strict(BaseModel):model_config=ConfigDict(extra='forbid')
class Selection(Strict):
    product_id:str=Field(pattern='^[a-f0-9]{32}$')
    variant_id:str=Field(pattern='^[a-f0-9]{32}$')
    manual_freight_cny:float|None=Field(default=None,ge=0,le=100000)
    images:list[str]=Field(default_factory=list,max_length=10)
    attributes:dict=Field(default_factory=dict)
class Preview(Strict):
    generation_provider:Literal['ai','codex']='ai'
    store_id:str=Field(pattern='^[a-f0-9]{32}$')
    items:list[Selection]=Field(min_length=1,max_length=200)
    product_type:str=Field(default='',max_length=80)
    fee_category:str=Field(max_length=80)
    pricing:dict=Field(default_factory=dict)
    parent_sku:str=Field(default='',max_length=40)
    variation_theme:str=Field(default='',max_length=100)
    shipping_template:str=Field(default='',max_length=100)
    quantity:int=Field(default=0,ge=0,le=100000)
    handling_days:int=Field(default=3,ge=1,le=30)
    attributes:dict=Field(default_factory=dict)
    # Seller-provided references are separate from the generated 249-byte search terms.
    keywords:str=Field(default='',max_length=KEYWORD_REFERENCE_MAX_CHARS)
    free_shipping_confirmed:bool=False
    main_image_confirmed:bool=False
class Metadata(Strict):
    keywords:str=Field(default='',max_length=200)
    product_type:str=Field(default='',max_length=80)
    variations:bool=False
    refresh:bool=False
class GroupPreview(Preview):
    items:list[Selection]=Field(min_length=1,max_length=1000)
    merge:bool=False
    merge_group_size:int=Field(default=20,ge=2,le=200)
class GroupPrepare(GroupPreview):
    request_id:str=Field(pattern='^[a-f0-9]{32}$')
class Submit(Strict):draft_digest:str=Field(pattern='^[a-f0-9]{64}$')
class Remove(Strict):batch_ids:list[Annotated[str,Field(pattern='^[a-f0-9]{32}$')]]=Field(min_length=1,max_length=1000)
class Result(Strict):result:dict
class Intent(Strict):
    sku:str=Field(min_length=1,max_length=40)
    body_digest:str=Field(pattern='^[a-f0-9]{64}$')
class Progress(Strict):
    current:int=Field(ge=0,le=1000)
    total:int=Field(ge=1,le=1000)
class Edit(Strict):
    attributes:dict=Field(default_factory=dict)
    item_attributes:dict=Field(default_factory=dict)
    content:dict=Field(default_factory=dict)
    main_image_confirmed:bool=False

@router.get('/pricing')
def get_pricing(db:Session=Depends(get_db)):
    return {'defaults':service.defaults(db),'commissions':pricing.options(),'brand':service.UNBRANDED_BRAND,'gtin_exemption_declared':True,
            'limits':{'keyword_reference_max_chars':KEYWORD_REFERENCE_MAX_CHARS}}

@router.get('/products')
def products(search:str='',page:int=Query(default=1,ge=1),page_size:int=Query(default=50,ge=1,le=100),
             store_id:str='',listing_status:Literal['available','occupied','all']='available',
             category:str=Query(default='',max_length=800),db:Session=Depends(get_db)):
    from erp.services.amazon_selection import products as selection_products
    return selection_products(db,search,page,page_size,store_id,listing_status,category)

@router.get('/stores/{store_id}/catalog')
def get_catalog(store_id:str,product_type:str='',db:Session=Depends(get_db)):return service.metadata(db,store_id,product_type)

@router.post('/stores/{store_id}/catalog')
def read_catalog(store_id:str,payload:Metadata,db:Session=Depends(get_db)):
    return service.metadata_job(db,store_id,payload.model_dump())

@router.post('/preview')
def preview(payload:Preview,db:Session=Depends(get_db)):return service.preview(db,payload.model_dump())

@router.post('/group-preview')
def grouped_preview(payload:GroupPreview,db:Session=Depends(get_db)):
    from erp.services.amazon_selection import group_preview
    return group_preview(db,payload.model_dump())

@router.post('/batch-groups',status_code=201)
def grouped_prepare(payload:GroupPrepare,db:Session=Depends(get_db)):
    from erp.services.amazon_selection import prepare_group
    return prepare_group(db,payload.model_dump())

@router.get('/batch-groups/{id}')
def grouped_detail(id:str,db:Session=Depends(get_db)):
    from erp.services.amazon_selection import get_group
    return get_group(db,id)

@router.post('/batches',status_code=201)
def prepare(payload:Preview,db:Session=Depends(get_db)):return service.prepare(db,payload.model_dump())

@router.get('/batches')
def batches(store_id:str='',status:str='',page:int=1,grouped:bool=False,db:Session=Depends(get_db)):
    if grouped:
        from erp.services.amazon_batches import list_grouped
        return list_grouped(db,store_id,status,max(1,page))
    query=select(AmazonListingBatch).where(AmazonListingBatch.status!='deleted')
    if store_id:query=query.where(AmazonListingBatch.store_id==store_id)
    if status:query=query.where(AmazonListingBatch.status==status)
    rows=db.scalars(query.order_by(AmazonListingBatch.created_at.desc()).offset((max(1,page)-1)*25).limit(25)).all()
    return {'items':[{'id':r.id,'store_id':r.store_id,'status':r.status,'job_id':r.job_id,
        'generation_provider':loads(r.snapshot_json,{}).get('generation_provider','ai'),
        'selection_group':loads(r.snapshot_json,{}).get('selection_group'),
        'parent_sku':loads(r.snapshot_json,{}).get('parent_sku',''),
        'created_at':r.created_at.isoformat(),'product_type':loads(r.snapshot_json,{}).get('product_type'),
        'count':len(loads(r.snapshot_json,{}).get('items',[]))} for r in rows]}

@router.post('/batches/remove')
def remove_batches(payload:Remove,db:Session=Depends(get_db)):
    from erp.services.amazon_batches import remove
    return remove(db,payload.batch_ids)

@router.delete('/batches/{id}')
def remove_batch(id:str,db:Session=Depends(get_db)):
    from erp.services.amazon_batches import remove
    return remove(db,[id])

@router.delete('/batch-groups/{id}')
def remove_group(id:str,db:Session=Depends(get_db)):
    from erp.services.amazon_batches import remove
    from erp.services.amazon_selection import get_group
    group=get_group(db,id)
    return remove(db,group['batch_ids']) if group['batch_ids'] else {'removed_batch_ids':[],'released_count':0}

@router.get('/batches/{id}')
def batch(id:str,db:Session=Depends(get_db)):return service.batch_dict(db,service.get_batch(db,id))

@router.patch('/batches/{id}/facts')
def edit(id:str,payload:Edit,db:Session=Depends(get_db)):
    return service.edit_facts(db,id,payload.model_dump())

@router.post('/batches/{id}/validate')
def validate(id:str,db:Session=Depends(get_db)):return service.operation(db,id,'amazon_validate')

@router.post('/batches/{id}/refresh-basis')
def refresh_basis(id:str,db:Session=Depends(get_db)):return service.refresh_basis(db,id)

@router.post('/batches/{id}/submit')
def submit(id:str,payload:Submit,db:Session=Depends(get_db)):
    return service.operation(db,id,'amazon_submit',payload.draft_digest)

@router.post('/batches/{id}/reconcile')
def reconcile(id:str,db:Session=Depends(get_db)):return service.operation(db,id,'amazon_reconcile')

def leased(worker_id,job_id,request,db,attempt):
    from erp.api.routes.execution_devices import local_job
    from erp.api.routes.workers import _leased_job
    job=local_job(worker_id,job_id,request,db)
    if job.type not in service.TYPES:raise ConflictError('不是 Amazon 本机任务')
    return _leased_job(db,worker_id,job_id,attempt)

@local_router.get('/local/{worker_id}/jobs/{job_id}/amazon-snapshot')
def snapshot(worker_id:str,job_id:str,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from erp.services.config_resolution import resolve_job_snapshot
    from erp.services.publications import resolve_publication_snapshot
    job=leased(worker_id,job_id,request,db,x_worker_attempt)
    p=loads(job.payload_json,{});s=service.store(db,p['store_id'])
    if service.scope(s)!=p['scope']:raise ConflictError('Amazon 店铺授权已变')
    config=(resolve_publication_snapshot(job.config_snapshot_json) if job.type=='amazon_prepare' else resolve_job_snapshot(job.config_snapshot_json))
    value={'operation':job.type,'request':p,'config':config,'worker_id':worker_id}
    if p.get('batch_id'):
        row=service.get_batch(db,p['batch_id'])
        if row.job_id!=job.id:raise ConflictError('本批次已有新任务')
        value['batch']=service.batch_dict(db,row)
        if job.type in {'amazon_prepare','amazon_prepare_codex'}:value['definitions']=service.definitions_for(db,row,p['definition_checksums'])
    db.commit()
    return JSONResponse(value,headers={'Cache-Control':'no-store'})

@local_router.post('/local/{worker_id}/jobs/{job_id}/amazon-intent')
def intent(worker_id:str,job_id:str,payload:Intent,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    job=leased(worker_id,job_id,request,db,x_worker_attempt)
    if job.type!='amazon_submit':raise ConflictError('此任务不能写入商品')
    result=service.record_intent(db,job,payload.model_dump());db.commit();return result

@local_router.post('/local/{worker_id}/jobs/{job_id}/amazon-progress')
def progress(worker_id:str,job_id:str,payload:Progress,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from erp.core.jsonutil import dumps
    job=leased(worker_id,job_id,request,db,x_worker_attempt)
    if payload.current>payload.total:raise ConflictError('进度数量无效')
    job.progress=min(99,100*payload.current/payload.total)
    for step in job.steps:
        step.progress=job.progress;step.details_json=dumps({'current':payload.current,'total':payload.total})
    db.commit();return {'progress':job.progress}

@local_router.post('/local/{worker_id}/jobs/{job_id}/amazon-result')
def result(worker_id:str,job_id:str,payload:Result,request:Request,db:Session=Depends(get_db),x_worker_attempt:int=Header(...)):
    from erp.core.jsonutil import dumps
    from erp.services.amazon_listing_payload import digest
    job=leased(worker_id,job_id,request,db,x_worker_attempt)
    recorded=loads(job.external_ref,{})
    if recorded.get('amazon_result_hash'):
        if recorded['amazon_result_hash']!=digest(payload.result):raise ConflictError('已有不同 Amazon 回执，不能覆盖')
        db.commit()
        return recorded['summary']
    if job.type=='amazon_metadata':
        service.accept_metadata(db,job,payload.result);value={'cached':True}
    elif job.type in {'amazon_prepare','amazon_prepare_codex'}:
        if job.type=='amazon_prepare_codex':
            receipt=(loads(job.payload_json,{}).get('codex_outputs') or {}).get(loads(job.payload_json,{})['store_id'],{})
            if not receipt or payload.result!={'content':receipt.get('copies')}:raise ConflictError('结果与已核验的 Codex 回填不一致')
        value=service.accept_prepared(db,job,payload.result)
    else:value=service.accept_operation(db,job,payload.result)
    value={k:v for k,v in value.items() if k!='errors'}
    job.external_ref=dumps({'amazon_result_hash':digest(payload.result),'summary':value})
    db.commit();return value
