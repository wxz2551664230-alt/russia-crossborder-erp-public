import hashlib,io,json
from pathlib import Path
from unittest.mock import patch
import pytest
from PIL import Image
from sqlalchemy import select
from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps,loads
from erp.core.security import vault
from erp.domains.pod.models import PodArtwork,PodComposition,PodCompositionSide
from erp.domains.pod.composition import create_native_composition
from erp.domains.pod.desktop_composition import snapshot,accept_result
from erp.models import Asset,IntegrationConnection,Job,CatalogProduct
from test_personal_job_controls import team


def seed():
    image=io.BytesIO();Image.new('RGB',(12,8),'red').save(image,'PNG');content=image.getvalue()
    with SessionLocal() as db:
        asset=Asset(name='design.png',local_path='',sha256=hashlib.sha256(content).hexdigest(),size_bytes=len(content),mime_type='image/png',oss_url='https://example-assets.oss-cn-chengdu.aliyuncs.com/art.png',oss_bucket='example-assets',oss_key='art.png')
        db.add(asset);db.flush();art=PodArtwork(asset_id=asset.id,name='design',sha256=asset.sha256,mime_type='image/png');db.add(art)
        h=IntegrationConnection(provider='hicustom',name='test',base_url='https://supplier.invalid',secrets_encrypted=vault.encrypt({'app_key':'isolated','app_secret':'test'}),enabled=True)
        o=IntegrationConnection(provider='oss',name='test-oss',config_json=dumps({'prefix':'hicustom-products','bucket':'example-assets','region':'cn-chengdu'}),secrets_encrypted=vault.encrypt({'access_key_id':'isolated','access_key_secret':'test'}),enabled=True)
        db.add_all([h,o]);db.flush()
        request={'supplier':'hicustom','product_type_id':1,'product_name':'Isolated product','default_color_id':1,'default_view_id':1,'selected_size_ids':[2],
                 'variant_snapshot':{'sizes':[{'id':2,'name':'M'}],'colors':[{'id':1,'name':'Red'}],'stock_info':[{'id':3,'color_id':1,'size_id':2,'stock_code':'RED-M'}]},
                 'config_snapshot':{'connection_ids':{'hicustom':h.id}},'placements':[{'view_id':1,'width':100,'height':100,'top_x':0,'top_y':0,'rotation':0,'artwork_id':art.id}]}
        c=create_native_composition(db,request=request,defer_source_preparation=True)
        job=db.get(Job,c.job_id);snap=snapshot(db,job)
    return content,snap


def test_native_handler_runs_in_private_task_database_and_result_import_is_atomic(monkeypatch,tmp_path):
    content,snap=seed();job=snap['job'];job['attempts']=1
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    import composition_worker
    from erp.domains.pod import composition as original
    from erp.domains.pod.integrations.oss import PodOssService
    source_id=snap['assets'][0]['id'];calls=[]
    class Cloud:
        def request(self,method,path,body,**kwargs):return (snap if path.endswith('composition-snapshot') else {},{})
    class Supplier:
        def close(self):pass
        def upload_gallery(self,**kwargs):calls.append('upload');return {'code':'g-1'}
        def create_product(self,payload):calls.append('create');return {'code':'p-1','id':'remote-1'}
        def rendering_groups(self,product,color):return [{'color_id':1,'rendering_urls':['https://supplier.invalid/effect.png'],'saved_files':[]}]
        def download_rendering(self,url):return content,'image/png'
    class Oss:
        def upload_product_asset(self,path,**kwargs):
            digest=hashlib.sha256(path.read_bytes()).hexdigest();key=f"hicustom-products/library/products/{snap['composition']['library_sku']}/renderings/{digest}.png"
            return {'oss_bucket':'example-assets','oss_key':key,'oss_url':'https://example-assets.oss-cn-chengdu.aliyuncs.com/'+key,'sha256':digest}
    def download(_job,root,lost):
        folder=root/'download';folder.mkdir(exist_ok=True);(folder/(source_id+'.png')).write_bytes(content)
        return {'folder':str(folder)}
    import local_worker,threading
    monkeypatch.setattr(local_worker,'restore',download)
    monkeypatch.setattr(original,'supplier_client',lambda *args,**kwargs:Supplier())
    monkeypatch.setattr(PodOssService,'from_session',lambda *args,**kwargs:Oss())
    # Restore process-global settings after the isolated local runner changes them.
    import os
    old=dict(os.environ)
    try:
        result=composition_worker.run(Cloud(),'/local/task',job,tmp_path,threading.Event())
        again=composition_worker.run(Cloud(),'/local/task',job,tmp_path,threading.Event())
        assert result==again and calls==['upload','create']
    finally:
        os.environ.clear();os.environ.update(old);get_settings.cache_clear()
    with SessionLocal() as db:
        assert db.scalar(select(CatalogProduct.id)) is None
        jobrow=db.get(Job,job['id'])
        with patch('erp.domains.pod.desktop_composition.verify_cloud_asset'):
            accept_result(db,jobrow,result)
            db.rollback()
            assert db.scalar(select(CatalogProduct.id)) is None
            accept_result(db,jobrow,result);db.commit()
            accept_result(db,jobrow,result);db.commit()
        products=list(db.scalars(select(CatalogProduct)))
        assert len(products)==1 and len(products[0].variants)==1
        assert products[0].variants[0].specification=='RED-M'
        assert all(not link.asset.local_path for link in products[0].asset_links)


def test_result_cannot_replace_shared_source(monkeypatch):
    _content,snap=seed();c=snap['composition'];side=snap['sides'][0];asset=snap['assets'][0]
    asset['sha256']='f'*64
    result={'product_code':'p','sides':[{'id':side['id'],'asset_id':asset['id'],'gallery_code':'g'}],'assets':[asset],'groups':[{'color_id':1,'asset_ids':[asset['id']]}]}
    from erp.core.errors import ConflictError
    with SessionLocal() as db,pytest.raises(ConflictError,match='共享原图'):
        accept_result(db,db.get(Job,snap['job']['id']),result)


def test_composition_snapshot_is_private_to_active_assigned_lease(team):
    from erp.core.task_identity import task_identity
    clients,worker=team;owner=clients['owner']
    assert owner.post('/api/v1/execution-devices/local/register',json={'id':worker,'name':'PC','composition_version':1}).status_code==200
    actor=owner.get('/api/v1/auth/session').json()['user'];actor['worker_id']=worker
    token=task_identity.set(actor)
    try:_content,snap=seed()
    finally:task_identity.reset(token)
    job_id=snap['job']['id'];base='/api/v1/execution-devices/local/'+worker
    claimed=owner.post(base+'/claim').json();assert claimed['id']==job_id
    path=base+'/jobs/'+job_id+'/composition-snapshot'
    for name in ('other','admin','viewer'):
        assert clients[name].post(path,headers={'X-Worker-Attempt':'1'}).status_code==403
    assert owner.post(path,headers={'X-Worker-Attempt':'2'}).status_code==409
    result=owner.post(path,headers={'X-Worker-Attempt':'1'})
    assert result.status_code==200,result.text
    assert result.headers['cache-control']=='no-store'
    assert {c['provider'] for c in result.json()['connections']}=={'hicustom','oss'}
    assert 'app_secret' not in owner.get('/api/v1/jobs/'+job_id).text
