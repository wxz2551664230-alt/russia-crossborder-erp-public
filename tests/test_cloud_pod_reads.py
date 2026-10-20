from fastapi.testclient import TestClient
from PIL import Image
from deploy.pilot_gateway import app,allowed
from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.models import Asset
from erp.domains.pod.models import PodArtwork

def test_cloud_artwork_without_local_original(monkeypatch):
    monkeypatch.setattr(get_settings(),'web_ui_profile','shared-products-pilot')
    digest='a'*64
    with SessionLocal() as db:
        db.add(Asset(id='cloud-only',name='original.png',local_path='',oss_url='https://example-assets.oss-cn-chengdu.aliyuncs.com/art/original.png',sha256=digest))
        db.flush();db.add(PodArtwork(id='art',asset_id='cloud-only',name='Cloud original',sha256=digest,mime_type='image/png'));db.commit()
    root=get_settings().data_dir/'cloud-thumbnails';root.mkdir(exist_ok=True)
    Image.new('RGB',(32,24)).save(root/(digest+'.jpg'))
    with TestClient(app,headers={'Authorization':'Bearer test-operator-token'}) as client:
        r=client.get('/api/v1/pod/artworks/art/thumbnail');assert r.status_code==200 and r.headers['content-type']=='image/jpeg'
        r=client.get('/api/v1/pod/artworks/art/file',follow_redirects=False)
        assert r.status_code==307 and r.headers['location'].startswith('https://example-assets.')
        assert client.get('/api/v1/pod/artworks/missing/thumbnail').status_code==404
        with SessionLocal() as db:
            a=db.get(Asset,'cloud-only');a.oss_url='https://evil.example/file';db.commit()
        assert client.get('/api/v1/pod/artworks/art/file',follow_redirects=False).status_code==403
        with SessionLocal() as db:
            a=db.get(Asset,'cloud-only');a.mirror_status='deleted';db.commit()
        assert client.get('/api/v1/pod/artworks/art/thumbnail').status_code==410
    assert allowed('POST','/api/v1/pod/compositions')
    assert allowed('POST','/api/v1/pod/supplier-gallery/sync')
    assert allowed('POST','/api/v1/pod/artworks')
    assert not allowed('GET','/api/v1/pod/artworks/art/usable')
