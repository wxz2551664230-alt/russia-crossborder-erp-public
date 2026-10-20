"""Read-only platform reconciliation, executed exclusively on the origin PC."""
from collections import defaultdict
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select
from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps, loads
from erp.core.task_identity import task_identity
from erp.models import PublicationStore, WorkerNode, Job, JobEvent, utc_now
from erp.services.jobs import enqueue_job
from erp.services.publications import get_publication, publication_status, resolve_publication_snapshot
from erp.services.catalog_personal_publications import digest

STAGES = {'failed', 'import_uncertain', 'import_pending', 'import_submitted', 'stock_pending'}

def connection_key(child):
    config=resolve_publication_snapshot(child.settings_snapshot_json)
    # Pricing and image/AI settings do not affect this read-only Seller call.
    return digest({key:config.get(key) for key in ('platform','store_id','external_account_id','api_base','base_url','request_timeout','secrets')})

def revision(child):
    return digest([child.offer_id, child.external_task_id, child.external_product_id,
                   loads(child.assets_json, {}).get('import_item'), child.settings_snapshot_json])

def eligible(child):
    return child.status in STAGES and bool(child.external_task_id) and child.stock_status not in {'completed','skipped','processing','unknown'}

def require_fresh(child):
    value = loads(child.assets_json, {}).get('stock_scan', {})
    try:
        age = (utc_now() - datetime.fromisoformat(value['observed_at'])).total_seconds()
    except (KeyError, ValueError, TypeError):
        age = 100000
    if value.get('outcome') != 'ready' or value.get('revision') != revision(child) or not -60 <= age <= 300:
        raise ConflictError('请先扫描上架状态；仅平台已确认且5分钟内核对的商品可以添加库存')

def enqueue(session, entries=None, store_id='', refresh=False):
    actor = task_identity.get() or {}
    device = session.get(WorkerNode, actor.get('worker_id',''))
    if not device or device.owner_user_id != actor.get('id') or 'catalog_ozon_stock_scan' not in loads(device.capabilities_json, []):
        raise ConflictError('请连接0.6.71或更新版本的本人执行电脑')
    if entries is None:
        query = select(PublicationStore).where(PublicationStore.status.in_(STAGES))
        if store_id: query = query.where(PublicationStore.store_id == store_id)
        children = [c for c in session.scalars(query) if eligible(c)]
    else:
        if not 1 <= len(entries) <= 1000: raise ConflictError('扫描数量需为1至1000')
        children=[]
        for entry in entries:
            p=get_publication(session,entry['publication_id'])
            c=next((c for c in p.stores if c.store_id==entry['store_id']),None)
            if not c or not eligible(c):raise ConflictError('商品阶段已变化或原库存结果尚未核对')
            children.append(c)
    groups=defaultdict(list);cached=0
    for c in {c.id:c for c in children}.values():
        if not refresh:
            try:require_fresh(c);cached+=1;continue
            except ConflictError:pass
        groups[(c.store_id,connection_key(c))].append(c)
    ids=[]
    for group in groups.values():
        for offset in range(0,len(group),1000):
            chunk=group[offset:offset+1000]
            targets=[{'child_id':c.id,'publication_id':c.publication_id,'offer_id':c.offer_id,'revision':revision(c)} for c in sorted(chunk,key=lambda c:c.id)]
            payload={'kind':'listing_status','store_id':chunk[0].store_id,'targets':targets,'params':{}}
            existing=next((j for j in session.scalars(select(Job).where(Job.type=='catalog_ozon_options',Job.status.in_(['queued','running','retrying']))) if j.created_by==actor['id'] and j.target_worker_id==actor['worker_id'] and loads(j.payload_json,{})==payload),None)
            job=existing or enqueue_job(session,job_type='catalog_ozon_options',payload=payload,config_snapshot=chunk[0].settings_snapshot_json,idempotency_key='stock-scan:'+uuid4().hex,steps=[('read','库存前批量扫描上架状态')],commit=False)
            ids.append(job.id)
    session.commit()
    return {'job_ids':ids,'cached_count':cached,'product_count':len(children)}

def classify(row):
    statuses=row.get('statuses') or {}
    errors=[e for e in row.get('errors',[]) if str(e.get('level','')).lower()!='warning']
    if row.get('is_archived') or row.get('is_autoarchived'):return 'unavailable'
    if (errors or statuses.get('status_failed') or statuses.get('moderate_status') in {'declined','rejected','banned'} or statuses.get('validation_status') in {'failed','fail','error'}):return 'blocked'
    if (statuses.get('is_created') is True and statuses.get('validation_status')=='success' and statuses.get('moderate_status')=='approved' and str(row.get('id','')).isdigit() and int(row['id'])>0):return 'ready'
    return 'pending'

def accept(session,job,items):
    payload=loads(job.payload_json,{})
    targets=payload['targets'];offers={t['offer_id'] for t in targets}
    if not isinstance(items,list) or any(r.get('offer_id') not in offers for r in items) or len({r['offer_id'] for r in items})!=len(items):raise ConflictError('扫描回执货号不一致或重复')
    index={r['offer_id']:r for r in items};counts=defaultdict(int)
    for target in targets:
        child=session.scalar(select(PublicationStore).where(PublicationStore.id==target['child_id']).with_for_update().execution_options(populate_existing=True))
        if not child or child.store_id!=payload['store_id'] or revision(child)!=target['revision'] or not eligible(child):counts['skipped']+=1;continue
        row=index.get(target['offer_id']);outcome=classify(row) if row else 'not_found'
        observed=(row or {}).get('observed_at','')
        try:
            age=(utc_now()-datetime.fromisoformat(observed)).total_seconds()
            if not -60<=age<=300:raise ValueError()
        except (ValueError,TypeError):
            if row:raise ConflictError('扫描结果已过期，请重新只读扫描')
            observed=utc_now().isoformat()
        if outcome=='ready' and str(child.external_product_id).isdigit() and int(child.external_product_id)>0 and str(row['id'])!=str(child.external_product_id):outcome='id_conflict'
        assets=loads(child.assets_json,{})
        if outcome=='ready':
            child.external_product_id=str(row['id']);child.status='stock_pending';child.stock_status='pending';child.last_error=''
        else:
            child.last_error={'blocked':'平台审核或资料校验未通过','unavailable':'平台商品已归档，不可添加库存','not_found':'平台未返回该货号，请核验是否已删除','pending':'平台建品或审核仍在处理中','id_conflict':'平台商品ID与原记录不一致，请人工核验'}[outcome]
            child.status='failed' if outcome!='pending' else 'import_pending'
        assets['stock_scan']={'job_id':job.id,'outcome':outcome,'observed_at':observed,'revision':revision(child),'platform':row or {}}
        child.assets_json=dumps(assets)
        p=get_publication(session,child.publication_id);p.status=publication_status(p.stores)
        counts[outcome]+=1
    summary=dict(counts)
    session.add(JobEvent(job_id=job.id,message='库存前只读扫描完成：'+dumps(summary)))
    return summary
