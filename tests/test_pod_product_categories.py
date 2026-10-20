"""Library and Amazon selection must share classifications without API reads."""
import pytest
from sqlalchemy import select

from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps
from erp.domains.pod.integrations.s2b import S2BClient
from erp.domains.pod.models import PodComposition, PodProductCategory
from erp.integrations.amazon import AmazonUSClient
from erp.models import CatalogProduct, CatalogVariant


def seed(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Classification must not call a marketplace or supplier")
    monkeypatch.setattr(S2BClient, '_request', unexpected)
    monkeypatch.setattr(AmazonUSClient, 'request', unexpected)
    with SessionLocal() as db:
        for n, (source, category, name) in enumerate([
            ('pod_amazon', 'Amazon 美国 POD', '美国浴帘'),
            ('pod_amazon', '', '美国浴帘'),
            ('pod_amazon', 'Amazon 美国 POD', '美国鼠标垫'),
            ('pod_amazon', '指定分类', '另一美国款'),
            ('pod', '国内浴帘', '国内浴帘产品'),
        ]):
            p = CatalogProduct(sku=f'S2B-{n + 1:06d}-01', source_type=source, category=category, name=name)
            db.add(p); db.flush()
            db.add(CatalogVariant(product_id=p.id, sku=p.sku, size='72x72', active=True))
        db.add(PodProductCategory(name='指定分类'))
        db.add(PodProductCategory(name='未使用的分类'))
        db.commit()


def test_market_categories_match_amazon_and_filter_before_paging(client, monkeypatch):
    seed(monkeypatch)
    us = client.get('/api/v1/pod/library/categories?market=US').json()
    assert {p['name']: p['count'] for p in us['items']} == {'美国浴帘': 2, '美国鼠标垫': 1, '指定分类': 1}
    cn = client.get('/api/v1/pod/library/categories?market=CN').json()
    assert {p['name']: p['count'] for p in cn['items']} == {'国内浴帘': 1}
    # The unscoped management endpoint retains stored empty/manual categories.
    managed = client.get('/api/v1/pod/library/categories').json()
    assert any(c['name'] == '未使用的分类' and c['count'] == 0 for c in managed['items'])
    assert any(c['name'] == 'Amazon 美国 POD' and c['count'] == 2 for c in managed['items'])
    amz = client.get('/api/v1/amazon/products').json()
    assert {p['label']: p['count'] for p in amz['category_options']} == {p['name']: p['count'] for p in us['items']}
    args = {'market': 'US', 'category': '美国浴帘', 'page_size': 1}
    a = client.get('/api/v1/pod/library/products', params=args).json()
    b = client.get('/api/v1/pod/library/products', params={**args, 'page': 2}).json()
    assert a['total'] == b['total'] == 2 and a['items'][0]['id'] != b['items'][0]['id']
    assert a['items'][0]['product_category'] == b['items'][0]['product_category'] == '美国浴帘'
    detail = client.get('/api/v1/pod/library/products/' + b['items'][0]['id']).json()
    assert detail['product_category'] == '美国浴帘' and detail['category'] == 'Amazon 美国 POD'
    assert client.get('/api/v1/pod/library/products', params={**args, 'market': 'CN'}).json()['total'] == 0
    # A long original product name must be filterable without truncation.
    with SessionLocal() as db:
        p = db.scalar(select(CatalogProduct).where(CatalogProduct.name == '美国鼠标垫'))
        p.name = '美国鼠标垫' + 'X' * 350; long_name = p.name; db.commit()
    assert client.get('/api/v1/pod/library/products', params={**args, 'category': long_name}).json()['total'] == 1


def test_manual_reclassification_updates_both_views_and_preserves_identity(client, monkeypatch):
    seed(monkeypatch)
    with SessionLocal() as db:
        original_products = set(db.scalars(select(CatalogProduct.id)))
        original_variants = set(db.scalars(select(CatalogVariant.id)))
        product_ids = list(db.scalars(select(CatalogProduct.id).where(CatalogProduct.name == '美国浴帘')))
    category = client.post('/api/v1/pod/library/categories', json={'name': '节日浴帘'}).json()['category']
    r = client.patch('/api/v1/pod/library/products/batch-category', json={'product_ids': product_ids, 'category': '节日浴帘'})
    assert r.status_code == 200 and r.json()['updated'] == 2
    assert client.get('/api/v1/amazon/products?category=节日浴帘').json()['total'] == 2
    r = client.patch('/api/v1/pod/library/categories/' + category['id'], json={'name': '浴室浴帘'})
    assert r.status_code == 200
    assert client.get('/api/v1/amazon/products?category=浴室浴帘').json()['total'] == 2
    assert client.get('/api/v1/pod/library/products?market=US&category=浴室浴帘').json()['total'] == 2
    assert client.delete('/api/v1/pod/library/categories/' + category['id']).status_code == 200
    assert client.get('/api/v1/amazon/products?category=美国浴帘').json()['total'] == 2
    with SessionLocal() as db:
        assert set(db.scalars(select(CatalogProduct.id))) == original_products
        assert set(db.scalars(select(CatalogVariant.id))) == original_variants
        assert db.scalar(select(CatalogProduct.category).where(CatalogProduct.source_type == 'pod')) == '国内浴帘'


def test_us_composition_uses_chosen_category_and_keeps_later_manual_category(monkeypatch):
    from erp.domains.pod.composition import _sync_product
    seed(monkeypatch)
    with SessionLocal() as db:
        chosen = PodProductCategory(name='美国生产鼠标垫'); db.add(chosen); db.flush()
        item = PodComposition(supplier='s2b', status='completed', library_sku='S2B-000100-01',
            product_name='美国鼠标垫', product_type_id=7, product_code='US001',
            raw_json=dumps({'market': 'US', 'category_id': chosen.id, 'variant_snapshot': {'produce_country': 'US'}}))
        db.add(item); db.flush()
        p = _sync_product(db, item)
        assert p.source_type == 'pod_amazon' and p.category == '美国生产鼠标垫'
        p.category = '人工重新分类'; product_id = p.id; db.commit()
        p = _sync_product(db, item)
        assert p.id == product_id and p.category == '人工重新分类'
