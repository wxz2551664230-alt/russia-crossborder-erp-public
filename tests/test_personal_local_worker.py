import hashlib
import io
import sys
import threading
from pathlib import Path
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from erp.main import app
from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.models import Asset, CatalogProduct, CatalogProductAsset
from erp.services.accounts import create_user

def test_personal_restore_isolation_and_verified_completion(monkeypatch):
    settings=get_settings()
    monkeypatch.setattr(settings,'multiuser_enabled',True)
    monkeypatch.setattr(settings,'runtime_mode','server')
    monkeypatch.setattr(settings,'session_cookie_secure',False)
    product='a'*32;asset='b'*32;device='pc-'+'c'*32;other='pc-'+'d'*32
    with SessionLocal() as db:
        for name,role in [('first','operator'),('second','operator'),('reader','viewer')]:
            create_user(db,username=name,display_name=name,password='isolated-long-test-password',role=role)
        db.add(CatalogProduct(id=product,sku='POD-ISOLATED',source_type='pod'))
        db.add(Asset(id=asset,name='a.png',local_path='',oss_url='https://example-assets.oss-cn-chengdu.aliyuncs.com/a.png',sha256='e'*64,size_bytes=42,mime_type='image/png'))
        db.flush();db.add(CatalogProductAsset(product_id=product,asset_id=asset,is_current=True));db.commit()
    prefix='/api/v1/execution-devices'
    with ExitStack() as stack:
        clients=[stack.enter_context(TestClient(app)) for _ in range(4)]
        browser,worker,second,reader=clients
        for client,name in zip(clients,['first','first','second','reader']):
            assert client.post('/api/v1/auth/login',json={'username':name,'password':'isolated-long-test-password'}).status_code==200
        assert reader.post(prefix+'/local/register',json={'id':device,'name':'PC'}).status_code==403
        assert worker.post(prefix+'/local/register',json={'id':device,'name':'PC'}).status_code==200
        assert second.post(prefix+'/local/register',json={'id':device,'name':'PC'}).status_code==409
        assert second.post(prefix+'/local/register',json={'id':other,'name':'Other'}).status_code==200
        assert browser.post(prefix+'/select',json={'worker_id':device}).status_code==200
        response=browser.post('/api/v1/pod/library/products/'+product+'/restore')
        assert response.status_code==202,response.text
        job=response.json()['job'];assert job['target_worker_id']==device
        assert second.post(prefix+'/local/'+other+'/claim',json={}).json() is None
        assert worker.post(prefix+'/local/'+device+'/claim',json={}).json()['id']==job['id']
        endpoint=prefix+'/local/'+device+'/jobs/'+job['id']+'/complete'
        result={'files':[{'id':asset,'bytes':42,'sha256':'e'*64}]}
        assert second.post(endpoint,json={'result':result},headers={'X-Worker-Attempt':'1'}).status_code==403
        assert worker.post(endpoint,json={'result':result},headers={'X-Worker-Attempt':'2'}).status_code==409
        for invalid in [[],[{'id':[],'bytes':42,'sha256':'e'*64}],[{'id':asset,'bytes':43,'sha256':'e'*64}]]:
            assert worker.post(endpoint,json={'result':{'files':invalid}},headers={'X-Worker-Attempt':'1'}).status_code==409
        for _ in range(2):
            response=worker.post(endpoint,json={'result':result},headers={'X-Worker-Attempt':'1'})
            assert response.status_code==200,response.text
            assert response.json()['status']=='completed'
        with SessionLocal() as db:assert db.get(Asset,asset).local_path==''

def test_client_download_verifies_hash_and_keeps_existing_files(monkeypatch,tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    import local_worker
    content=b'isolated asset content'
    class Response(io.BytesIO):pass
    class Opener:
        def open(self,*args,**kwargs):return Response(content)
    monkeypatch.setattr(local_worker.urllib.request,'build_opener',lambda *args:Opener())
    monkeypatch.setattr(local_worker.shutil,'disk_usage',lambda p:type('Disk',(),{'free':2*1024**3})())
    item={'id':'b'*32,'url':'https://example-assets.oss-cn-chengdu.aliyuncs.com/a.png','sha256':hashlib.sha256(content).hexdigest(),'size_bytes':len(content),'mime_type':'image/png'}
    job={'id':'a'*32,'payload':{'product_id':'c'*32,'files':[item]}}
    first=local_worker.restore(job,tmp_path,threading.Event())
    assert local_worker.restore(job,tmp_path,threading.Event())==first
    target=Path(first['folder'])/(item['id']+'.png');target.write_bytes(b'different')
    with pytest.raises(ValueError,match='已有'):local_worker.restore(job,tmp_path,threading.Event())
    assert target.read_bytes()==b'different'
    job['id']='d'*32;item['sha256']='f'*64
    with pytest.raises(ValueError,match='校验失败'):local_worker.restore(job,tmp_path,threading.Event())
    assert not list((tmp_path/'restored'/job['id']).iterdir())
    item['url']='https://other.example/file'
    with pytest.raises(ValueError,match='OSS'):local_worker.restore(job,tmp_path,threading.Event())
