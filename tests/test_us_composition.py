import hashlib
import io
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image
from sqlalchemy import select

from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps, loads
from erp.core.security import vault
from erp.domains.pod.models import PodArtwork, PodSupplierCatalogSnapshot
from erp.models import Asset, CatalogProduct, IntegrationConnection, Job
from test_personal_job_controls import team


def seed(owner, worker, version=2):
    assert owner.post('/api/v1/execution-devices/local/register', json={
        'id':worker,'name':'PC','composition_version':version}).status_code==200
    image=io.BytesIO();Image.new('RGB',(120,80),'green').save(image,'PNG');content=image.getvalue()
    with SessionLocal() as db:
        asset=Asset(name='design.png',sha256=hashlib.sha256(content).hexdigest(),size_bytes=len(content),
                    mime_type='image/png',oss_url='https://example-assets.oss-cn-chengdu.aliyuncs.com/art.png',oss_bucket='example-assets',oss_key='art.png')
        db.add(asset);db.flush();art=PodArtwork(asset_id=asset.id,name='design',sha256=asset.sha256,mime_type='image/png');db.add(art);db.flush()
        for provider,config in [('s2b',{}),('oss',{'prefix':'hicustom-products','bucket':'example-assets','region':'cn-chengdu'})]:
            db.add(IntegrationConnection(provider=provider,name=provider,enabled=True,base_url='https://supplier.invalid',
                config_json=dumps(config),secrets_encrypted=vault.encrypt({'app_key':'test','app_secret':'test','access_key_id':'test','access_key_secret':'test'})))
        product={'id':7,'supplier':'s2b','source_id':'7','catalog_key':'s2b:7','produce_country':'US','name':'US mouse pad'}
        detail={**product,'sizes':[{'id':2,'name':'40x80cm'}],'colors':[{'id':1,'name':'White'}],
                'views':[{'id':1,'name':'A'}],'print_areas':[{'view_id':1,'width':800,'height':400}],
                'items':[{'id':3,'color_id':1,'size_id':2,'price':25.9,'length':20,'width':30,'height':2,'weight':500,'code':'US-XL'}]}
        db.add(PodSupplierCatalogSnapshot(provider='amazon_us',is_current=True,payload_json=dumps({
            'products':[product],'details':{'s2b:7':detail},'categories':[],'synced_at':'now'})))
        db.commit()
    request={'market':'US','supplier':'s2b','product_type_id':7,'product_name':'US mouse pad','default_color_id':1,
        'default_view_id':1,'selected_size_ids':[2],'placements':[{'view_id':1,'width':800,'height':400,'top_x':0,'top_y':0,'artwork_id':art.id}]}
    return request,content


def test_us_composer_cached_country_specs_and_old_client_fencing(team,monkeypatch):
    clients,worker=team;owner=clients['owner'];request,_=seed(owner,worker,version=1)
    from erp.domains.pod.integrations.s2b import S2BClient
    monkeypatch.setattr(S2BClient,'_request',lambda *a,**kw:pytest.fail('Cloud composer must not call supplier'))
    assert owner.get('/api/v1/pod/us-composer/catalog').json()['products'][0]['produce_country']=='US'
    detail=owner.get('/api/v1/pod/us-composer/products/s2b:7').json()
    assert detail['prices']['currency']=='CNY' and detail['stock_info'][0]['price']==25.9
    assert owner.get('/api/v1/pod/us-composer/products/hicustom:7').status_code in {403,404}
    assert owner.post('/api/v1/pod/compositions',json=request).status_code==409
    assert owner.post('/api/v1/execution-devices/local/register',json={'id':worker,'name':'PC','composition_version':2}).status_code==200
    for field,value in [('default_color_id',9),('selected_size_ids',[999]),('market','CA')]:
        assert owner.post('/api/v1/pod/compositions',json={**request,field:value}).status_code in {409,422}
    with SessionLocal() as db:
        snap=db.scalar(select(PodSupplierCatalogSnapshot));data=loads(snap.payload_json,{});data['details']={};snap.payload_json=dumps(data);db.commit()
    assert owner.post('/api/v1/pod/compositions',json=request).status_code==409
    with SessionLocal() as db:assert db.query(Job).count()==0


def test_us_composition_runs_on_origin_pc_replays_and_imports_isolated_product(team,monkeypatch,tmp_path):
    clients,worker=team;owner=clients['owner'];request,content=seed(owner,worker)
    created=owner.post('/api/v1/pod/compositions',json=request);assert created.status_code==202,created.text
    job=created.json()['job'];assert job['type']=='pod_us_composition' and job['target_worker_id']==worker and job['max_attempts']==1
    base='/api/v1/execution-devices/local/'+worker
    assert owner.post(base+'/claim').json()['id']==job['id']
    endpoint=base+'/jobs/'+job['id'];headers={'X-Worker-Attempt':'1'}
    assert clients['other'].post(endpoint+'/composition-snapshot',headers=headers).status_code==403
    snap=owner.post(endpoint+'/composition-snapshot',headers=headers).json();source_id=snap['assets'][0]['id']
    assert loads(snap['composition']['raw_json'],{})['variant_snapshot']['produce_country']=='US'
    assert 'app_secret' not in owner.get('/api/v1/jobs/'+job['id']).text
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    import composition_worker,local_worker
    from erp.domains.pod import composition as original
    from erp.domains.pod.integrations.oss import PodOssService
    calls=[]
    class Cloud:
        def request(self,method,path,body,**kwargs):return (snap if path.endswith('composition-snapshot') else {},{})
    class Supplier:
        def close(self):pass
        def upload_gallery(self,**kw):calls.append('upload');return {'code':'77'}
        def create_product(self,payload):
            assert payload['market']=='US' and payload['product_type_id']==7
            calls.append('create');return {'code':'US001','id':'123'}
        def rendering_groups(self,*args):return [{'color_id':1,'rendering_urls':['https://supplier.invalid/effect.png'],'saved_files':[]}]
        def download_rendering(self,url):return content,'image/png'
    class Oss:
        def upload_product_asset(self,path,**kw):
            digest=hashlib.sha256(path.read_bytes()).hexdigest();key=f"hicustom-products/library/products/{snap['composition']['library_sku']}/{digest}.png"
            return {'oss_bucket':'example-assets','oss_key':key,'oss_url':'https://example-assets.oss-cn-chengdu.aliyuncs.com/'+key,'sha256':digest}
    def download(_job,root,lost):
        folder=root/'download';folder.mkdir(exist_ok=True);(folder/(source_id+'.png')).write_bytes(content);return {'folder':str(folder)}
    monkeypatch.setattr(local_worker,'restore',download)
    monkeypatch.setattr(original,'supplier_client',lambda *a,**kw:Supplier())
    monkeypatch.setattr(PodOssService,'from_session',lambda *a,**kw:Oss())
    old=dict(os.environ)
    try:
        result=composition_worker.run(Cloud(),endpoint,{**job,'attempts':1},tmp_path,threading.Event())
        assert result==composition_worker.run(Cloud(),endpoint,{**job,'attempts':1},tmp_path,threading.Event())
        assert calls==['upload','create']
    finally:os.environ.clear();os.environ.update(old);get_settings.cache_clear()
    # The PC runner deliberately resets its private settings. Restore the
    # cloud test gateway settings before receiving the result in this process.
    settings=get_settings()
    monkeypatch.setattr(settings,'multiuser_enabled',True)
    monkeypatch.setattr(settings,'runtime_mode','server')
    monkeypatch.setattr(settings,'session_cookie_secure',False)
    with patch('erp.domains.pod.desktop_composition.verify_cloud_asset'):
        finished=owner.post(endpoint+'/complete',json={'result':result},headers=headers)
    assert finished.status_code==200,finished.text
    us=owner.get('/api/v1/pod/library/products?market=US').json();cn=owner.get('/api/v1/pod/library/products').json()
    assert us['total']==1 and cn['total']==0 and us['items'][0]['source_type']=='pod_amazon'
    detail=owner.get('/api/v1/pod/library/products/'+us['items'][0]['id']).json()
    assert detail['category']=='Amazon 美国 POD' and detail['purchase_price_cny']==25.9 and detail['currency']=='CNY'
    assert detail['variants'][0]['package_length_mm']==200 and detail['variants'][0]['weight_grams']==500
    assert detail['variants'][0]['properties']['supplier_price_raw']==25.9
    assert detail['versions'][0]['recipe']['market']=='US' and detail['versions'][0]['product_code']=='US001'
    assert all(not a['asset']['local_path'] for a in detail['assets'])
    from erp.services.publications import product_snapshot
    from erp.core.errors import ConflictError
    with SessionLocal() as db,pytest.raises(ConflictError,match='Amazon'):
        product_snapshot(db.get(CatalogProduct,detail['id']),None)
