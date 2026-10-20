import json
import time
import pytest

from erp.core.database import SessionLocal
from erp.models import Store, CatalogProduct, Publication, PublicationStore
from erp.services import ozon_categories as cache


@pytest.mark.parametrize('kind', ['promotions', 'warehouses'])
def test_empty_options_cached_and_expiry_reloads(kind):
    cache.save_options_cache('isolated-'+kind, kind, {}, [])
    assert cache.load_options_cache('isolated-'+kind, kind, {}) == []
    path=cache._options_path('isolated-'+kind, kind, {})
    value=json.loads(path.read_text())
    value['saved_at']=time.time()-301
    path.write_text(json.dumps(value))
    assert cache.load_options_cache('isolated-'+kind, kind, {}) is None


def test_changed_review_rejected_before_approval(client):
    with SessionLocal() as db:
        store=Store(platform='ozon',name='review-test')
        product=CatalogProduct(sku='review-test')
        db.add_all([store,product]);db.flush()
        p=Publication(batch_id='test',product_id=product.id)
        db.add(p);db.flush()
        child=PublicationStore(publication_id=p.id,store_id=store.id,offer_id='test',
            idempotency_key='test',status='image_review',assets_json=json.dumps({'personal_review':{'revision':'new','sha256':'new'}}))
        db.add(child);db.commit()
        pid,sid=p.id,store.id
    response=client.post(f'/api/v1/publications/{pid}/stores/{sid}/image/approve',
        json={'expected_review':{'revision':'old','sha256':'old'}})
    assert response.status_code==409
    assert '候选图片已变化' in response.text
    with SessionLocal() as db:
        assert db.get(Publication,pid).stores[0].status=='image_review'
