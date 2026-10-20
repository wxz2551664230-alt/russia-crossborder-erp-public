from types import SimpleNamespace

import pytest

from erp.core.errors import ConflictError
from erp.modules.ozon_follow.legacy.config import AppConfig
from erp.modules.ozon_follow.legacy.services.ozon_listing import OzonListingDraft
from erp.services import ozon_personal_ai as bridge


def test_snapshot_is_limited_to_selected_store_and_contains_original_draft(monkeypatch):
    product = SimpleNamespace(source_sku='SKU-1', title='原产品', brand='品牌',
                              image_url='https://example.com/1.jpg',
                              image_urls_json='["https://example.com/1.jpg"]',
                              ozon_url='https://ozon.ru/product/1', weight_grams=200)
    listing = SimpleNamespace(id='listing-1', research_product=product)
    children = [SimpleNamespace(store_id='shop-a', store=SimpleNamespace(name='甲店')),
                SimpleNamespace(store_id='shop-b', store=SimpleNamespace(name='乙店'))]
    config_calls = []

    monkeypatch.setattr(bridge, '_load', lambda _context: (listing, children))
    monkeypatch.setattr(bridge, '_draft', lambda _listing, child: SimpleNamespace(
        to_dict=lambda: {'offer_id': f'{child.store_id}-SKU-1', 'title': '原文案'}))

    def config(_context, child):
        config_calls.append(child.store_id)
        return AppConfig(ozon_api_key=f'private-{child.store_id}')

    monkeypatch.setattr(bridge, '_config', config)
    job = SimpleNamespace(id='job-1', type='ozon_ai_prepare')
    result = bridge.snapshot(None, job, 'shop-b')
    assert result['store_id'] == 'shop-b'
    assert result['draft']['offer_id'] == 'shop-b-SKU-1'
    assert result['source']['sku'] == 'SKU-1'
    assert result['config']['ozon_api_key'] == 'private-shop-b'
    assert config_calls == ['shop-b']
    assert 'private-shop-b' not in result['revision']
    assert len(result['revision']) == 64
    with pytest.raises(ConflictError):
        bridge.snapshot(None, job, 'shop-c')
    assert config_calls == ['shop-b']


def test_snapshot_rejects_other_job_type_before_reading_data(monkeypatch):
    monkeypatch.setattr(bridge, '_load', lambda _context: pytest.fail('must not load'))
    with pytest.raises(ConflictError):
        bridge.snapshot(None, SimpleNamespace(id='job-2', type='worker_probe'), 'shop-a')


def test_result_keeps_original_identity_and_accepts_one_store_once(monkeypatch):
    product = SimpleNamespace(source_sku='SKU-2', title='原产品', brand='品牌',
                              image_url='https://example.com/2.jpg', image_urls_json='[]',
                              ozon_url='https://ozon.ru/product/2', weight_grams=200)
    listing = SimpleNamespace(id='listing-2', research_product=product,
                              title='原产品', description_ru='')
    child = SimpleNamespace(store_id='shop-a', store=SimpleNamespace(name='甲店'),
                            content_json='{}', assets_json='{}', attributes_json='[]',
                            complex_attributes_json='[]', status='ai_preparing', last_error='')
    step = SimpleNamespace(key='shop-a:ai_prepare', status='pending',
                           details_json='{}', progress=0, finished_at=None)
    job = SimpleNamespace(id='job-3', type='ozon_ai_prepare', steps=[step])
    original = OzonListingDraft(source_sku='SKU-2', offer_id='A-SKU-2',
                                base_offer_id='SKU-2', source_ozon_url=product.ozon_url,
                                title='原产品', exported_category='家居')
    monkeypatch.setattr(bridge, '_load', lambda _context: (listing, [child]))
    monkeypatch.setattr(bridge, '_draft', lambda _listing, _child: original)
    revision = bridge._revision(bridge._source(listing), original.to_dict())
    result = original.to_dict()
    result.update(title='新文案', description_ru='新的俄文介绍',
                  ai_preparation={'image_review_status': 'PENDING'})
    tampered = {**result, 'offer_id': 'OTHER'}
    with pytest.raises(ConflictError):
        bridge.accept_result(None, job, 'shop-a', revision=revision, draft_data=tampered)
    assert step.status == 'pending'
    saved = bridge.accept_result(None, job, 'shop-a', revision=revision, draft_data=result)
    assert saved['status'] == 'image_review'
    assert child.status == 'image_review'
    assert listing.title == '新文案'
    assert step.status == 'completed'
    assert bridge.completion_summary(job) == {'schema': 1, 'stores': {'shop-a': saved['result_hash']}}
    assert bridge.accept_result(None, job, 'shop-a', revision=revision, draft_data=result) == saved
    with pytest.raises(ConflictError):
        bridge.accept_result(None, job, 'shop-a', revision=revision,
                             draft_data={**result, 'title': '另一份文案'})


def test_completion_requires_every_store_receipt():
    job = SimpleNamespace(steps=[SimpleNamespace(key='shop-a:ai_prepare', status='pending',
                                                details_json='{}')])
    with pytest.raises(ConflictError):
        bridge.completion_summary(job)
