"""Personal-computer artwork processing; server stages bytes and verifies storage."""
import hashlib
import os
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
import httpx
from sqlalchemy import select,text
from erp.core.config import get_settings
from erp.core.errors import ConflictError,AppError
from erp.core.jsonutil import dumps,loads
from erp.core.security import vault
from erp.models import Asset,IntegrationConnection,Job
from erp.core.task_identity import task_identity
from erp.services.jobs import enqueue_job
from erp.domains.pod.models import PodArtwork,PodArtworkCategory
from erp.domains.pod.artworks import _safe_name,_system_category,MAX_ARTWORK_BYTES,MAX_ARTWORK_PIXELS
from erp.domains.pod.desktop_composition import columns,verify_cloud_asset


def stage(db,content,filename,content_type,category_id,*,upload_token=None):
    if not content or len(content)>MAX_ARTWORK_BYTES:raise AppError('图片为空或超过30MB',code='artwork_size_invalid',status_code=413)
    if category_id and category_id!='uncategorized' and not db.get(PodArtworkCategory,category_id):raise ConflictError('图库分类不存在')
    token=upload_token or uuid.uuid4().hex
    if upload_token and not re.fullmatch('[a-f0-9]{32}',upload_token):raise ConflictError('暂存编号无效')
    if upload_token:
        actor=task_identity.get() or {}
        assignment='cloud-artwork:'+token
        key='assigned:'+hashlib.sha256(f"{actor.get('id','')}:{actor.get('worker_id','')}:{assignment}".encode()).hexdigest()
        old=db.scalar(select(Job).where(Job.idempotency_key==key))
        if old:
            previous=loads(old.payload_json,{})
            if old.created_by!=actor.get('id') or old.target_worker_id!=actor.get('worker_id') or previous.get('sha256')!=hashlib.sha256(content).hexdigest() or previous.get('category_id')!=category_id:
                raise ConflictError('暂存图片或分类已变化，不能重复创建')
            return {'filename':previous['filename'],'status':old.status,'job_id':old.id}
    path=get_settings().data_dir/'pod'/'cloud-upload-staging'/(token+'.upload')
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb') as output:output.write(content);output.flush();os.fsync(output.fileno())
    try:
        job=enqueue_job(db,job_type='pod_artwork_upload',payload={'upload_token':token,'filename':_safe_name(filename),'content_type':content_type,'category_id':category_id,'sha256':hashlib.sha256(content).hexdigest(),'size_bytes':len(content)},config_snapshot='{}',idempotency_key='cloud-artwork:'+token,steps=[('validate','校验设计图'),('save','保存并去重'),('oss','同步 OSS')],max_attempts=3)
    except Exception:
        db.rollback();path.unlink(missing_ok=True);raise
    return {'filename':_safe_name(filename),'status':'queued','job_id':job.id}


def staged_path(job):
    token=loads(job.payload_json,{})['upload_token']
    if not re.fullmatch('[a-f0-9]{32}',token):raise ConflictError('上传记录无效')
    path=get_settings().data_dir/'pod'/'cloud-upload-staging'/(token+'.upload')
    if path.is_symlink() or not path.is_file():raise ConflictError('上传暂存文件不存在')
    return path


def snapshot(db,job):
    connection=next((c for c in db.scalars(select(IntegrationConnection).where(IntegrationConnection.provider=='oss',IntegrationConnection.enabled.is_(True))) if loads(c.config_json,{}).get('prefix','').strip('/')=='hicustom-products'),None)
    if not connection:raise ConflictError('未配置图库 OSS')
    row=columns(connection);row.pop('secrets_encrypted',None);row['secrets']=vault.decrypt(connection.secrets_encrypted)
    return {'payload':loads(job.payload_json,{}),'connection':row}


def verify_object(row,expected_sha,expected_size,suffix):
    key=f'hicustom-products/artwork-library/{expected_sha[:2]}/{expected_sha}{suffix}'
    if row.get('sha256')!=expected_sha or row.get('size_bytes')!=expected_size or row.get('oss_key')!=key:raise ConflictError('上传结果与原文件不一致')
    verify_cloud_asset(row,'',SimpleNamespace(oss_key=key,sha256=expected_sha))


def accept_result(db,job,result,*,allow_webp=False):
    payload=loads(job.payload_json,{})
    source=result.get('source');thumb=result.get('thumbnail')
    if not isinstance(source,dict) or not isinstance(thumb,dict):raise ConflictError('原图与缩略图结果缺失')
    width,height=result.get('width'),result.get('height')
    if type(width)!=int or type(height)!=int or width<=0 or height<=0 or width*height>MAX_ARTWORK_PIXELS:raise ConflictError('图片尺寸无效')
    suffix={ 'image/jpeg':'.jpg','image/png':'.png',**({'image/webp':'.webp'} if allow_webp else {}) }.get(result.get('mime_type'))
    if not suffix:raise ConflictError('只支持PNG/JPG')
    verify_object(source,payload['sha256'],payload['size_bytes'],suffix)
    td=thumb.get('sha256','');ts=thumb.get('size_bytes')
    if not isinstance(td,str) or not re.fullmatch('[a-f0-9]{64}',td) or type(ts)!=int or not 0<ts<=1024**2:raise ConflictError('缩略图无效')
    verify_object(thumb,td,ts,'.jpg')
    # Copy only the small, already-generated preview; no image processing here.
    with httpx.Client(timeout=30,follow_redirects=False,trust_env=False) as client:
        with client.stream('GET',thumb['oss_url']) as response:
            response.raise_for_status();parts=[];size=0
            for chunk in response.iter_bytes():
                size+=len(chunk)
                if size>ts:raise ConflictError('缩略图超过声明大小')
                parts.append(chunk)
    preview=b''.join(parts)
    if len(preview)!=ts or hashlib.sha256(preview).hexdigest()!=td:raise ConflictError('缩略图校验失败')
    if db.bind.dialect.name=='postgresql':db.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':int(payload['sha256'][:15],16)})
    item=db.scalar(select(PodArtwork).where(PodArtwork.sha256==payload['sha256']).order_by(PodArtwork.created_at))
    if not item:
        category=_system_category(db) if payload['category_id']=='uncategorized' else db.get(PodArtworkCategory,payload['category_id']) if payload['category_id'] else None
        if payload['category_id'] and not category:raise ConflictError('原分类已删除，请核对后重试')
        asset=Asset(kind='image',name=payload['filename'],source='pod_artwork',local_path='',sha256=payload['sha256'],size_bytes=payload['size_bytes'],width=width,height=height,mime_type=result['mime_type'],oss_bucket=source['oss_bucket'],oss_key=source['oss_key'],oss_url=source['oss_url'],mirror_status='mirrored',read_only=True,metadata_json=dumps({'uploaded_in_erp':True,'pod_asset_kind':'artwork'}))
        db.add(asset);db.flush()
        item=PodArtwork(asset_id=asset.id,category_id=category.id if category else None,name=asset.name,sha256=asset.sha256,mime_type=asset.mime_type,source='erp_upload',source_relative_path='',oss_sync_status='synced',metadata_json=dumps({'oss_asset':source,'thumbnail':thumb}))
        db.add(item);db.flush()
    elif not item.asset.oss_url:
        for key in ('oss_bucket','oss_key','oss_url'):setattr(item.asset,key,source[key])
        item.asset.local_path='';item.asset.mirror_status='mirrored';item.oss_sync_status='synced';item.oss_sync_error=''
    root=get_settings().data_dir/'cloud-thumbnails';root.mkdir(parents=True,exist_ok=True)
    target=root/(payload['sha256']+'.jpg')
    if not target.exists():
        temporary=root/(uuid.uuid4().hex+'.tmp');temporary.write_bytes(preview);os.replace(temporary,target)
    # DB commit is owned by complete_job, atomically with its completion receipt.
    return item
