"""One immutable batch, one platform write, per-product shared receipts."""
import json
import os
from pathlib import Path
from types import SimpleNamespace


def candidate_ids(adapter,action_id,wanted,check):
    found=set();last_id='';seen=set()
    for _ in range(100):
        check();data=adapter.promotion_candidates(action_id,100,last_id)
        result=data.get('result',data);items=result.get('products') or []
        found.update(str(r.get('id') or r.get('product_id')) for r in items if str(r.get('id') or r.get('product_id')) in wanted)
        if found==wanted:return found
        cursor=str(result.get('last_id') or '')
        if not items or not cursor:return found
        if cursor in seen:raise ValueError('活动资格游标重复，停止重复请求')
        seen.add(cursor);last_id=cursor
    raise ValueError('活动资格分页超过本批读取上限，请先核对')


def stock_requests(entries):
    unique={}
    for entry in entries:
        row={'offer_id':entry['offer_id'],**entry['params']}
        key=(row['offer_id'],row['warehouse_id'])
        if key in unique and unique[key]!=row:raise ValueError('同一商品仓库的库存数量冲突，请先核对')
        unique[key]=row
    return list(unique.values())


def promotion_requests(entries):
    unique={}
    for entry in entries:
        row={'product_id':int(entry['product_id']),'action_price':entry['params']['action_price'],'stock':entry['params']['stock']}
        identity=row['product_id']
        if identity in unique and unique[identity]!=row:raise ValueError('同一商品活动价或促销数量冲突，请先核对；未发平台请求')
        unique[identity]=row
    return list(unique.values())


def execute(plan,directory,adapter,protect,post,check):
    stock_body=stock_requests(plan['entries']) if plan['phase']=='stock' else None
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    path=directory/'batch-receipt.dpapi'
    def save(state,exclusive=False):
        if exclusive:
            with path.open('xb') as stream:
                stream.write(protect(json.dumps(state).encode()));stream.flush();os.fsync(stream.fileno())
        else:
            temporary=path.with_suffix('.partial');temporary.write_bytes(protect(json.dumps(state).encode()));temporary.replace(path)
    state=json.loads(protect(path.read_bytes(),decrypt=True)) if path.exists() else None
    if state and (state['job_id']!=plan['job_id'] or state['operation_hash']!=plan['operation_hash']):raise ValueError('原批次检查点不同')
    if state is None and plan['phase']=='promotion':promotion_requests(plan['entries']) # before intent; known receipts always replay
    body={'operation_hash':plan['operation_hash']};check();post('intent',body)
    if state is None:
        state={'job_id':plan['job_id'],'operation_hash':plan['operation_hash'],'state':'pending'}
        eligible=None
        if plan['phase']=='promotion':
            wanted={str(e['product_id']) for e in plan['entries']}
            cache=directory.parent.parent/'catalog-ozon-candidates'/plan.get('read_group_id',plan['job_id'])
            cache.parent.mkdir(parents=True,exist_ok=True)
            if cache.exists():
                eligible=set(json.loads(protect(cache.read_bytes(),decrypt=True))) & wanted
            else:
                all_wanted=set(plan.get('candidate_ids') or wanted)
                found=candidate_ids(adapter,plan['entries'][0]['params']['action_id'],all_wanted,check)
                temporary=cache.with_suffix('.partial');temporary.write_bytes(protect(json.dumps(sorted(found)).encode()));temporary.replace(cache)
                eligible=found & wanted
        save(state,exclusive=True);check()
        # Never automatically resend a write once this durable intent exists.
        if plan['phase']=='submit':
            response=adapter.post('/v3/product/import',{'items':[e['item'] for e in plan['entries']]},product_create=True)
            task=response.get('result',{}).get('task_id')
            if not str(task).isdigit() or int(task)<=0:raise ValueError('建品task_id未知，禁止重发')
            state.update(state='task_received',task_id=str(task));save(state)
        elif plan['phase']=='stock':
            response=adapter.post('/v2/products/stocks',{'stocks':stock_body})
            state.update(state='result_received',result=response);save(state)
        else:
            entries=[e for e in plan['entries'] if str(e['product_id']) in eligible]
            try:
                response=adapter.update_promotions(plan['entries'][0]['params']['action_id'],promotion_requests(entries)) if entries else {'active_product_ids':[],'rejected':[]}
            except Exception as error:
                # Only the platform's explicit duplicate-ID validation rejection
                # is known to precede writes. Timeouts/5xx stay pending/unknown.
                if getattr(error,'status_code',None)!=400 or getattr(error,'request_path',None)!='/v1/actions/products/update' or 'products have duplicate id' not in str(error).casefold():raise
                response={'active_product_ids':[],'rejected':[{'product_id':row['product_id'],'reason':str(error)[:1000]} for row in promotion_requests(entries)]}

            response['ineligible']=[int(e['product_id']) for e in plan['entries'] if str(e['product_id']) not in eligible]
            state.update(state='result_received',result=response);save(state)
    if state['state']=='pending':raise ValueError('批次写入结果未知，保留原检查点，禁止自动重发')
    if plan['phase']=='submit':
        post('task',{**body,'task_id':int(state['task_id'])});check()
        if state['state']!='result_received':
            result=adapter.wait_import(int(state['task_id']))
            state.update(state='result_received',result=result);save(state)
    return post('result',{**body,'task_id':state.get('task_id'),'result':state['result']})


def run(client,endpoint,job,root,lost):
    import sys,time
    from erp_desktop import protect,TemporaryConnectionError
    runtime=Path(__file__).parent/'runtime'
    sys.path.insert(0,str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2]/'backend'))
    from erp.integrations.ozon import OzonSellerAdapter
    headers={'X-Worker-Attempt':str(job['attempts'])}
    def post(action,body):
        if lost.is_set():raise ValueError('原电脑任务连接已失效')
        for attempt in range(3):
            try:return client.request('POST',endpoint+'/catalog-ozon-batch/'+action,body,extra_headers=headers)[0]
            except TemporaryConnectionError:
                if attempt==2:raise
                time.sleep(2**attempt)
    def check():
        if lost.is_set():raise ValueError('原电脑任务连接已失效')
        client.request('POST',endpoint+'/heartbeat',{},extra_headers=headers)
    plan=client.request('GET',endpoint+'/catalog-ozon-batch/snapshot',extra_headers=headers)[0]
    return execute(plan,Path(root)/'catalog-ozon-batches'/job['id'],OzonSellerAdapter(plan['config'],repository=SimpleNamespace()),protect,post,check)
