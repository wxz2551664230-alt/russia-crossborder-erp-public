import hashlib,io,threading
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from sqlalchemy import select,func
from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps
from erp.core.security import vault
from erp.models import IntegrationConnection,Asset
from erp.domains.pod.models import PodArtwork
from test_personal_job_controls import team


def test_upload_owner_lease_worker_and_cloud_dedup(team,monkeypatch,tmp_path):
    clients,device=team;owner=clients['owner'];base='/api/v1/execution-devices/local/'+device
    content=io.BytesIO();Image.new('RGB',(24,32),'blue').save(content,'PNG');content=content.getvalue()
    old=owner.post('/api/v1/pod/artworks',files={'image':('design.png',content,'image/png')})
    assert old.status_code==409
    owner.post('/api/v1/execution-devices/local/register',json={'id':device,'name':'test','artwork_version':1})
    assert clients['viewer'].post('/api/v1/pod/artworks',files={'image':('design.png',content,'image/png')}).status_code==403
    with SessionLocal() as db:
        db.add(IntegrationConnection(provider='oss',name='test',enabled=True,config_json=dumps({'prefix':'hicustom-products'}),secrets_encrypted=vault.encrypt({'access_key_secret':'isolated-only'})));db.commit()
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    import artwork_worker
    objects={}
    class FakeOSS:
        def __init__(self,connection):pass
        def upload_artwork(self,path,digest):
            data=path.read_bytes();assert hashlib.sha256(data).hexdigest()==digest
            key=f'hicustom-products/artwork-library/{digest[:2]}/{digest}{path.suffix}'
            url='https://example-assets.oss-cn-chengdu.aliyuncs.com/'+key;objects[url]=data
            return {'oss_bucket':'example-assets','oss_key':key,'oss_url':url,'sha256':digest,'size':len(data)}
    class Download:
        def open(self,*args,**kwargs):return io.BytesIO(content)
    class Response:
        def __init__(self,data):self.data=data
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def raise_for_status(self):pass
        def iter_bytes(self):yield self.data
    class HTTP:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def stream(self,method,url):return Response(objects[url])
    class Client:
        profile={'cookie':'test'};base='https://erp.invalid'
        def request(self,method,path,body=None,extra_headers=None):
            r=owner.request(method,'/api/v1'+path,json=body,headers=extra_headers)
            assert r.status_code==200,r.text
            return r.json(),r.headers
    monkeypatch.setattr(artwork_worker.urllib.request,'build_opener',lambda *args:Download())
    monkeypatch.setattr('erp.domains.pod.integrations.oss.PodOssService',FakeOSS)
    for _ in range(2):
        r=owner.post('/api/v1/pod/artworks',files={'image':('design.png',content,'image/png')});assert r.status_code==201,r.text
        job=owner.post(base+'/claim').json();assert job['id']==r.json()['job_id']
        endpoint=base+'/jobs/'+job['id'];headers={'X-Worker-Attempt':str(job['attempts'])}
        for other in ('other','admin','viewer'):
            assert clients[other].post(endpoint+'/artwork-snapshot',headers=headers).status_code==403
        assert owner.post(endpoint+'/artwork-snapshot',headers={'X-Worker-Attempt':'99'}).status_code==409
        result=artwork_worker.run(Client(),endpoint.removeprefix('/api/v1'),job,tmp_path,threading.Event())
        with patch('erp.domains.pod.desktop_artwork.verify_cloud_asset'),patch('erp.domains.pod.desktop_artwork.httpx.Client',HTTP):
            bad={**result,'source':{**result['source'],'sha256':'f'*64}}
            assert owner.post(endpoint+'/complete',headers=headers,json={'result':bad}).status_code==409
            completed=owner.post(endpoint+'/complete',headers=headers,json={'result':result});assert completed.status_code==200,completed.text
            assert owner.post(endpoint+'/complete',headers=headers,json={'result':result}).status_code==200
        lookup=owner.get('/api/v1/pod/artworks/upload-result/'+job['id']);assert lookup.json()['artwork']['oss_uploaded']
        assert owner.get(endpoint+'/artwork-source',headers=headers).status_code==409
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(PodArtwork))==1
        asset=db.scalar(select(Asset));assert asset.local_path=='' and asset.mirror_status=='mirrored'
