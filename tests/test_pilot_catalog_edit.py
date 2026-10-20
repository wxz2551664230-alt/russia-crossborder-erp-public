from fastapi.testclient import TestClient
from sqlalchemy import select
from deploy.pilot_gateway import app
from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.models import CatalogProduct, CatalogVariant, Store
from erp.auth_models import UserAuditEvent
from erp.services.accounts import create_user



def test_shared_original_edit_preserves_variants_and_enforces_roles(monkeypatch):
    monkeypatch.setattr(get_settings(),'multiuser_enabled',True)
    monkeypatch.setattr(get_settings(),'session_cookie_secure',False)
    with SessionLocal() as db:
        for name,role in [('editor','operator'),('reader','viewer')]:
            create_user(db,username=name,display_name=name,password='test-only-strong-password',role=role,bootstrap=False)
        db.add(Store(id='edit-store',name='原店铺',platform='ozon'))
        db.add(CatalogProduct(id='edit-product',sku='TEST-EDIT',name='原名称',source_type='follow'))
        db.flush();db.add(CatalogVariant(product_id='edit-product',sku='TEST-EDIT',specification='15绿色恐龙',weight_grams=210));db.commit()
    # Real persisted snapshot, without contacting Ozon.
    import json
    directory=get_settings().data_dir/'ozon_categories';directory.mkdir(exist_ok=True)
    (directory/'edit-store-ZH_HANS.json').write_text(json.dumps({'store_id':'edit-store','options':[{'description_category_id':123,'type_id':456,'display':'原类目'}],'tree':[]}),encoding='utf-8')
    with TestClient(app) as editor, TestClient(app) as reader, TestClient(app) as anonymous:
        for client,name in [(editor,'editor'),(reader,'reader')]:
            assert client.post('/api/v1/auth/login',json={'username':name,'password':'test-only-strong-password'}).status_code==200
        assert anonymous.get('/api/v1/stores').status_code==401
        assert editor.get('/api/v1/stores').json()['items'][0]['name']=='原店铺'
        assert editor.get('/api/v1/stores/edit-store/category-options').json()['options'][0]['type_id']==456
        path='/api/v1/catalog/products/edit-product'
        payload={'name':'改后名称','source_url':'https://detail.1688.com/offer/773453066664.html','purchase_price_cny':7.5,'package_weight_g':210,'category_display':'原类目','description_category_id':123,'type_id':456,'notes':'隔离验收'}
        assert reader.patch(path,json=payload).status_code==403
        assert editor.patch(path,json={'purchase_price_cny':-1}).status_code==422
        assert editor.patch(path,json=payload).status_code==200
        got=reader.get(path).json()
        assert all(got[k]==v for k,v in payload.items())
        assert got['variants'][0]['specification']=='15绿色恐龙'
        assert editor.post('/api/v1/stores/edit-store/categories/sync').status_code==403
        assert editor.post(path+'/assets/prepare',json={}).status_code==403
    with SessionLocal() as db:
        assert any(r.status_code==200 and r.action=='PATCH' for r in db.scalars(select(UserAuditEvent)).all())

