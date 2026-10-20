"""Read-only batch presentation; original jobs and leases remain intact."""
from collections import Counter
from hashlib import sha256
from sqlalchemy import select
from sqlalchemy.orm import load_only, selectinload
from erp.models import Job, JobStep, Publication, PublicationStore, CatalogProduct, CatalogVariant, Store
from erp.core.jsonutil import loads
from erp.services.job_filters import job_module, MODULES

def overview(session,worker_id=''):
    query=select(Job)
    if worker_id:query=query.where(Job.target_worker_id==worker_id)
    jobs=session.scalars(query.options(load_only(Job.id,Job.type,Job.status,Job.payload_json,
        Job.created_by,Job.target_worker_id,Job.progress,Job.updated_at,Job.created_at,Job.error_message),
        selectinload(Job.steps).load_only(JobStep.id,JobStep.key,JobStep.name,JobStep.status,JobStep.progress))).all()
    pub_ids=[loads(j.payload_json,{}).get('publication_id') for j in jobs]
    contexts={}
    for pid,base_sku,variant_sku,store_id,store_name in session.execute(
        select(Publication.id,CatalogProduct.sku,CatalogVariant.sku,PublicationStore.store_id,Store.name)
        .outerjoin(CatalogProduct,Publication.product_id==CatalogProduct.id)
        .outerjoin(CatalogVariant,Publication.variant_id==CatalogVariant.id)
        .outerjoin(PublicationStore,PublicationStore.publication_id==Publication.id)
        .outerjoin(Store,PublicationStore.store_id==Store.id).where(Publication.id.in_([p for p in pub_ids if p]))):
        context=contexts.setdefault(pid,{'sku':variant_sku or base_sku or '', 'stores':[]})
        if store_id:context['stores'].append({'id':store_id,'name':store_name})
    groups={}
    from erp.services.amazon_batches import compact_rows
    amazon_ids=[loads(j.payload_json,{}).get('batch_id') for j in jobs if j.type=='amazon_prepare_codex']
    amazon_sizes={r['id']:r['count'] for r in compact_rows(session,ids=[id for id in amazon_ids if id])} if amazon_ids else {}
    for job in jobs:
        payload=loads(job.payload_json,{})
        if payload.get('amazon_batch_removed'):continue
        batch=str(payload.get('batch_id') or payload.get('read_group_id') or '')
        if payload.get('generation_provider')=='codex':
            batch=str(payload.get('codex_request_id') or payload.get('selection_group_id') or batch)
        identity=(job.type,str(payload.get('batch_phase') or ''),batch,job.created_by,job.target_worker_id) if batch else ('job',job.id)
        key=sha256(repr(identity).encode()).hexdigest()[:32]
        groups.setdefault(key,[]).append((job,payload))
    results=[]
    for key,members in groups.items():
        counts=Counter();work=0;progress=0;children=[];store_ids=set()
        for job,payload in members:
            entries=payload.get('batch_entries') or []
            context=contexts.get(payload.get('publication_id'),{})
            store_ids.update(s['id'] for s in context.get('stores',[]))
            store_ids.update(str(e.get('store_id') or payload.get('store_id') or '') for e in entries or [{}])
            size=len(entries) or (amazon_sizes.get(payload.get('batch_id'),1) if job.type=='amazon_prepare_codex' else
                len(payload.get('product_ids') or []) if payload.get('generation_provider')=='codex' and job.type.startswith('pod_wb_') else 1)
            size=max(1,size);work+=size
            complete=size if job.status=='completed' else sum(s.status=='completed' for s in job.steps if entries and s.key in {e.get('child_id') for e in entries})
            counts['completed']+=complete
            if job.status!='completed':counts[job.status]+=size-complete
            progress+=max(complete,size*max(0,min(100,job.progress))/100)
            children.append({'id':job.id,'type':job.type,'status':job.status,'progress':job.progress,'sku':context.get('sku') or payload.get('sku',''),
                'store_names':[s['name'] for s in context.get('stores',[])],
                'payload':payload,'updated_at':job.updated_at.isoformat(),'error_message':job.error_message,
                'steps':[{'id':s.id,'name':s.name,'status':s.status,'progress':s.progress} for s in job.steps]})
        if counts['running'] or counts['retrying']:status='running'
        elif counts['failed']:status='failed'
        elif counts['action_required']:status='action_required'
        elif counts['queued']:status='queued'
        elif counts['paused']:status='paused'
        elif counts['completed']==work:status='completed'
        else:status='cancelled'
        first=members[0][0]
        results.append({'id':key,'type':first.type,'business_module':job_module(first.type),'status':status,
            'wb_price_recovery':members[0][1].get('wb_price_recovery') if len(members)==1 else None,
            'progress':round(progress/work*100),'total_items':work,'job_count':len(members),
            'counts':dict(counts),'target_worker_id':first.target_worker_id,'updated_at':max(j.updated_at for j,_ in members).isoformat(),
            'children':children,'store_ids':list(store_ids),
            'search_text':' '.join(j.id+' '+j.type+' '+j.error_message+' '+j.payload_json+' '+str(contexts.get(p.get('publication_id'),{}).get('sku','')) for j,p in members)})
    return results

def list_overview(session,page,page_size,status,module,search,store_id,job_type,worker_id=''):
    groups=overview(session,worker_id)
    filtered=[g for g in groups if (not module or g['business_module']==module) and
        (not job_type or g['type']==job_type) and (not store_id or store_id in g['store_ids']) and
        (not search or search.casefold() in g['search_text'].casefold())]
    counts=Counter(g['status'] for g in filtered)
    selected=[g for g in filtered if not status or status=='all' or
        status=='unfinished' and g['status'] not in {'completed','cancelled'} or
        status=='active' and g['status'] in {'running','retrying'} or
        status=='attention' and g['status'] in {'failed','action_required','paused'} or g['status']==status]
    selected.sort(key=lambda g:g['updated_at'],reverse=True)
    if status=='unfinished':selected.sort(key=lambda g:{'running':0,'action_required':1,'failed':2,'queued':3,'paused':4}.get(g['status'],5))
    items=[{k:v for k,v in g.items() if k not in {'children','search_text'}} for g in selected[(page-1)*page_size:page*page_size]]
    return {'items':items,'total':len(selected),'page':page,'page_size':page_size,'status_counts':dict(counts),
        'modules':[{'key':k,'label':v[0]} for k,v in MODULES.items()]+[{'key':'other','label':'其他任务'}]}
