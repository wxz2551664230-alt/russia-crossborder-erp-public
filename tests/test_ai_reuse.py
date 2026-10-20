import asyncio
import io
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from PIL import Image

from erp.core import ai_reuse


@pytest.fixture(autouse=True)
def cache_root(monkeypatch,tmp_path):
    monkeypatch.setattr(ai_reuse,'get_settings',lambda:SimpleNamespace(data_dir=tmp_path))


def test_paid_image_receipt_atomic_write_in_deep_windows_task_directory(tmp_path):
    directory = tmp_path
    while len(str(directory)) < 165:
        directory /= 'task-store-runtime'
    receipt = directory / ('a' * 64 + '.json')
    assert len(str(receipt)) < 260
    assert len(str(receipt)) + 37 >= 260
    ai_reuse.write_json(receipt, {'data': [{'b64_json': 'cached-paid-result'}]})
    assert ai_reuse.read_json(receipt) == {'data': [{'b64_json': 'cached-paid-result'}]}
    assert not list(directory.glob('*.tmp'))


def test_wb_new_listing_always_fresh_but_same_task_can_resume(monkeypatch):
    from erp.domains.pod import wb_export as wb
    from erp.core.ai_usage import task_scope
    product=SimpleNamespace(id='product',name='tablecloth',supplier='s2b',category='cloth',production_json='{}',source_snapshot_json='{}',variants=[],asset_links=[])
    config={'ai':{'base_url':'https://test.invalid','secrets':{'api_key':'key'},'config':{'default_model':'model'}}}
    payload={'store_id':'shop','subject_name':'cloth','brand':'none','requirement':'plain'}
    calls=[]
    def send(*args):
        calls.append(1);time.sleep(.04)
        return {'title':'title','description':'description','characteristics':[]}
    monkeypatch.setattr(wb,'_request_copy',send)
    def generate(_):
        with task_scope('job-1','wb',1):
            return wb._generate_copy(config,product,{},payload)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(generate,range(4)))
    assert len(calls)==1 and len(results)==4
    with task_scope('job-2','wb',1):
        wb._generate_copy(config,product,{},payload)
    assert len(calls)==2
    with task_scope('job-1','wb',2):
        wb._generate_copy(config,product,{},payload)
        assert len(calls)==2
        wb._generate_copy(config,product,{},dict(payload,store_id='other'))
        wb._generate_copy(config,product,{},dict(payload,requirement='new'))
        product.name='changed facts'
        wb._generate_copy(config,product,{},payload)
    assert len(calls)==5


def test_wb_does_not_cache_failed_generation(monkeypatch):
    from erp.domains.pod import wb_export as wb
    product=SimpleNamespace(id='p',name='n',supplier='s',category='c',production_json='{}',source_snapshot_json='{}',variants=[],asset_links=[])
    config={'ai':{'base_url':'https://test.invalid','secrets':{'api_key':'key'}}}
    payload={'subject_name':'c','brand':'b','requirement':''}
    calls=[]
    def send(*args):
        calls.append(1)
        if len(calls)==1:raise ValueError('invalid response')
        return {'title':'title','description':'description'}
    monkeypatch.setattr(wb,'_request_copy',send)
    with pytest.raises(ValueError):wb._generate_copy(config,product,{},payload)
    wb._generate_copy(config,product,{},payload)
    assert len(calls)==2


def test_wb_rejects_similar_history_after_only_one_rewrite(monkeypatch):
    from erp.domains.pod import wb_export as wb
    from erp.core.errors import AppError
    calls=[]
    old={'title':'Историческое название товара','description':'Историческое описание товара для магазина'}
    monkeypatch.setattr(wb,'_request_copy',lambda *args:calls.append(1) or dict(old))
    with pytest.raises(AppError) as error:
        wb._distinct_copy('endpoint','token','model',[],[old])
    assert error.value.code=='wb_copy_not_distinct' and len(calls)==2


def test_ozon_download_failure_reuses_receipt_but_explicit_regeneration_is_new(monkeypatch,tmp_path):
    from erp.modules.ozon_follow.legacy.services import ai_listing as ai
    source=tmp_path/'source.jpg';Image.new('RGB',(10,10)).save(source)
    service=ai.ImageEditService('https://test.invalid','key','model')
    calls=[];downloads=[]
    monkeypatch.setattr(service,'_generate_payload',lambda *args:calls.append(1) or {'data':[{'url':'https://image.invalid/paid.png'}]})
    stream=io.BytesIO();Image.new('RGB',(10,10)).save(stream,'PNG')
    def download(*args,**kwargs):
        downloads.append(1)
        if len(downloads)==1:raise ai.requests.ReadTimeout('download only')
        return SimpleNamespace(content=stream.getvalue(),raise_for_status=lambda:None)
    monkeypatch.setattr(ai.requests,'get',download)
    with pytest.raises(ai.requests.ReadTimeout):service.generate(str(source),'prompt',tmp_path)
    assert service.generate(str(source),'prompt',tmp_path).is_file()
    assert len(calls)==1 and len(downloads)==2
    assert service.generate(str(source),'prompt',tmp_path).is_file()
    assert len(calls)==2


def test_async_image_receipt_survives_download_failure_and_invalidates(monkeypatch,tmp_path):
    from erp.domains.self_listing.vendor_sources import image_generation as images
    from erp.core.ai_usage import task_scope
    source=tmp_path/'source.jpg';Image.new('RGB',(10,10)).save(source)
    cfg=images.AppConfig(root_dir=str(tmp_path))
    client=SimpleNamespace(cfg=cfg)
    calls=[]
    async def generate(*args,**kwargs):
        calls.append(1)
        return {'data':[{'url':'https://image.invalid/paid.png'}]}
    monkeypatch.setattr(images,'_generate_image_from_plan_item',generate)
    async def run():
        with task_scope('job','self_listing',1):
            args=(client,None,{'id':'one'},source,[])
            assert await images.generate_image_from_plan_item(*args)==await images.generate_image_from_plan_item(*args)
            assert len(calls)==1
            cfg.image_model='changed-model'
            await images.generate_image_from_plan_item(*args)
            Image.new('RGB',(10,10),'red').save(source)
            await images.generate_image_from_plan_item(*args)
        with task_scope('new-job','self_listing',1):
            await images.generate_image_from_plan_item(*args)
    asyncio.run(run())
    assert len(calls)==4


def test_async_wait_cancel_releases_no_other_lock(tmp_path):
    async def run():
        async with ai_reuse.async_result_cache('test','same'):
            async def waiter():
                async with ai_reuse.async_result_cache('test','same'):pass
            task=asyncio.create_task(waiter())
            await asyncio.sleep(.03);task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
        async with ai_reuse.async_result_cache('test','same'):pass
    asyncio.run(run())


def test_ozon_attribute_failure_reuses_only_same_listing_content(monkeypatch):
    import json
    from erp.services import publications as p
    facts=dict(sku='sku',name='tablecloth',package_weight_g=100,package_length_mm=100,package_width_mm=100,package_height_mm=10,assets=[{'role':'main','oss_url':'https://image.invalid/source.jpg'}])
    def child():
        return SimpleNamespace(id='child',store_id='shop',store=SimpleNamespace(name='shop'),status='queued',external_task_id='',assets_json='{}',content_json='{}',attributes_json='[]',complex_attributes_json='[]',offer_id='offer')
    current=child()
    publication=SimpleNamespace(id='publication',product_id='p',product_snapshot_json=json.dumps(facts),requirements_json='{"description_category_id":1,"type_id":2}',image_policy_json='{}',stores=[current])
    session=SimpleNamespace(commit=lambda:None,scalars=lambda query:SimpleNamespace(all=lambda:[]))
    context=SimpleNamespace(session=session,start_step=lambda *a:None,complete_step=lambda *a,**kw:None,event=lambda *a:None)
    monkeypatch.setattr(p,'validate_product_facts',lambda *a:[])
    monkeypatch.setattr(p,'_app_config',lambda snapshot:SimpleNamespace(ai_api_url='url',ai_model='model',vision_model='vision',ozon_client_id='id',ozon_api_key='key',ozon_api_base='base'))
    monkeypatch.setattr(p,'ProductRepository',lambda:None)
    monkeypatch.setattr(p,'OzonSellerClient',lambda *a,**kw:SimpleNamespace(category_attributes=lambda *a:[]))
    monkeypatch.setattr(p,'_price',lambda *a:{'price':1,'old_price':2})
    calls=[]
    def generate(**kwargs):
        calls.append(1)
        draft=kwargs['draft'];draft.title='fresh';draft.description_ru='fresh description'
        draft.ai_preparation.update(content={'title_ru':'fresh'},visual_analysis={},image_stage_status='PENDING')
        kwargs['on_content_checkpoint']()
    monkeypatch.setattr(p,'prepare_listing_with_ai',generate)
    monkeypatch.setattr(p,'normalize_ozon_dictionary_attributes',lambda *a:(_ for _ in ()).throw(ValueError('later step failed')))
    for _ in range(2):
        with pytest.raises(ValueError,match='later step failed'):p.prepare_store_package(context,publication,current,{})
    assert len(calls)==1
    current=child();publication.id='new-publication';publication.stores=[current]
    with pytest.raises(ValueError):p.prepare_store_package(context,publication,current,{})
    assert len(calls)==2
    facts['name']='changed facts';publication.product_snapshot_json=json.dumps(facts)
    with pytest.raises(ValueError):p.prepare_store_package(context,publication,current,{})
    assert len(calls)==3
