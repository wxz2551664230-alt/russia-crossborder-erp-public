from types import SimpleNamespace as Obj


def test_options_cache_does_not_call_platform_or_enqueue(monkeypatch, tmp_path):
    import erp.api.routes.stores as routes
    import erp.services.ozon_categories as cache
    monkeypatch.setattr(cache, 'get_settings', lambda: Obj(data_dir=tmp_path))
    monkeypatch.setattr(routes, '_get', lambda *args: Obj(id='store', platform='ozon'))
    monkeypatch.setattr(routes, 'enqueue_job', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('No task for cached attributes')))
    cache.save_options_cache('store','category_attributes',{'category_id':10,'type_id':20,'language':'ZH_HANS'},[{'id':1}])
    result=routes.personal_ozon_options('store','category_attributes',10,20,False,Obj())
    assert result=={'items':[{'id':1}],'cached':True}


def test_category_search_returns_bounded_cloud_options(monkeypatch):
    import erp.api.routes.stores as routes
    monkeypatch.setattr(routes,'_get',lambda *args:Obj(id='store',platform='ozon'))
    monkeypatch.setattr(routes,'load_category_snapshot',lambda *args:{'options':[{'display':f'Towel {i}'} for i in range(1000)],'synced_at':'now'})
    result=routes.category_options('store','ZH_HANS','towel',100,Obj())
    assert len(result['options'])==100 and result['total']==1000


def test_import_pending_polls_back_off_without_resubmission(monkeypatch):
    import erp.integrations.ozon as ozon
    calls=[];delays=[]
    adapter=Obj(import_info=lambda task:calls.append(task) or {'result':{'items':[{'status':'pending'}]}})
    monkeypatch.setattr(ozon.time,'sleep',delays.append)
    ozon.OzonSellerAdapter.wait_import(adapter,123,attempts=6,interval=3)
    assert calls==[123]*6
    assert delays==[3,6,12,24,30]
