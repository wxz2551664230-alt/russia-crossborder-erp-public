"""Task-scoped transport for the unchanged native composition handler."""
import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit, unquote

import httpx
from sqlalchemy import DateTime, select

from erp.core.config import get_settings
from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps, loads
from erp.core.security import vault
from erp.domains.pod.models import PodArtwork, PodComposition, PodCompositionSide, PodRemoteGalleryAsset
from erp.models import Asset, IntegrationConnection


def columns(row):
    return {c.name:getattr(row,c.name) for c in row.__table__.columns if not isinstance(c.type,DateTime)}


def verify_market(job, item):
    raw=loads(item.raw_json,{})
    if (raw.get('market')=='US') != (job.type=='pod_us_composition'):
        raise ConflictError('合成任务与货盘国家不匹配')


def snapshot(db, job):
    item=db.get(PodComposition,loads(job.payload_json,{})['composition_id'])
    if not item:raise ConflictError('合成记录不存在')
    verify_market(job,item)
    sides=list(db.scalars(select(PodCompositionSide).where(PodCompositionSide.composition_id==item.id)))
    connection_ids=loads(item.config_snapshot_json,{}).get('connection_ids',{})
    provider=db.get(IntegrationConnection,connection_ids.get(item.supplier,''))
    oss=next((c for c in db.scalars(select(IntegrationConnection).where(IntegrationConnection.provider=='oss',IntegrationConnection.enabled.is_(True))) if loads(c.config_json,{}).get('prefix','').strip('/')=='hicustom-products'),None)
    if not provider or not provider.enabled or provider.provider!=item.supplier or not oss:
        raise ConflictError('合成所需供应商或OSS连接不可用')
    # Never place secrets in public job payloads, events or completion receipts.
    connections=[]
    for c in (provider,oss):
        row=columns(c);row.pop('secrets_encrypted',None);row['secrets']=vault.decrypt(c.secrets_encrypted)
        connections.append(row)
    assets=[]
    for identity in dict.fromkeys(s.asset_id for s in sides):
        a=db.get(Asset,identity)
        if not a or a.mirror_status=='deleted':raise ConflictError('任务素材不可用')
        row=columns(a);row['local_path']='';assets.append(row)
    artworks=[columns(db.get(PodArtwork,s.artwork_id)) for s in sides if s.artwork_id]
    return {'composition':columns(item),'sides':[columns(s) for s in sides], 'assets':assets,
            'artworks':artworks,'connections':connections,'job':columns(job),'steps':[columns(s) for s in job.steps]}


def source_path(db,job,asset_id):
    cid=loads(job.payload_json,{})['composition_id']
    side=db.scalar(select(PodCompositionSide).where(PodCompositionSide.composition_id==cid,PodCompositionSide.asset_id==asset_id))
    asset=db.get(Asset,asset_id) if side else None
    path=Path(asset.local_path).resolve() if asset and asset.local_path else None
    if not path or not path.is_relative_to(get_settings().data_dir.resolve()) or not path.is_file():
        raise ConflictError('该任务的临时上传素材不存在')
    return path


def verify_cloud_asset(row,sku,existing=None):
    url=urlsplit(str(row.get('oss_url','')))
    if url.scheme!='https' or url.hostname!='example-assets.oss-cn-chengdu.aliyuncs.com' or url.username or url.password or url.query or url.fragment or url.port not in (None,443):
        raise ConflictError('结果必须保存到本账户OSS')
    key=str(row.get('oss_key',''))
    if unquote(url.path).lstrip('/')!=key or row.get('oss_bucket')!='example-assets':raise ConflictError('OSS结果路径不一致')
    if not (existing and existing.oss_key==key and existing.sha256==row.get('sha256')) and not key.startswith(f'hicustom-products/library/products/{sku}/'):
        raise ConflictError('结果不是当前合成任务的资产')
    if not re.fullmatch('[a-f0-9]{64}',str(row.get('sha256',''))) or type(row.get('size_bytes')) is not int or not 0<row['size_bytes']<=128*1024**2:
        raise ConflictError('结果文件校验信息无效')
    with httpx.Client(timeout=30,follow_redirects=False,trust_env=False) as client:
        response=client.head(row['oss_url']);response.raise_for_status()
    if int(response.headers.get('content-length','0'))!=row['size_bytes'] or response.headers.get('x-oss-meta-sha256','')!=row['sha256']:
        raise ConflictError('OSS结果正文元数据校验不一致')


def accept_result(db,job,result):
    from erp.domains.pod.composition import _sync_product
    cid=loads(job.payload_json,{})['composition_id'];item=db.get(PodComposition,cid)
    if not item or not isinstance(result,dict):raise ConflictError('合成结果无效')
    verify_market(job,item)
    receipt=hashlib.sha256(dumps(result).encode()).hexdigest()
    raw=loads(item.raw_json,{})
    if raw.get('desktop_receipt'):
        if raw['desktop_receipt']!=receipt:raise ConflictError('已入库任务拒绝不同结果')
        return
    sides=list(db.scalars(select(PodCompositionSide).where(PodCompositionSide.composition_id==cid)))
    returned=result.get('sides',[]);assets=result.get('assets',[]);groups=result.get('groups',[])
    if not isinstance(returned,list) or any(not isinstance(s,dict) or not isinstance(s.get('id'),str) or not isinstance(s.get('asset_id'),str) for s in returned):raise ConflictError('合成印刷面格式无效')
    if not isinstance(assets,list) or any(not isinstance(a,dict) or not isinstance(a.get('id'),str) or not isinstance(a.get('name'),str) for a in assets):raise ConflictError('合成资产格式无效')
    if not isinstance(groups,list) or any(not isinstance(g,dict) or not isinstance(g.get('asset_ids'),list) or any(not isinstance(i,str) for i in g['asset_ids']) for g in groups):raise ConflictError('合成图片分组无效')
    if not isinstance(returned,list) or {s.get('id') for s in returned} != {s.id for s in sides} or len(returned)!=len(sides):raise ConflictError('合成印刷面结果不完整')
    if not result.get('product_code') or not groups or not isinstance(assets,list) or len(assets)>1000:raise ConflictError('合成结果不完整')
    amap={a['id']:a for a in assets}
    wanted={s['asset_id'] for s in returned}|{identity for g in groups for identity in g.get('asset_ids',[])}
    if not wanted or wanted!=set(amap) or len(amap)!=len(assets):raise ConflictError('结果资产关联不完整')
    if item.product_code and item.product_code!=result['product_code']:raise ConflictError('不得替换已有供应商产品')
    source_ids={s.asset_id for s in sides}
    for identity,row in amap.items():
        if not re.fullmatch('[a-f0-9]{32}',identity):raise ConflictError('资产编号无效')
        old=db.get(Asset,identity)
        if old and identity not in source_ids and loads(old.metadata_json,{}).get('composition_id')!=cid:raise ConflictError('禁止覆盖其他任务的资产')
        if old and identity in source_ids and (old.sha256!=row.get('sha256') or old.size_bytes!=row.get('size_bytes')):raise ConflictError('不得改写共享原图')
        verify_cloud_asset(row,item.library_sku,old if identity in source_ids else None)
    # Validate everything before mutation; final task completion commits this transaction.
    for identity,row in amap.items():
        asset=db.get(Asset,identity)
        if not asset:asset=Asset(id=identity,kind='image',source='pod_rendering',metadata_json=dumps({'composition_id':cid}));db.add(asset)
        fields=('oss_bucket','oss_key','oss_url') if identity in source_ids else ('name','sha256','size_bytes','mime_type','width','height','oss_bucket','oss_key','oss_url')
        for key in fields:
            if key in row:setattr(asset,key,row[key])
        asset.local_path='';asset.mirror_status='mirrored';asset.read_only=True
    db.flush();returned_by_id={s['id']:s for s in returned};new_sides=[]
    for side in sides:
        value=returned_by_id[side.id];side.asset_id=value['asset_id'];side.gallery_code=str(value.get('gallery_code') or '')
        if not side.gallery_code:raise ConflictError('供应商素材编号缺失')
        side.source_legacy_path='';side.source_filename=amap[side.asset_id]['name'];side.upload_status='completed'
        original=next(s for s in raw['sides'] if s['side_id']==side.id)
        new_sides.append({**original,'asset_id':side.asset_id,'gallery_code':side.gallery_code,'source_path':'','source_filename':side.source_filename,'source_prepared':True})
        if not db.scalar(select(PodRemoteGalleryAsset.id).where(PodRemoteGalleryAsset.supplier==item.supplier,PodRemoteGalleryAsset.gallery_code==side.gallery_code)):
            db.add(PodRemoteGalleryAsset(artwork_id=side.artwork_id,composition_side_id=side.id,supplier=item.supplier,gallery_code=side.gallery_code,usage='pending',protected=False,cleanup_candidate=True,product_name=item.product_name,filename=side.source_filename))
    cleaned_groups=[{**g,'saved_files':[]} for g in groups]
    item.product_code=str(result['product_code']);item.remote_product_id=str(result.get('remote_product_id') or '')
    item.rendering_groups_json=dumps(cleaned_groups)
    item.oss_assets_json=dumps([{'asset_id':a['id'],'oss_url':a['oss_url'],'oss_key':a['oss_key'],'sha256':a['sha256']} for a in assets])
    raw.update(sides=new_sides,folder='',saved_files=[],desktop_receipt=receipt)
    item.raw_json=dumps(raw);db.flush()
    _sync_product(db,item,commit=False)
    item.status='completed';item.stage_text='本机合成完成并已进入共享产品库';item.error_message='';item.outcome_unknown=False
