"""WB Excel execution in a fresh process with a job-private database."""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from composition_worker import atomic_json


class WorkerError(ValueError):
    def __init__(self, message, code='local_execution_failed'):
        super().__init__(message); self.code = code


def paid_transport(original, journal, lease):
    """Replay confirmed responses only within this job; pending is never resent."""
    import threading
    counters = {}; lock = threading.Lock()
    journal.mkdir(parents=True, exist_ok=True)
    def request(kind, send, url, **kwargs):
        import httpx
        from erp.core.errors import AppError
        signature = hashlib.sha256(json.dumps([kind,url,kwargs],sort_keys=True).encode()).hexdigest()
        with lock:
            index = counters.get(signature, 0); counters[signature] = index + 1
        path = journal / (signature + '-' + str(index) + '.json')
        lease()
        if path.exists():
            row = json.loads(path.read_text(encoding='utf-8'))
            if row['state'] != 'done':
                raise AppError('文案请求结果未知，禁止自动重发，请先核对', code='ai_write_outcome_unknown')
            return httpx.Response(row['status'], content=base64.b64decode(row['body']), request=httpx.Request('POST',url))
        atomic_json(path, {'state':'pending'})
        try:
            response = original(kind, send, url, **kwargs)
            if response.status_code >= 500 or response.status_code == 408:
                raise OSError('AI upstream outcome unknown')
            atomic_json(path, {'state':'done','status':response.status_code,'body':base64.b64encode(response.content).decode()})
            return response
        except Exception as error:
            raise AppError('文案请求结果未确认，请先核对，不能重复计费', code='ai_write_outcome_unknown') from error
    return request


def run(client, endpoint, job, root, lost):
    if not re.fullmatch('[a-f0-9]{32}',job['id']): raise ValueError('任务编号无效')
    work = root/'wb-tasks'/job['id']; work.mkdir(parents=True,exist_ok=True)
    import shutil
    if shutil.disk_usage(work).free < 512*1024**2:
        raise ValueError('本机可用空间不足512MiB，暂不执行 WB 导出')
    input_path = work/'input.json'; output_path = work/'transport-result.json'
    atomic_json(input_path, {'endpoint':endpoint, 'job':job, 'root':str(root.resolve())})
    output_path.unlink(missing_ok=True)
    # The child loads the encrypted personal session itself; no credentials on CLI.
    process = subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--child',str(input_path.resolve())],
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        while process.poll() is None:
            if lost.wait(.5):
                process.terminate(); process.wait(timeout=15)
                raise ValueError('任务连接失效，已停止 WB 执行；请核对本机检查点')
        if not output_path.is_file(): raise ValueError('WB 执行进程中断，检查点已保留')
        value = json.loads(output_path.read_text(encoding='utf-8'))
        if 'error' in value: raise WorkerError(value['error'],value['code'])
        return value['result']
    finally:
        if process.poll() is None: process.terminate(); process.wait(timeout=15)


def execute(input_path):
    from erp_desktop import Client, TemporaryConnectionError, protect
    from cryptography.fernet import Fernet
    request = json.loads(input_path.read_text(encoding='utf-8'))
    job=request['job']; endpoint=request['endpoint']; root=Path(request['root']); work=input_path.parent
    key=root/'composer-key.dat'
    if not key.exists():
        with key.open('xb') as stream: stream.write(protect(Fernet.generate_key()))
    os.environ.update(ERP_DATA_DIR=str(work), ERP_DATABASE_URL='sqlite:///'+(work/'task.db').as_posix(),
                      ERP_MULTIUSER_ENABLED='false', ERP_RUNTIME_MODE='worker',
                      ERP_SECRET_KEY=protect(key.read_bytes(),decrypt=True).decode())
    runtime=Path(__file__).parent/'runtime'
    sys.path.insert(0,str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2]/'backend'))
    from sqlalchemy import select
    from erp.core.database import engine, SessionLocal
    from erp.models import Base, Job, WbPublicationItem
    from erp.services.jobs import JobContext
    from erp.domains.pod import desktop_wb, wb_export
    from erp.core.ai_usage import task_scope
    from erp.core.jsonutil import loads,dumps
    from erp.services import wb_codex
    client=Client(); headers={'X-Worker-Attempt':str(job['attempts'])}
    def safe_request(path, body):
        # Lease checks and progress reports are repeatable. A temporary
        # connection fault here must not abandon paid calls still running in
        # another copy thread. Business writes are never retried by this.
        for attempt in range(3):
            try: return client.request('POST',path,body,extra_headers=headers)
            except TemporaryConnectionError:
                if attempt == 2: raise
                time.sleep(2)
        raise AssertionError('unreachable')
    def lease(): safe_request(endpoint+'/heartbeat',{})
    lease()
    Base.metadata.create_all(engine)
    snapshot=client.request('POST',endpoint+'/wb-snapshot',{},extra_headers=headers)[0]
    class Context(JobContext):
        def refresh(self):
            # The native copy loop polls every 200 ms. A request on every
            # poll overwhelmed some Windows connections while AI was pending.
            now=time.monotonic()
            if now-getattr(self,'_last_check',0)<5:return
            lease(); self._last_check=now; super().refresh()
        def publish(self,key,completed=False):
            step=next(s for s in self.job.steps if s.key==key)
            safe_request(endpoint+'/wb-progress',{'key':key,'progress':step.progress,'completed':completed})
        def set_progress(self,key,progress,message=''):
            super().set_progress(key,progress,message); self.publish(key)
        def complete_step(self,key,*,details=None):
            super().complete_step(key,details=details); self.publish(key,True)
    with SessionLocal() as db:
        local_job=db.get(Job,job['id'])
        if not local_job:
            local_job=desktop_wb.install_snapshot(db,snapshot,base64.b64decode(snapshot['template_content'],validate=True));db.commit()
        fresh_payload=loads(snapshot['job']['payload_json'],{})
        if fresh_payload.get('generation_provider')=='codex':
            snap=wb_codex.public_snapshot(snapshot)
            path=work/'codex-content.dpapi'
            if not path.is_file():raise WorkerError('等待原电脑 Codex 输出，不会调用外部模型','codex_output_missing')
            record=json.loads(protect(path.read_bytes(),decrypt=True))
            receipt=(fresh_payload.get('codex_outputs') or {}).get(snap['store_id']) or {}
            if any(record.get(k)!=snap[k] for k in ('job_id','store_id','revision')) or record.get('output_sha256')!=wb_codex.digest(record.get('copies')) or receipt.get('output_sha256')!=record.get('output_sha256') or receipt.get('revision')!=snap['revision']:
                raise WorkerError('WB 输出或商品资料已变化，请重新导出','codex_output_missing')
            if fresh_payload.get('mode')=='api' and not fresh_payload.get('codex_publish_approved'):
                raise WorkerError('请在 WB 页面核验文案并确认 API 发布','codex_output_missing')
            wb_codex.validate_outputs(snap,record.get('copies'))
            local_payload=loads(local_job.payload_json,{})
            local_payload.update(_codex_outputs_by_product=record['copies'],codex_publish_approved=fresh_payload.get('codex_publish_approved'),codex_outputs=fresh_payload.get('codex_outputs'))
            local_job.payload_json=dumps(local_payload)
        local_job.pause_requested=False;local_job.cancel_requested=False;local_job.attempts=job['attempts'];db.commit()
        finished=work/'business-complete.json'
        wb_export.ai_post=paid_transport(wb_export.ai_post,work/'paid-calls',lease)
        if not finished.exists():
            with task_scope(job['id'],job['type'],job['attempts']): wb_export.wb_export_job(Context(db,local_job))
            atomic_json(finished,{'completed':True})
        files=[]
        export_root=wb_export._roots()[1]/job['id']
        paths={'wb_package':export_root/'publication-package.json','wb_copy':export_root/'copy.json'}
        if job['type']=='pod_wb_excel_export':
            paths['wb_excel']=wb_export.export_file(db,job['id'])
        if (export_root/'copy-failures.json').is_file():paths['wb_copy_failures']=export_root/'copy-failures.json'
        for role,path in paths.items():
            lease(); content=path.read_bytes()
            if len(content)>24*1024**2: raise ValueError('WB 文件超过24MiB，保留本机文件等待处理')
            receipt=client.request('POST',endpoint+'/wb-file/'+role,{'content':base64.b64encode(content).decode()},extra_headers=headers)[0]
            if receipt['sha256']!=hashlib.sha256(content).hexdigest() or receipt['size_bytes']!=len(content):raise ValueError('云端文件回执不一致')
            files.append(receipt)
        ledgers=[{'id':r.id,'status':r.status,'last_error':r.last_error,'external_product_id':r.external_product_id} for r in db.scalars(select(WbPublicationItem).where(WbPublicationItem.job_id==job['id']))]
        return {'folder':str(export_root),'files':files,'ledgers':ledgers}


if __name__=='__main__':
    input_path=Path(sys.argv[2]).resolve()
    try: value={'result':execute(input_path)}
    except Exception as error: value={'error':str(error)[:300],'code':getattr(error,'code','local_execution_failed')}
    atomic_json(input_path.parent/'transport-result.json',value)
