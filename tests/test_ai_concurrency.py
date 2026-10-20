import asyncio
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from erp.core import ai_concurrency as ai
from erp.core.database import SessionLocal
from erp.models import CatalogProduct


@pytest.fixture
def gate(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, '_root', lambda: tmp_path)
    monkeypatch.setattr(ai, 'limits', lambda: {'text': 2, 'image': 1})
    return tmp_path


def test_shared_thread_limits_and_release_after_error(gate):
    active=0; peak=0; guard=threading.Lock()
    def send():
        nonlocal active,peak
        with guard: active+=1;peak=max(peak,active)
        time.sleep(.08)
        with guard: active-=1
        return SimpleNamespace(status_code=200)
    with ThreadPoolExecutor(max_workers=7) as pool:
        list(pool.map(lambda _:ai.post('text',send),range(7)))
    assert peak==2
    with pytest.raises(ValueError):
        with ai.slot('image'): raise ValueError()
    with ai.slot('image'): pass


def test_lock_is_shared_across_processes_and_released(gate):
    path=gate/'text-0.lock'
    child="from pathlib import Path; from erp.core.ai_concurrency import _try_lock; import sys; h=_try_lock(Path(sys.argv[1])); print(h is not None); h.close() if h else None"
    handle=ai._try_lock(path)
    try:
        run=subprocess.run([sys.executable,'-c',child,str(path)],capture_output=True,text=True,check=True)
        assert run.stdout.strip()=='False'
    finally:handle.close()
    run=subprocess.run([sys.executable,'-c',child,str(path)],capture_output=True,text=True,check=True)
    assert run.stdout.strip()=='True'


def test_async_cancel_and_separate_image_pool(gate):
    class Response:
        status=200
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class Client:
        def post(self,*args,**kwargs):return Response()
    async def run():
        with ai.slot('image'):
            task=asyncio.create_task(wait_image())
            await asyncio.sleep(.05)
            assert not task.done()
            async with ai.async_post('text',Client(),'url'):pass
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
        async with ai.async_post('image',Client(),'url'):pass
    async def wait_image():
        async with ai.async_post('image',Client(),'url'):pass
    asyncio.run(run())


def test_429_cools_only_affected_pool_without_replay(gate):
    calls=[]
    response=SimpleNamespace(status_code=429,headers={'Retry-After':'20'})
    assert ai.post('text',lambda:calls.append(1) or response) is response
    assert len(calls)==1
    assert ai._try_slot('text',2) is None
    with ai.slot('image'):pass


def test_settings_validation_and_readback(client):
    assert client.get('/api/v1/settings').json()['ai_concurrency']=={'text':5,'image':3}
    for value in ({'text':0,'image':3},{'text':5,'image':99},{'text':True,'image':2}):
        assert client.put('/api/v1/settings/ai_concurrency',json={'value':value}).status_code==422
    assert client.put('/api/v1/settings/ai_concurrency',json={'value':{'text':4,'image':2}}).status_code==200
    assert ai.limits()=={'text':4,'image':2}


def test_wb_parallel_copy_checkpoints_and_resume(gate,monkeypatch,tmp_path):
    from erp.domains.pod import wb_export as wb
    with SessionLocal() as session:
        products=[CatalogProduct(sku=f'copy-{n}',name=f'copy-{n}',source_type='pod') for n in range(4)]
        session.add_all(products);session.commit()
    active=0;peak=0;calls=[];guard=threading.Lock()
    def generate(config,product,template,payload):
        nonlocal active,peak
        with guard:active+=1;peak=max(peak,active);calls.append(product.id)
        time.sleep(.07)
        with guard:active-=1
        return {'title':product.name,'description':'text'}
    monkeypatch.setattr(wb,'_generate_copy',generate)
    context=SimpleNamespace(session=session,job=SimpleNamespace(id='wb-test',type='pod_wb_api_publish',attempts=1),refresh=lambda:None,set_progress=lambda *args:None)
    copies={};checkpoint=tmp_path/'copy.json'
    wb._prepare_copies_parallel(context,products,{}, {},{},copies,checkpoint)
    assert peak==2 and len(copies)==4 and len(json.loads(checkpoint.read_text()))==4
    wb._prepare_copies_parallel(context,products,{}, {},{},copies,checkpoint)
    assert len(calls)==4


def test_wb_copy_retries_per_product_and_continues_after_failure(gate,monkeypatch,tmp_path):
    from erp.domains.pod import wb_export as wb
    with SessionLocal() as session:
        products=[CatalogProduct(sku=f'partial-{n}',name=f'partial-{n}',source_type='pod') for n in range(3)]
        session.add_all(products);session.commit()
    calls=[]
    def generate(config,product,template,payload):
        calls.append(product.id)
        if product.id==products[0].id:raise ValueError('upstream failed')
        return {'title':'ok','description':'ok'}
    monkeypatch.setattr(wb,'_generate_copy',generate)
    copies={};failures={};checkpoint=tmp_path/'partial.json';failure_checkpoint=tmp_path/'failures.json'
    context=SimpleNamespace(session=session,job=SimpleNamespace(id='wb-partial',type='pod_wb_api_publish',attempts=1),refresh=lambda:None,set_progress=lambda *args:None)
    wb._prepare_copies_parallel(context,products,{}, {},{},copies,checkpoint,failures,failure_checkpoint)
    assert calls.count(products[0].id)==3
    assert set(copies)=={products[1].id,products[2].id}
    assert failures[products[0].id]['attempts']==3
    assert set(json.loads(checkpoint.read_text()))==set(copies)
    assert set(json.loads(failure_checkpoint.read_text()))=={products[0].id}
    wb._prepare_copies_parallel(context,products,{}, {},{},copies,checkpoint,failures,failure_checkpoint)
    assert len(calls)==5


def test_ai_prepare_queue_cannot_submit_to_ozon():
    from erp.services.publications import catalog_ozon_publication_job
    from erp.core.errors import ConflictError
    child=SimpleNamespace(store_id='store',external_task_id='')
    context=SimpleNamespace(payload={'publication_id':'test','submit_ready':True},job=SimpleNamespace(type='catalog_ozon_prepare'),session=SimpleNamespace(scalar=lambda query:SimpleNamespace(stores=[child])))
    with pytest.raises(ConflictError):catalog_ozon_publication_job(context)


def test_parallel_copy_never_reads_main_session_job_from_threads(gate,monkeypatch,tmp_path):
    from erp.domains.pod import wb_export as wb
    main_thread=threading.get_ident()
    class Context:
        def __init__(self, session):
            self.session = session
        @property
        def job(self):
            assert threading.get_ident()==main_thread, 'ORM job accessed from copy thread'
            return SimpleNamespace(id='thread-safe-job',type='pod_wb_excel_export',attempts=2)
        def refresh(self):
            assert threading.get_ident()==main_thread
        def set_progress(self,*args):
            assert threading.get_ident()==main_thread
    with SessionLocal() as session:
        products=[CatalogProduct(sku=f'thread-safe-{n}',name=f'product-{n}',source_type='pod') for n in range(8)]
        session.add_all(products);session.commit()
        def generate(*args):
            from erp.core.ai_usage import scope
            assert scope.get()['job_id']=='thread-safe-job'
            time.sleep(.02)
            return {'title':'ok','description':'ok'}
        monkeypatch.setattr(wb,'_generate_copy',generate)
        copies={}
        wb._prepare_copies_parallel(Context(session),products,{}, {},{},copies,tmp_path/'copy.json')
        assert len(copies)==8
