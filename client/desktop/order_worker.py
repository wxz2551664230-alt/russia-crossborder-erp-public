"""Read platform orders on the initiating PC; retain encrypted resume inputs."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from composition_worker import atomic_json


_PHASE = 'startup'


def _stage(value):
    global _PHASE
    _PHASE = value


class OrderExecutionError(ValueError):
    def __init__(self,message,code='local_execution_failed'):
        super().__init__(message);self.code=code


def order_chunks(postings):
    # Match Client's ASCII JSON encoding. Leave room for the envelope under the
    # private transport's 2 MiB request limit, including long recipient fields.
    chunks=[]; current=[]; size=0
    for row in postings:
        encoded=len(json.dumps(row).encode('utf-8'))+2
        if encoded>1024*1024:
            raise ValueError('单条订单超过传输上限，请保留任务检查点')
        if current and (len(current)>=200 or size+encoded>1024*1024):
            chunks.append(current);current=[];size=0
        current.append(row);size+=encoded
    return chunks+[current] if current else chunks or [[]]


def run(client, endpoint, job, root, lost):
    if not re.fullmatch('[a-f0-9]{32}',job['id']):raise ValueError('任务编号无效')
    work=root/'order-tasks'/job['id'];work.mkdir(parents=True,exist_ok=True)
    source=work/'input.json';output=work/'transport-result.json'
    atomic_json(source,{'endpoint':endpoint,'job':job,'root':str(root.resolve())})
    output.unlink(missing_ok=True)
    process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--child',str(source.resolve())],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        while process.poll() is None:
            if lost.wait(.5):
                process.terminate();process.wait(timeout=15)
                raise ValueError('订单任务连接失效，已保留加密检查点')
        if not output.is_file():raise ValueError('订单执行进程中断，请保留原任务目录')
        value=json.loads(output.read_text(encoding='utf-8'))
        if 'error' in value:raise OrderExecutionError(value['error'],value.get('code','local_execution_failed'))
        return value['result']
    finally:
        if process.poll() is None:process.terminate();process.wait(timeout=15)


def execute(source):
    _stage('load_input')
    from erp_desktop import Client, protect
    value=json.loads(source.read_text(encoding='utf-8'));job=value['job'];endpoint=value['endpoint']
    work=source.parent
    os.environ.update(ERP_DATA_DIR=str(work),ERP_DATABASE_URL='sqlite:///'+(work/'unused.db').as_posix(),
                      ERP_MULTIUSER_ENABLED='false',ERP_RUNTIME_MODE='worker')
    runtime=Path(__file__).parent/'runtime'
    sys.path.insert(0,str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2]/'backend'))
    _stage('import_order_runtime')
    from erp.domains.orders.service import fetch_platform_orders,fetch_platform_images
    _stage('load_client_session')
    client=Client();headers={'X-Worker-Attempt':str(job['attempts']), 'X-Order-Scope-Version':'1'}
    def post(suffix,body):return client.request('POST',endpoint+suffix,body,extra_headers=headers)[0]
    def check():post('/heartbeat',{})
    def checkpoint(name,fetch):
        path=work/(name+'.dpapi')
        check()
        if path.exists():return json.loads(protect(path.read_bytes(),decrypt=True))
        result=fetch();check()
        temporary=path.with_suffix('.partial')
        temporary.write_bytes(protect(json.dumps(result,ensure_ascii=False).encode('utf-8')))
        temporary.replace(path)
        return result
    if job['type']=='pod_purchase_submit':
        _stage('import_purchase_runtime')
        from purchase_submit_worker import execute as submit
        return submit(work,job,post,check,stage=_stage)
    if job['type']=='pod_purchase_sync':
        from erp.domains.orders.service import fetch_purchase_status
        snapshot=post('/purchase-snapshot',{})
        records=checkpoint('purchase-statuses',lambda:fetch_purchase_status(snapshot['config'],snapshot['targets'],check))
        return post('/purchase-statuses',{'records':records})
    _stage('load_order_snapshot')
    snapshot=post('/order-snapshot',{});summary={}
    if job['type']=='pod_order_sync':
        _stage('read_platform_orders')
        postings=checkpoint('postings',lambda:fetch_platform_orders(snapshot['config'],snapshot['payload']))
        _stage('upload_orders')
        chunks=order_chunks(postings)
        totals={'imported':0,'updated':0,'total':0}
        for index,chunk in enumerate(chunks):
            check()
            receipt=post('/order-batch',{'index':index,'postings':chunk,'final':index==len(chunks)-1})
            for key in totals:totals[key]+=receipt[key]
        summary['sync']=totals
        snapshot=post('/order-snapshot',{})
    _stage('read_platform_images')
    images=checkpoint('images',lambda:fetch_platform_images(snapshot['config'],snapshot['offers'],check)) if snapshot['payload'].get('sync_images',True) else {}
    _stage('upload_images')
    summary['images']=post('/order-images',{'images':images})
    return summary


if __name__=='__main__':
    source=Path(sys.argv[2]).resolve()
    try:result={'result':execute(source)}
    except Exception as error:
        # Platform errors can contain recipients or credentials; never copy them
        # into shared task logs. Original encrypted checkpoints remain on this PC.
        unknown=bool(getattr(error,'outcome_unknown',False) or (getattr(error,'details',None) or {}).get('outcome_unknown'))
        result=({'error':'采购写入结果未知，请先核对原外部订单号','code':'purchase_write_outcome_unknown'} if unknown
                else {'error':'订单执行或回传未完成，请检查连接并从原电脑继续原任务','code':'local_execution_failed'})
        # Local-only diagnostic: never include recipient data, platform error
        # text or credentials in the shared job log.
        result['diagnostic']={'phase':_PHASE,'exception_type':type(error).__name__}
        status = getattr(error, 'status_code', None)
        if type(status) is int:
            result['diagnostic']['status_code'] = status
        if _PHASE == 'read_platform_orders' and status == 429:
            result.update(error='平台订单读取触发限流，等待后仍未完成；请稍后继续原任务',
                          code='order_read_rate_limited')
    atomic_json(source.parent/'transport-result.json',result)
