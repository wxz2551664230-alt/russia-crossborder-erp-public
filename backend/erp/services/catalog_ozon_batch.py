"""Batch coordination only; all Seller API calls remain on the originator PC."""
from uuid import uuid4
from collections import defaultdict
from sqlalchemy import select
from erp.models import PublicationStore, WorkerNode, JobEvent, utc_now
from erp.core.errors import ConflictError
from erp.core.jsonutil import loads, dumps
from erp.core.task_identity import task_identity
from erp.services.jobs import enqueue_job
from erp.services.publications import get_publication, publication_status, resolve_publication_snapshot
from erp.services.catalog_personal_publications import digest
from erp.schemas import PublicationStockRequest, PublicationPromotionRequest


def enqueue(session, phase, entries):
    if phase not in {'submit','stock','promotion'} or not 1 <= len(entries) <= 1000:
        raise ConflictError('批次类型或数量无效')
    actor = task_identity.get() or {}
    device = session.get(WorkerNode, actor.get('worker_id',''))
    if not device or 'catalog_ozon_batch' not in loads(device.capabilities_json,[]):
        raise ConflictError('请先将当前执行电脑升级到0.6.58并连接，才能执行Ozon批量请求')
    groups = defaultdict(list); seen = set()
    for entry in entries:
        publication = get_publication(session, entry['publication_id'])
        child = next((c for c in publication.stores if c.store_id == entry['store_id']),None)
        if not child or child.id in seen: raise ConflictError('商品不存在或重复选择')
        seen.add(child.id)
        child = session.scalar(select(PublicationStore).where(PublicationStore.id==child.id).with_for_update().execution_options(populate_existing=True))
        expected = 'ready_for_submit' if phase=='submit' else phase+'_pending'
        if child.status != expected: raise ConflictError('选中商品已不在该阶段，请刷新后核对')
        params = {}
        if phase=='submit':
            assets=loads(child.assets_json,{})
            if child.external_task_id or not assets.get('package_ready') or not isinstance(assets.get('import_item'),dict):
                raise ConflictError('商品尚未核验或已提交，禁止重复建品')
        else:
            if getattr(child,phase+'_status') in {'processing','unknown'}: raise ConflictError('原阶段正在处理或结果未知')
            if not str(child.external_product_id).isdigit() or int(child.external_product_id)<=0: raise ConflictError('Ozon商品编号未确认')
            if phase=='stock':
                from erp.services.catalog_ozon_stock_scan import require_fresh
                require_fresh(child)
            schema=PublicationStockRequest if phase=='stock' else PublicationPromotionRequest
            params=schema.model_validate(entry.get('params',{})).model_dump(mode='json')
            if phase=='promotion' and params['stock'] is None and 'ozon_promotion_unlimited' not in loads(device.capabilities_json,[]):
                raise ConflictError('无限促销数量需要将执行电脑更新至0.6.82并重新连接')
        key=(child.store_id,digest(resolve_publication_snapshot(child.settings_snapshot_json)),params.get('warehouse_id') if phase=='stock' else params.get('action_id'))
        groups[key].append((publication,child,params))
    jobs=[]
    for group in groups.values():
        read_group_id=uuid4().hex
        units={}
        for record in group:
            product_key=record[1].external_product_id if phase=='promotion' else record[1].id
            if phase=='promotion' and product_key in units and units[product_key][0][2]!=record[2]:raise ConflictError('同一平台商品的活动价或数量冲突，请核对后提交')
            units.setdefault(product_key,[]).append(record)
        buckets=list(units.values())
        for offset in range(0,len(buckets),100):
            chunk=[record for unit in buckets[offset:offset+100] for record in unit]
            payload={'batch_phase':phase,'read_group_id':read_group_id,'candidate_ids':[c.external_product_id for _,c,_ in group] if phase=='promotion' else [],'batch_entries':[{'publication_id':p.id,'child_id':c.id,'store_id':c.store_id,'offer_id':c.offer_id,'product_id':c.external_product_id,'params':params} for p,c,params in chunk]}
            job=enqueue_job(session,job_type='catalog_ozon_publication',payload=payload,config_snapshot=chunk[0][1].settings_snapshot_json,idempotency_key='ozon-batch:'+uuid4().hex,steps=[(c.id,str(loads(p.product_snapshot_json,{}).get('sku') or c.offer_id)+' · '+phase) for p,c,_ in chunk],commit=False)
            for publication,child,_ in chunk:
                if phase=='submit':
                    assets=loads(child.assets_json,{});assets['personal_submit_job_id']=job.id;child.assets_json=dumps(assets)
                    child.status='submit_queued';publication.job_id=job.id
                else: setattr(child,phase+'_status','processing')
                publication.status=publication_status(publication.stores)
            jobs.append(job.id)
    session.commit()
    return {'job_ids':jobs,'product_count':len(entries),'batch_count':len(jobs)}


def targets(session,job):
    value=loads(job.payload_json,{})
    if job.type!='catalog_ozon_publication' or not job.created_by or not job.target_worker_id or value.get('batch_phase') not in {'submit','stock','promotion'}:
        raise ConflictError('不是本人电脑的批量任务')
    result=[]
    for entry in value['batch_entries']:
        publication=get_publication(session,entry['publication_id'])
        child=next((c for c in publication.stores if c.id==entry['child_id']),None)
        if not child or child.store_id!=entry['store_id'] or child.offer_id!=entry['offer_id']:
            raise ConflictError('批次商品资料已变化')
        if value['batch_phase']=='submit' and loads(child.assets_json,{}).get('personal_submit_job_id')!=job.id:
            raise ConflictError('已有另一建品任务')
        result.append((publication,child,entry))
    if len({c.store_id for _,c,_ in result})!=1:raise ConflictError('批次跨店铺')
    return value['batch_phase'],result


def snapshot(session,job):
    phase,rows=targets(session,job)
    entries=[]
    for _,child,entry in rows:
        entries.append({**entry,'item':loads(child.assets_json,{}).get('import_item') if phase=='submit' else None})
    plan={'phase':phase,'entries':entries}
    return {'schema':1,'job_id':job.id,**plan,'operation_hash':digest(plan),'config':resolve_publication_snapshot(job.config_snapshot_json),'receipt':loads(job.external_ref,{}),'read_group_id':loads(job.payload_json,{})['read_group_id'],'candidate_ids':loads(job.payload_json,{}).get('candidate_ids',[])}


def accept(session,job,action,value):
    plan=snapshot(session,job);prior=loads(job.external_ref,{})
    if value.get('operation_hash')!=plan['operation_hash']:raise ConflictError('批次快照已变化')
    if action=='intent':
        if prior and prior.get('operation_hash')!=plan['operation_hash']:raise ConflictError('已有不同意图')
        if not prior:job.external_ref=dumps({'operation_hash':plan['operation_hash'],'state':'pending'})
        return {'ok':True}
    if prior.get('operation_hash')!=plan['operation_hash']:raise ConflictError('没有原操作意图')
    if action=='task':
        task=str(value.get('task_id',''))
        if plan['phase']!='submit' or not task.isdigit() or int(task)<=0:raise ConflictError('批量task_id无效')
        if prior.get('task_id') and prior['task_id']!=task:raise ConflictError('原task_id不一致')
        phase,rows=targets(session,job)
        for publication,child,_ in rows:
            if child.external_task_id and child.external_task_id!=task:raise ConflictError('商品已关联其他task_id')
            child.external_task_id=task
            if prior.get('state')!='done':child.status='import_pending'
        prior['task_id']=task;job.external_ref=dumps(prior)
        return {'ok':True}
    result=value.get('result');phase,rows=targets(session,job)
    if not isinstance(result,dict) or len(dumps(result).encode())>1024*1024:raise ConflictError('批次回执格式无效')
    if prior.get('state')=='done':
        if prior.get('result_hash')!=digest(result):raise ConflictError('已有不同结果')
        return {'catalog_ozon_finalized':True,'job_id':job.id,'status':job.status}
    outcomes={}
    if phase=='submit':
        if str(value.get('task_id'))!=prior.get('task_id'):raise ConflictError('回执task_id不同')
        raw=result.get('result',{}).get('items',[])
        expected={c.offer_id for _,c,_ in rows}
        if any(r.get('offer_id') not in expected for r in raw) or len({r.get('offer_id') for r in raw})!=len(raw):raise ConflictError('回执含其他商品或重复货号')
        for row in raw:
            status=str(row.get('status','')).lower();errors=[e for e in row.get('errors',[]) if e.get('level','').lower()!='warning']
            if status=='imported' and not errors and str(row.get('product_id','')).isdigit():outcomes[row['offer_id']]=('success',str(row['product_id']))
            elif status in {'failed','error'} or errors:outcomes[row['offer_id']]=('failed',dumps(errors)[:1000])
    elif phase=='stock':
        raw=result.get('result',[])
        if not isinstance(raw,list):raise ConflictError('库存未返回逐商品结果')
        expected={c.offer_id for _,c,_ in rows}
        if any(r.get('offer_id') not in expected for r in raw):raise ConflictError('库存回执货号不一致')
        grouped=defaultdict(list)
        for row in raw:
            if str(row.get('warehouse_id')) != str(rows[0][2]['params']['warehouse_id']):raise ConflictError('库存回执仓库不一致')
            grouped[row['offer_id']].append(row)
        for offer,values in grouped.items():
            # Duplicate ledger rows can yield an empty product_id=0 placeholder
            # alongside the explicit result for the same real platform product.
            explicit=[r for r in values if r.get('updated') is True or r.get('errors')]
            if explicit:values=explicit
            unique={digest(r):r for r in values}
            if len(unique)!=1:
                outcomes[offer]=('unknown','平台同一货号返回相互冲突的库存结果，请核对原回执');continue
            row=next(iter(unique.values()))
            product=str(row.get('product_id') or '')
            expected_ids={str(c.external_product_id) for _,c,_ in rows if c.offer_id==offer}
            if product and product!='0' and product not in expected_ids:
                outcomes[offer]=('unknown','库存回执商品编号与原商品不一致');continue
            if row.get('updated') is True and not row.get('errors'):outcomes[offer]=('success','')
            elif row.get('errors'):
                codes={str(e.get('code') or '') for e in row['errors']}
                missing=bool(codes & {'PRODUCT_IS_NOT_CREATED','PRODUCT_NOT_FOUND','PRODUCT_IS_ARCHIVED','PRODUCT_NOT_EXIST'})
                outcomes[offer]=('unavailable' if missing else 'failed',dumps(row['errors'])[:1000])
    else:
        raw=result.get('result',result)
        if not isinstance(raw,dict):raise ConflictError('活动未返回逐商品结果')
        accepted={str(v) for v in raw.get('active_product_ids',raw.get('product_ids',[]))}
        rejected={str(r.get('product_id')):str(r.get('reason') or r.get('message') or '活动拒绝') for r in raw.get('rejected',[])}
        for identity in raw.get('deactivated_product_ids',[]):
            rejected[str(identity)]='平台已将商品移出活动，请核对活动限价'
        ineligible={str(v) for v in result.get('ineligible',[])}
        expected={c.external_product_id for _,c,_ in rows}
        if (accepted|set(rejected)|ineligible)-expected or accepted&set(rejected) or accepted&ineligible:raise ConflictError('活动回执商品不一致')
        for _,child,_ in rows:
            identity=child.external_product_id
            if identity in accepted:outcomes[child.offer_id]=('success','')
            elif identity in rejected:outcomes[child.offer_id]=('failed',rejected[identity])
            elif identity in ineligible:outcomes[child.offer_id]=('ineligible','未满足活动资格')
    for publication,child,entry in rows:
        status,message=outcomes.get(child.offer_id,('unknown','平台未返回该商品的明确结果'))
        if phase=='submit':
            if status=='success':child.external_product_id=message;child.status='stock_pending';child.stock_status='pending';child.promotion_status='pending'
            elif status=='failed':child.status='failed'
            else:child.status='import_pending'
        elif phase=='stock':
            child.stock_status='completed' if status=='success' else 'failed' if status=='unavailable' else 'pending' if status=='failed' else 'unknown'
            if status=='unavailable':child.status='failed'
            if status=='success':
                child.status='promotion_pending';child.promotion_status='pending'
                assets=loads(child.assets_json,{})
                assets['stock_confirmation']={'quantity':entry['params']['stock'],'warehouse_id':entry['params']['warehouse_id'],'job_id':job.id}
                child.assets_json=dumps(assets)
        else:
            child.promotion_status='completed' if status=='success' else 'pending_eligibility' if status=='ineligible' else 'pending' if status=='failed' else 'unknown'
            if status=='success':child.status='completed'
        child.last_error='' if status=='success' else message
        publication.status=publication_status(publication.stores)
        step=next(s for s in job.steps if s.key==child.id)
        step.status='completed' if status=='success' else 'failed' if status in {'failed','unavailable'} else 'pending';step.progress=100 if status=='success' else 0
        step.details_json=dumps({'offer_id':child.offer_id,'status':status,'task_id':prior.get('task_id'),'message':child.last_error});step.error_message=child.last_error
    job.status='completed' if phase!='submit' and all(s.status=='completed' for s in job.steps) else 'action_required'
    job.progress=sum(s.progress for s in job.steps)/len(job.steps);job.finished_at=utc_now();job.lease_owner='';job.lease_expires_at=None
    prior.update(state='done',result_hash=digest(result));job.external_ref=dumps(prior)
    session.add(JobEvent(job_id=job.id,message='批次回执已按商品分别核对',data_json=dumps({'phase':phase,'count':len(rows),'result':result})))
    return {'catalog_ozon_finalized':True,'job_id':job.id,'status':job.status}
