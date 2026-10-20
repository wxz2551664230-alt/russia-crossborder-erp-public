"""Run the original ERP handler against a small, private, per-task database."""
import hashlib,json,os,re,sys,threading,urllib.request,uuid
from pathlib import Path
from erp_desktop import protect,NoRedirect


def atomic_json(path,value):
    temporary=path.parent/(uuid.uuid4().hex[:16]+'.tmp')
    with temporary.open('x',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def run(client,endpoint,job,root,lost):
    from cryptography.fernet import Fernet
    from local_worker import restore
    if not re.fullmatch('[a-f0-9]{32}',job['id']):raise ValueError('任务编号无效')
    work=(root/'compositions'/job['id']).resolve();work.mkdir(parents=True,exist_ok=True)
    if not work.is_relative_to(root.resolve()):raise ValueError('工作目录无效')
    key=root/'composer-key.dat'
    if not key.exists():
        with key.open('xb') as stream:stream.write(protect(Fernet.generate_key()))
    os.environ.update(ERP_DATA_DIR=str(work),ERP_DATABASE_URL='sqlite:///'+(work/'task.db').as_posix(),ERP_MULTIUSER_ENABLED='false',ERP_RUNTIME_MODE='worker',ERP_SECRET_KEY=protect(key.read_bytes(),decrypt=True).decode())
    runtime=Path(__file__).parent/'runtime'
    if runtime.is_dir():sys.path.insert(0,str(runtime))
    from erp.core.config import get_settings
    get_settings.cache_clear()
    from erp.core.security import vault
    from erp.core.jsonutil import loads,dumps
    from erp.core.errors import AppError
    from erp.models import Base,Asset,Job,JobStep,IntegrationConnection
    from erp.domains.pod.models import PodComposition,PodCompositionSide,PodArtwork
    from erp.domains.pod.desktop_composition import columns
    from erp.domains.pod import composition as original
    from erp.services.jobs import JobContext
    from sqlalchemy import create_engine,select
    from sqlalchemy.orm import Session
    headers={'X-Worker-Attempt':str(job['attempts'])}
    def lease():
        if lost.is_set():raise ValueError('电脑任务连接已失效')
        client.request('POST',endpoint+'/heartbeat',{},extra_headers=headers)
    lease()
    snapshot=client.request('POST',endpoint+'/composition-snapshot',{},extra_headers=headers)[0]
    cid=snapshot['composition']['id']
    engine=create_engine('sqlite:///'+(work/'task.db').as_posix())
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        if not db.get(Job,job['id']):
            for row in snapshot['assets']:
                row=dict(row);row['local_path']='';db.add(Asset(**row))
            db.flush()
            for row in snapshot['artworks']:db.merge(PodArtwork(**row))
            c=dict(snapshot['composition']);raw=loads(c['raw_json'],{});raw['folder']=str(work/'output');raw['saved_files']=[]
            for side in raw['sides']:side['source_path']=''
            c['raw_json']=dumps(raw);db.add(PodComposition(**c));db.flush()
            for row in snapshot['sides']:
                row=dict(row);row['source_legacy_path']='';db.add(PodCompositionSide(**row))
            db.add(Job(**snapshot['job']));db.flush()
            for row in snapshot['steps']:db.add(JobStep(**row))
            db.commit()
        for row in snapshot['connections']:
            row=dict(row);secret=row.pop('secrets');row['secrets_encrypted']=vault.encrypt(secret)
            db.merge(IntegrationConnection(**row))
        db.commit()
        local_job=db.get(Job,job['id']);local_job.pause_requested=False;local_job.cancel_requested=False;db.commit()
        sources=work/'sources';sources.mkdir(exist_ok=True)
        for row in snapshot['assets']:
            asset=db.get(Asset,row['id'])
            if asset.local_path and Path(asset.local_path).is_file():
                with Path(asset.local_path).open('rb') as stream:good=hashlib.file_digest(stream,'sha256').hexdigest()==asset.sha256
                if not good:raise ValueError('已有任务素材校验失败')
                continue
            if row.get('oss_url'):
                downloaded=restore({'id':job['id'],'payload':{'product_id':cid,'files':[{'id':row['id'],'url':row['oss_url'],'sha256':row['sha256'],'size_bytes':row['size_bytes'],'mime_type':row['mime_type']}]}},root,lost)
                downloaded_path=next(Path(downloaded['folder']).glob(row['id']+'.*'))
                target=sources/downloaded_path.name
                if not target.exists():os.link(downloaded_path,target)
                asset.local_path=str(target)
            else:
                target=sources/(row['id']+('.png' if row['mime_type']=='image/png' else '.jpg'))
                request=urllib.request.Request(client.base+'/api/v1'+endpoint+'/source/'+row['id'],headers={**headers,'Cookie':'erp_session='+client.profile['cookie']})
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
                with opener.open(request,timeout=30) as response:
                    content=response.read(30*1024**2+1)
                if len(content)!=row['size_bytes'] or hashlib.sha256(content).hexdigest()!=row['sha256']:raise ValueError('上传素材校验失败')
                if target.exists():
                    if hashlib.sha256(target.read_bytes()).hexdigest()!=row['sha256']:raise ValueError('已有上传素材校验失败')
                else:
                    temporary=sources/(uuid.uuid4().hex[:16]+'.tmp')
                    with temporary.open('xb') as stream:
                        stream.write(content);stream.flush();os.fsync(stream.fileno())
                    os.replace(temporary,target)
                asset.local_path=str(target)
            db.commit()
        journal=work/'supplier-writes';journal.mkdir(exist_ok=True)
        factory=original.supplier_client
        class DurableSupplier:
            def __init__(self,inner):self.inner=inner
            def __enter__(self):return self
            def __exit__(self,*args):self.inner.close()
            def __getattr__(self,key):return getattr(self.inner,key)
            def once(self,key,call):
                lease();path=journal/(hashlib.sha256(key.encode()).hexdigest()[:32]+'.json')
                if path.exists():
                    previous=json.loads(path.read_text(encoding='utf-8'))
                    if previous['state']=='done':return previous['result']
                    if previous['state']!='rejected':raise AppError('供应商写入结果未知，必须核对原结果后恢复，禁止自动重发',code='pod_write_outcome_unknown')
                atomic_json(path,{'state':'pending','operation':key})
                try:result=call()
                except AppError as error:
                    if error.code in {'hicustom_api_error','s2b_api_error'} and isinstance(error.details,dict) and error.details.get('http_status',500)<500:
                        atomic_json(path,{'state':'rejected','operation':key})
                        raise
                    raise AppError('供应商写入结果未确认，请核对后恢复',code='pod_write_outcome_unknown') from error
                atomic_json(path,{'state':'done','result':result});return result
            def upload_gallery(self,**kwargs):return self.once('upload:'+kwargs['external_id'],lambda:self.inner.upload_gallery(**kwargs))
            def create_product(self,payload):return self.once('create:'+payload['external_id'],lambda:self.inner.create_product(payload))
        class Context(JobContext):
            def refresh(self):lease();super().refresh()
            def start_step(self,key):
                step=super().start_step(key)
                client.request('POST',endpoint+'/composition-progress',{'key':key,'progress':step.progress,'completed':step.status=='completed'},extra_headers=headers)
                return step
            def complete_step(self,key,*,details=None):
                super().complete_step(key,details=details)
                client.request('POST',endpoint+'/composition-progress',{'key':key,'progress':100,'completed':True},extra_headers=headers)
        original.supplier_client=lambda *args,**kwargs:DurableSupplier(factory(*args,**kwargs))
        try:
            item=db.get(PodComposition,cid)
            if item.status!='completed':original.native_composition_job(Context(db,local_job))
        finally:original.supplier_client=factory
        item=db.get(PodComposition,cid)
        sides=list(db.scalars(select(PodCompositionSide).where(PodCompositionSide.composition_id==cid)))
        groups=loads(item.rendering_groups_json,[])
        identities={s.asset_id for s in sides}|{identity for g in groups for identity in g.get('asset_ids',[])}
        assets=[columns(db.get(Asset,identity)) for identity in sorted(identities)]
        for a in assets:a['local_path']=''
        return {'composition_id':cid,'product_code':item.product_code,'remote_product_id':item.remote_product_id,
                'sides':[{'id':s.id,'asset_id':s.asset_id,'gallery_code':s.gallery_code} for s in sides],
                'groups':groups,'assets':assets,'folder':str(work/'output')}
