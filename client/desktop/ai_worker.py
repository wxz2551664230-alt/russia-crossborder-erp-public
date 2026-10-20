"""Original AI handlers in a private task workspace, with paid-call checkpoints."""
import hashlib,json,os,re,shutil,sys,urllib.request
from pathlib import Path
from erp_desktop import protect,NoRedirect
from composition_worker import atomic_json


def runtime_path():
    candidates=[Path(__file__).parent/'tools/realesrgan-ncnn-vulkan/realesrgan-ncnn-vulkan.exe',Path(__file__).resolve().parents[2]/'tools/realesrgan-ncnn-vulkan/realesrgan-ncnn-vulkan.exe']
    return next((p for p in candidates if p.is_file() and all((p.parent/'models'/('realesrgan-x4plus.'+suffix)).is_file() for suffix in ('bin','param'))),None)


def run(client,endpoint,job,root,lost):
    from cryptography.fernet import Fernet
    if not re.fullmatch('[a-f0-9]{32}',job['id']):raise ValueError('任务编号无效')
    work=root/'ai-tasks'/job['id'];work.mkdir(parents=True,exist_ok=True)
    key=root/'composer-key.dat'
    if not key.exists():
        with key.open('xb') as stream:stream.write(protect(Fernet.generate_key()))
    os.environ.update(ERP_DATA_DIR=str(work.resolve()),ERP_DATABASE_URL='sqlite:///'+(work/'task.db').resolve().as_posix(),ERP_MULTIUSER_ENABLED='false',ERP_RUNTIME_MODE='worker',ERP_SECRET_KEY=protect(key.read_bytes(),decrypt=True).decode())
    binary=runtime_path()
    if binary:os.environ['ERP_REALESRGAN_EXE']=str(binary)
    runtime=Path(__file__).parent/'runtime'
    if runtime.is_dir():sys.path.insert(0,str(runtime))
    from erp.core.config import get_settings
    get_settings.cache_clear()
    from erp.core.errors import AppError
    from erp.core.security import vault
    from erp.core.jsonutil import loads
    from erp.models import Base,Job,JobStep,IntegrationConnection
    from erp.domains.pod.ai_design import service as original
    from erp.domains.pod.integrations.oss import PodOssService
    from erp.services.jobs import JobContext
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from PIL import Image
    headers={'X-Worker-Attempt':str(job['attempts'])}
    def lease():
        if lost.is_set():raise ValueError('任务连接已失效')
        client.request('POST',endpoint+'/heartbeat',{},extra_headers=headers)
    lease();snapshot=client.request('POST',endpoint+'/ai-snapshot',{},extra_headers=headers)[0]
    manifest=snapshot['manifest'];batch_id=manifest['id']
    if manifest['config'].get('upscale_scale') and not binary:raise ValueError('当前电脑缺少Real-ESRGAN运行时')
    manifest_path=original.batch_dir(batch_id)/'manifest.json'
    if not manifest_path.exists():
        for item in manifest.get('inputs',[])+manifest.get('references',[]):
            if not re.fullmatch('[a-f0-9]{32}',item['id']):raise ValueError('输入文件编号无效')
            if shutil.disk_usage(work).free<512*1024**2+item['size_bytes']:raise ValueError('本机空间不足')
            suffix=Path(item['stored_name']).suffix
            if suffix not in original.IMAGE_SUFFIXES:raise ValueError('输入图片格式无效')
            path=work/(item['id']+suffix)
            request=urllib.request.Request(client.base+'/api/v1'+endpoint+'/ai-source/'+item['id'],headers={**headers,'Cookie':'erp_session='+client.profile['cookie']})
            lease()
            with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(request,timeout=60) as response:content=response.read(original.MAX_UPLOAD_BYTES+1)
            if len(content)!=item['size_bytes'] or hashlib.sha256(content).hexdigest()!=item['sha256']:raise ValueError('输入文件校验失败')
            path.write_bytes(content);original.inspect_image(path);item['path']=str(path.resolve())
        original.save_manifest(manifest)
    engine=create_engine('sqlite:///'+(work/'task.db').resolve().as_posix());Base.metadata.create_all(engine)
    original_request=original._request
    journal=work/'paid-calls';journal.mkdir(exist_ok=True)
    def normalized(value):
        if isinstance(value,bytes):return {'bytes_sha256':hashlib.sha256(value).hexdigest()}
        if isinstance(value,dict):return {k:normalized(v) for k,v in value.items() if k!='headers'}
        if isinstance(value,(list,tuple)):return [normalized(v) for v in value]
        return value
    def durable(kind,method,url,*,model,**kwargs):
        lease()
        fingerprint=hashlib.sha256(json.dumps(normalized([kind,method,url,model,kwargs]),sort_keys=True).encode()).hexdigest()
        path=journal/(fingerprint[:32]+'.json')
        if path.exists():
            checkpoint=json.loads(path.read_text(encoding='utf-8'))
            if checkpoint['state']=='done':return checkpoint['result']
            raise AppError('AI调用结果未知，禁止自动重发计费请求',code='ai_write_outcome_unknown')
        atomic_json(path,{'state':'pending'})
        try:result=original_request(kind,method,url,model=model,**kwargs)
        except Exception as exc:raise AppError('AI调用结果未确认，请核对后恢复',code='ai_write_outcome_unknown') from exc
        atomic_json(path,{'state':'done','result':result});return result
    class Context(JobContext):
        def refresh(self):lease();super().refresh()
        def publish(self,key,completed=False):
            step=next(s for s in self.job.steps if s.key==key)
            client.request('POST',endpoint+'/ai-progress',{'key':key,'progress':step.progress,'completed':completed},extra_headers=headers)
        def set_progress(self,key,progress,message=''):
            super().set_progress(key,progress,message);self.publish(key)
        def complete_step(self,key,*,details=None):
            super().complete_step(key,details=details);self.publish(key,True)
    try:
        with Session(engine) as db:
            if not db.get(Job,job['id']):
                db.add(Job(**snapshot['job']));db.flush()
                for step in snapshot['steps']:db.add(JobStep(**step))
            for row in snapshot['connections']:
                row=dict(row);secrets=row.pop('secrets');row['secrets_encrypted']=vault.encrypt(secrets);db.merge(IntegrationConnection(**row))
            db.commit();local_job=db.get(Job,job['id']);local_job.pause_requested=False;local_job.cancel_requested=False;db.commit()
            original._request=durable
            manifest=original.load_manifest(batch_id)
            finished=manifest.get('status')==('planned' if job['type']=='pod_ai_design_plan' else 'completed')
            if not finished:
                handler={'pod_ai_design_plan':original.plan_job,'pod_ai_design_generate':original.generate_job,'pod_ai_design_upscale':original.upscale_job}[job['type']]
                handler(Context(db,local_job))
            manifest=original.load_manifest(batch_id)
            if job['type']=='pod_ai_design_plan':return {'plan':manifest['plan'],'folder':str(work)}
            oss=PodOssService.from_session(db)
            if not oss:raise ValueError('缺少OSS连接')
            rows=[]
            latest={}
            for output in manifest.get('outputs',[]):
                if output.get('status')=='completed' or latest.get(output['index'],{}).get('status')!='completed':latest[output['index']]=output
            def upload(path):
                lease();result=oss.upload_artwork(path,hashlib.sha256(path.read_bytes()).hexdigest())
                return {k:result[k] for k in ('oss_bucket','oss_key','oss_url','sha256')}|{'size_bytes':result['size']}
            for value in latest.values():
                if value['status']!='completed':rows.append({k:value.get(k,'') for k in ('index','title','status','error')});continue
                path=Path(value['path']);preview=work/('thumb-'+str(value['index'])+'.jpg')
                with Image.open(path) as image:
                    mime=Image.MIME.get(image.format,'image/png');width,height=image.size
                    image.thumbnail((640,640));image.convert('RGB').save(preview,'JPEG',quality=85)
                suffix={'image/jpeg':'.jpg','image/png':'.png','image/webp':'.webp'}.get(mime)
                if not suffix:raise ValueError('AI输出图片格式不支持')
                if path.suffix.lower()!=suffix:
                    canonical=work/('output-'+str(value['index'])+suffix);shutil.copyfile(path,canonical);path=canonical
                rows.append({'index':value['index'],'title':value['title'],'status':'completed','metadata':value.get('metadata',{}),'artwork':{'source':upload(path),'thumbnail':upload(preview),'width':width,'height':height,'mime_type':mime}})
            return {'outputs':rows,'folder':str(work)}
    finally:original._request=original_request;engine.dispose()
