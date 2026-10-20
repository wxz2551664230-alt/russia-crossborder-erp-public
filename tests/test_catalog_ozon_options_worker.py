from pathlib import Path
from types import SimpleNamespace as Obj
from threading import Event
import pytest


@pytest.mark.parametrize('kind', ['warehouses', 'promotions', 'category_tree'])
def test_options_read_only_and_store_fence(monkeypatch, kind):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from catalog_ozon_options_worker import run
    import erp.integrations.ozon as ozon
    calls=[]
    def adapter(config, repository):
        assert repository is not None
        return Obj(**{kind: lambda: calls.append(kind) or [{'id': 1, 'name': 'choice'}]})
    monkeypatch.setattr(ozon, 'OzonSellerAdapter', adapter)
    client=Obj(request=lambda *args, **kwargs: [{'store_id': 'store', 'platform': 'ozon'}])
    job={'payload': {'store_id': 'store', 'kind': kind}, 'attempts': 1}
    assert run(client, '/job', job, '.', Event())['items']==[{'id':1,'name':'choice'}]
    assert calls==[kind]
    job['payload']['store_id']='different'
    with pytest.raises(ValueError): run(client, '/job', job, '.', Event())
    assert calls==[kind]


def test_category_read_uses_original_adapter_signature(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from catalog_ozon_options_worker import run
    import erp.integrations.ozon as ozon
    def read(category_id, type_id, language='ZH_HANS'):
        assert (category_id,type_id,language)==(10,20,'ZH_HANS')
        return [{'id':1,'name':'attribute'}]
    monkeypatch.setattr(ozon,'OzonSellerAdapter',lambda config,repository:Obj(category_attributes=read))
    client=Obj(request=lambda *args,**kwargs:[{'store_id':'store','platform':'ozon'}])
    job={'payload':{'store_id':'store','kind':'category_attributes','params':{'category_id':10,'type_id':20,'language':'ZH_HANS'}},'attempts':1}
    assert run(client,'/job',job,'.',Event())['items'][0]['id']==1


def test_real_category_adapter_uses_memory_cache_not_product_database(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from catalog_ozon_options_worker import run
    import erp.integrations.ozon as ozon
    monkeypatch.setattr(ozon,'ProductRepository',lambda: (_ for _ in ()).throw(AssertionError('No business database should be created')))
    monkeypatch.setattr(ozon.OzonSellerAdapter,'post',lambda self,path,payload:{'result':[{'id':1,'name':'color'}]})
    client=Obj(request=lambda *args,**kwargs:[{'store_id':'store','platform':'ozon'}])
    job={'payload':{'store_id':'store','kind':'category_attributes','params':{'category_id':10,'type_id':20}},'attempts':1}
    assert run(client,'/job',job,'.',Event())['items']==[{'id':1,'name':'color'}]

def test_category_cache_survives_and_isolates_store_and_category(tmp_path, monkeypatch):
    import erp.services.ozon_categories as cache
    monkeypatch.setattr(cache, 'get_settings', lambda: Obj(data_dir=tmp_path))
    params={'category_id':10,'type_id':20,'language':'ZH_HANS'}
    cache.save_options_cache('store','category_attributes',params,[{'id':1}])
    assert cache.load_options_cache('store','category_attributes',params)==[{'id':1}]
    assert cache.load_options_cache('other','category_attributes',params) is None
    assert cache.load_options_cache('store','category_attributes',{**params,'type_id':21}) is None
    assert cache.load_options_cache('store','warehouses',{}) is None
