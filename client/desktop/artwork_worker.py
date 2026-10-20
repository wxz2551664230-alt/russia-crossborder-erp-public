"""Validate and prepare uploaded artwork on the initiating user's computer."""
import hashlib,os,shutil,sys,urllib.request,uuid
from pathlib import Path
from erp_desktop import NoRedirect


def run(client,endpoint,job,root,lost):
    runtime=Path(__file__).parent/'runtime'
    if runtime.is_dir():sys.path.insert(0,str(runtime))
    from erp.domains.pod.artworks import _validate_image
    from erp.domains.pod.integrations.oss import PodOssService
    from erp.models import IntegrationConnection
    from erp.core.jsonutil import dumps
    from erp.core.security import vault
    from PIL import Image
    headers={'X-Worker-Attempt':str(job['attempts'])}
    def lease():
        if lost.is_set():raise ValueError('任务连接已失效')
        client.request('POST',endpoint+'/heartbeat',{},extra_headers=headers)
    lease();snapshot=client.request('POST',endpoint+'/artwork-snapshot',{},extra_headers=headers)[0]
    payload=snapshot['payload'];work=root/'artworks'/job['id'];work.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(work).free<512*1024**2+payload['size_bytes']:raise ValueError('本机空间不足，停止上传处理')
    original=next((p for p in (work/'original.png',work/'original.jpg') if p.is_file()),None)
    if original:content=original.read_bytes()
    else:
        request=urllib.request.Request(client.base+'/api/v1'+endpoint+'/artwork-source',headers={**headers,'Cookie':'erp_session='+client.profile['cookie']})
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(request,timeout=60) as response:content=response.read(30*1024**2+1)
    if len(content)!=payload['size_bytes'] or hashlib.sha256(content).hexdigest()!=payload['sha256']:raise ValueError('上传原图校验失败')
    suffix,mime,width,height=_validate_image(content,payload['filename'],payload['content_type'])
    original=work/('original'+suffix)
    if not original.exists():
        temporary=work/(uuid.uuid4().hex+'.tmp');temporary.write_bytes(content);os.replace(temporary,original)
    thumbnail=work/'thumbnail.jpg'
    with Image.open(original) as image:
        image.thumbnail((640,640));image.convert('RGB').save(thumbnail,'JPEG',quality=85)
    connection=dict(snapshot['connection']);secret=connection.pop('secrets')
    # The connection is ephemeral; the encrypted value is never written to disk.
    connection['secrets_encrypted']=vault.encrypt(secret)
    service=PodOssService(IntegrationConnection(**connection))
    def upload(path,digest):
        lease();row=service.upload_artwork(path,digest)
        return {key:row[key] for key in ('oss_bucket','oss_key','oss_url','sha256')}|{'size_bytes':row['size']}
    source=upload(original,payload['sha256'])
    thumb=upload(thumbnail,hashlib.sha256(thumbnail.read_bytes()).hexdigest())
    return {'source':source,'thumbnail':thumb,'mime_type':mime,'width':width,'height':height,'folder':str(work)}
