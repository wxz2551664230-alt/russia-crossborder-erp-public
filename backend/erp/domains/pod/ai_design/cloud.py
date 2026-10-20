"""Shared AI metadata and transport. All AI and image work runs on personal PCs."""
import hashlib,re,uuid
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy import select
from fastapi.responses import FileResponse,RedirectResponse
from erp.core.config import get_settings
from erp.core.errors import ConflictError,AppError
from erp.core.jsonutil import dumps,loads
from erp.core.security import vault
from erp.core.task_identity import task_identity
from erp.models import SystemSetting,Job,IntegrationConnection,WorkerNode
from erp.serializers import job_dict,connection_dict
from erp.services.jobs import enqueue_job
from erp.domains.pod.desktop_composition import columns
from erp.domains.pod.desktop_artwork import accept_result as accept_artwork
from .service import calculate_size,public_manifest,MAX_UPLOAD_BYTES,IMAGE_SUFFIXES

TYPES={'pod_ai_design_plan','pod_ai_design_generate','pod_ai_design_upscale'}

def actor():return task_identity.get() or {}
def key(identity):
    if not re.fullmatch('[a-f0-9]{32}',identity):raise ConflictError('批次编号无效')
    return 'cloud-ai:'+identity

def row(db,identity):
    value=db.scalar(select(SystemSetting).where(SystemSetting.key==key(identity)).with_for_update())
    if not value:raise ConflictError('批次不存在')
    return value

def require_owner(manifest):
    if manifest.get('owner_id')!=actor().get('id'):raise AppError('只能操作本人创建的批次',code='forbidden',status_code=403)
    if manifest.get('worker_id')!=actor().get('worker_id'):raise ConflictError('请选回创建此批次的原执行电脑')

def read(db,identity):
    stored=db.get(SystemSetting,key(identity))
    if not stored:raise ConflictError('批次不存在')
    manifest=loads(stored.value_json,{})
    jobid=manifest.get('generation_job_id') or manifest.get('plan_job_id') or manifest.get('job_id')
    job=db.get(Job,jobid) if jobid else None
    if job and job.status in {'failed','cancelled','paused'}:
        manifest['status']=job.status;manifest['error']=job.error_message
    return public_manifest(manifest)

def status(db):
    device=db.get(WorkerNode,actor().get('worker_id',''))
    caps=loads(device.capabilities_json,[]) if device else []
    batches=[public_manifest(loads(r.value_json,{})) for r in db.scalars(select(SystemSetting).where(SystemSetting.key.like('cloud-ai:%')).order_by(SystemSetting.updated_at.desc()).limit(50))]
    connections=db.scalars(select(IntegrationConnection).where(IntegrationConnection.provider.in_(['ai_chat','ai_image']),IntegrationConnection.enabled.is_(True))).all()
    return {'runtime':{'available':'pod_ai_design_upscale' in caps,'engine':'realesrgan-ncnn-vulkan','model':'realesrgan-x4plus','token_usage':False,'output_limit_bytes':30_000_000,'executable':'所选个人电脑' if device else ''},'connections':[connection_dict(c) for c in connections],'batches':batches}

def stage(db,content,filename):
    if not content or len(content)>MAX_UPLOAD_BYTES:raise ConflictError('图片必须大于0且不超过30MB')
    suffix=Path(filename).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:raise ConflictError('只支持PNG、JPG、WebP')
    identity=uuid.uuid4().hex
    root=get_settings().data_dir/'pod'/'ai-cloud-inputs';root.mkdir(parents=True,exist_ok=True)
    path=root/(identity+suffix);path.write_bytes(content)
    value={'id':identity,'name':Path(filename).name,'stored_name':path.name,'sha256':hashlib.sha256(content).hexdigest(),'size_bytes':len(content),'owner_id':actor().get('id')}
    db.add(SystemSetting(key='cloud-ai-upload:'+identity,value_json=dumps(value)));db.commit()
    return value

def uploads(db,ids,limit):
    if not isinstance(ids,list) or len(ids)>limit:raise ConflictError('图片数量超出限制')
    rows=[]
    for identity in ids:
        if not isinstance(identity,str) or not re.fullmatch('[a-f0-9]{32}',identity):raise ConflictError('上传记录无效')
        stored=db.get(SystemSetting,'cloud-ai-upload:'+identity)
        value=loads(stored.value_json,{}) if stored else {}
        if value.get('owner_id')!=actor().get('id'):raise AppError('上传文件不属于当前账号',code='forbidden',status_code=403)
        rows.append(value)
    return rows

def create(db,kind,config,ids,*,identity=None):
    if identity:
        existing=db.get(SystemSetting,key(identity))
        if existing:
            manifest=loads(existing.value_json,{})
            if (manifest.get('owner_id')!=actor().get('id') or manifest.get('worker_id')!=actor().get('worker_id')
                    or manifest.get('kind')!=kind or manifest.get('config')!=config
                    or [row.get('id') for row in manifest.get('inputs' if kind=='upscale' else 'references',[])]!=ids):
                raise ConflictError('原任务参数或执行电脑已变化，不能重复创建')
            job=db.get(Job,manifest.get('job_id') or manifest.get('plan_job_id'))
            if not job:raise ConflictError('原任务记录缺失，请人工核对')
            return {'batch':public_manifest(manifest),'job':job_dict(job)}
    refs=uploads(db,ids,16 if kind=='generation' else 30)
    if kind=='upscale' and not refs:raise ConflictError('请选择超分图片')
    if kind=='generation':
        for field,provider in [('planner_connection_id','ai_chat'),('image_connection_id','ai_image')]:
            connection=db.get(IntegrationConnection,config[field])
            if not connection or not connection.enabled or connection.provider!=provider:raise ConflictError('请选择已启用的AI接口')
        config['size']=calculate_size(config['aspect_ratio'],config['max_edge'])
    if config.get('upscale_scale'):
        device=db.get(WorkerNode,actor().get('worker_id',''))
        if not device or 'pod_ai_design_upscale' not in loads(device.capabilities_json,[]):raise ConflictError('所选电脑未安装超分运行时，请更新客户端')
    identity=identity or uuid.uuid4().hex;now=datetime.now(timezone.utc).isoformat()
    manifest={'id':identity,'kind':kind,'status':'planning' if kind=='generation' else 'queued','created_at':now,'updated_at':now,'owner_id':actor().get('id'),'worker_id':actor().get('worker_id'),'config':config,'outputs':[],('references' if kind=='generation' else 'inputs'):refs}
    stored=SystemSetting(key=key(identity),value_json='{}');db.add(stored)
    typ='pod_ai_design_plan' if kind=='generation' else 'pod_ai_design_upscale'
    job=enqueue_job(db,job_type=typ,payload={'batch_id':identity},config_snapshot=dumps(config),idempotency_key=typ+':'+identity,max_attempts=1,steps=[('plan' if kind=='generation' else 'upscale','AI设计规划' if kind=='generation' else '本机超分')],commit=False)
    manifest['plan_job_id' if kind=='generation' else 'job_id']=job.id
    stored.value_json=dumps(manifest);db.commit()
    return {'batch':public_manifest(manifest),'job':job_dict(job)}

def edit_plan(db,identity,items):
    stored=row(db,identity);manifest=loads(stored.value_json,{})
    require_owner(manifest)
    active=db.get(Job,manifest.get('generation_job_id') or manifest.get('plan_job_id'))
    if active and active.error_code=='ai_write_outcome_unknown':raise ConflictError('AI调用结果未知，需核对后恢复')
    if active and active.status in {'queued','running','retrying','paused'}:raise ConflictError('请等待当前任务结束')
    if manifest.get('status') not in {'planned','failed'}:raise ConflictError('当前规划不可修改')
    if len({i['index'] for i in items})!=len(items):raise ConflictError('规划序号不能重复')
    manifest.setdefault('plan',{})['items']=items;stored.value_json=dumps(manifest);db.commit();return public_manifest(manifest)

def generate(db,identity):
    stored=row(db,identity);manifest=loads(stored.value_json,{});require_owner(manifest)
    old=db.get(Job,manifest.get('generation_job_id') or manifest.get('plan_job_id'))
    if old and (old.status in {'queued','running','retrying','paused'} or old.error_code=='ai_write_outcome_unknown'):raise ConflictError('原任务仍在执行或结果待核对，请勿重复提交')
    if not manifest.get('plan',{}).get('items'):raise ConflictError('请先完成规划')
    job=enqueue_job(db,job_type='pod_ai_design_generate',payload={'batch_id':identity},config_snapshot='{}',idempotency_key='ai-generate:'+identity+':'+uuid.uuid4().hex,max_attempts=1,steps=[('generate','生图与超分')],commit=False)
    manifest['generation_job_id']=job.id;manifest['status']='queued';manifest['outputs']=[]
    stored.value_json=dumps(manifest);db.commit();return {'batch':public_manifest(manifest),'job':job_dict(job)}

def snapshot(db,job):
    manifest=loads(db.get(SystemSetting,key(loads(job.payload_json,{})['batch_id'])).value_json,{})
    required=[]
    if job.type=='pod_ai_design_plan':required.append((manifest['config']['planner_connection_id'],'ai_chat'))
    if job.type=='pod_ai_design_generate':required.append((manifest['config']['image_connection_id'],'ai_image'))
    connections=[]
    for identity,provider in required:
        c=db.get(IntegrationConnection,identity)
        if not c or not c.enabled or c.provider!=provider:raise ConflictError('AI连接不可用')
        connections.append(c)
    oss=next((c for c in db.scalars(select(IntegrationConnection).where(IntegrationConnection.provider=='oss',IntegrationConnection.enabled.is_(True))) if loads(c.config_json,{}).get('prefix')=='hicustom-products'),None)
    if not oss:raise ConflictError('未配置OSS')
    connections.append(oss);values=[]
    for c in connections:
        value=columns(c);value.pop('secrets_encrypted',None);value['secrets']=vault.decrypt(c.secrets_encrypted);values.append(value)
    return {'manifest':manifest,'job':columns(job),'steps':[columns(s) for s in job.steps],'connections':values}

def source(db,job,identity):
    manifest=loads(db.get(SystemSetting,key(loads(job.payload_json,{})['batch_id'])).value_json,{})
    item=next((r for r in manifest.get('references',[])+manifest.get('inputs',[]) if r['id']==identity),None)
    if not item:raise ConflictError('文件不属于此任务')
    name=item['stored_name']
    if Path(name).name!=name:raise ConflictError('文件名无效')
    path=get_settings().data_dir/'pod'/'ai-cloud-inputs'/name
    if not path.is_file() or path.is_symlink():raise ConflictError('输入文件缺失')
    return path

def accept(db,job,result):
    stored=row(db,loads(job.payload_json,{})['batch_id']);manifest=loads(stored.value_json,{})
    if job.type=='pod_ai_design_plan':
        from .api import PlanUpdate
        plan=result.get('plan',{});validated=PlanUpdate(items=plan.get('items',[]))
        if len(validated.items)!=manifest['config']['count']:raise ConflictError('规划数量不完整')
        if {i.index for i in validated.items}!=set(range(1,len(validated.items)+1)):raise ConflictError('规划序号不完整')
        manifest['plan']={**{k:str(plan.get(k,''))[:8000] for k in ['summary','audience']},'palette':[str(c)[:100] for c in plan.get('palette',[])[:100]],'items':[i.model_dump() for i in validated.items]};manifest['status']='planned'
    else:
        rows=result.get('outputs')
        expected={r['index'] for r in manifest.get('plan',{}).get('items',[]) if r.get('selected',True)} if job.type=='pod_ai_design_generate' else set(range(1,len(manifest['inputs'])+1))
        if not isinstance(rows,list) or len(rows)!=len(expected) or any(not isinstance(r,dict) or type(r.get('index'))!=int for r in rows) or {r['index'] for r in rows}!=expected:raise ConflictError('批次结果不完整')
        outputs=[]
        for value in rows:
            if value.get('status')=='failed':outputs.append({'index':value['index'],'title':str(value.get('title',''))[:200],'status':'failed','error':str(value.get('error',''))[:500]});continue
            art=value.get('artwork',{});original=art.get('source',{})
            if not isinstance(original.get('sha256'),str) or not re.fullmatch('[a-f0-9]{64}',original['sha256']):raise ConflictError('结果哈希无效')
            if type(original.get('size_bytes'))!=int or not 0<original['size_bytes']<=MAX_UPLOAD_BYTES:raise ConflictError('结果图片超过30MB')
            payload={'sha256':original.get('sha256'),'size_bytes':original['size_bytes'],'filename':str(value.get('title','设计图'))[:200],'category_id':manifest['config'].get('category_id','uncategorized') if job.type=='pod_ai_design_upscale' else 'uncategorized'}
            item=accept_artwork(db,SimpleNamespace(payload_json=dumps(payload)),art,allow_webp=True)
            outputs.append({'index':value['index'],'title':item.name,'status':'completed','artwork_id':item.id,'asset_id':item.asset_id,'url':f'/api/v1/pod/artworks/{item.id}/file','width':item.asset.width,'height':item.asset.height,'size_bytes':item.asset.size_bytes,'metadata':value.get('metadata',{})})
        if not any(o['status']=='completed' for o in outputs):raise ConflictError('本批没有成功结果')
        manifest['outputs']=outputs;manifest['status']='completed'
    manifest['updated_at']=datetime.now(timezone.utc).isoformat();stored.value_json=dumps(manifest)
