import copy
from types import SimpleNamespace as Obj
import pytest
from erp.core.database import SessionLocal
from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps, loads
from erp.core.task_identity import task_identity
from erp.models import Job, WorkerNode
from erp.services import codex_handoffs as bridge

@pytest.fixture
def handoff(monkeypatch):
    token=task_identity.set({'id':'owner','worker_id':'pc'})
    with SessionLocal() as session:
        device=WorkerNode(id='pc',name='测试电脑',owner_user_id='owner')
        job=Job(type='catalog_ozon_prepare',created_by='owner',target_worker_id='pc',status='paused',pause_requested=True,idempotency_key='codex-test',payload_json=dumps({'publication_id':'pub','batch_id':'a'*32,'generation_provider':'codex'}))
        session.add_all([device,job]);session.commit()
        pub=Obj(stores=[Obj(store_id='store')])
        snap={'revision':'revision','resume':{'assets':{'personal_review':{'image_action':{'id':'action'}}}}}
        monkeypatch.setattr(bridge.ozon,'get_publication',lambda *a:pub)
        monkeypatch.setattr(bridge.ozon,'snapshot',lambda *a:copy.deepcopy(snap))
        yield session,job,device,snap
    task_identity.reset(token)

def test_resume_same_task_once_and_reject_changed_receipt(handoff):
    session,job,device,snap=handoff
    receipt={'store':{'revision':'revision','output_sha256':'a'*64,'image_action_id':'action'}}
    assert bridge.resume(session,job.id,'pc',receipt)['status']=='queued'
    assert bridge.resume(session,job.id,'pc',receipt)['status']=='queued'
    changed=copy.deepcopy(receipt);changed['store']['output_sha256']='b'*64
    with pytest.raises(ConflictError):bridge.resume(session,job.id,'pc',changed)
    assert loads(job.payload_json,{})['codex_outputs']==receipt

@pytest.mark.parametrize('mutation',['owner','device','provider','revision','action','hash','missing_store'])
def test_wrong_owner_device_and_stale_output_never_resume(handoff,mutation):
    session,job,device,snap=handoff
    receipt={'store':{'revision':'revision','output_sha256':'a'*64,'image_action_id':'action'}}
    if mutation=='owner':job.created_by='someone'
    elif mutation=='device':device.owner_user_id='someone'
    elif mutation=='provider':job.payload_json=dumps({'generation_provider':'model'})
    elif mutation=='revision':receipt['store']['revision']='old'
    elif mutation=='action':receipt['store']['image_action_id']='old'
    elif mutation=='hash':receipt['store']['output_sha256']='!'*64
    else:receipt={}
    with pytest.raises(ConflictError):bridge.resume(session,job.id,'pc',receipt)
    assert job.status=='paused'

def test_new_chat_bundle_is_scoped_and_has_no_secrets(handoff):
    session,job,device,snap=handoff
    result=bridge.bundle(session,'a'*32)
    assert result['jobs'][0]['job_id']==job.id
    assert 'export --batch' in result['prompt'] and 'imagegen' in result['prompt']
    assert 'config' not in dumps(result)
    token=task_identity.set({'id':'another','worker_id':'pc'})
    try:
        with pytest.raises(ConflictError):bridge.bundle(session,'a'*32)
    finally:task_identity.reset(token)

def test_codex_missing_output_blocks_original_model(tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'client/desktop'))
    from catalog_ozon_worker import prepare,CatalogWorkerError
    with pytest.raises(CatalogWorkerError) as failure:
        prepare({'schema':1,'requirements':{'generation_provider':'codex'}},tmp_path,lambda:None,lambda data,**kw:data)
    assert failure.value.code=='codex_output_missing'

def test_local_export_excludes_credentials_and_submit_binds_original_task(monkeypatch,tmp_path):
    import sys,json
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'client/desktop'))
    import codex_bridge as local
    from catalog_ozon_worker import codex_binding
    job='a'*32;store='b'*32
    snap={'job_id':job,'publication_id':'publication','product_id':'product','store_id':store,'store_name':'测试','offer_id':'O-SKU','revision':'revision',
          'facts':{'sku':'SKU','assets':[]},'requirements':{'generation_provider':'codex'},'image_policy':{'mode':'reuse'},'prior_copy_for_difference_check':[],
          'config':{'ai_api_key':'NEVER_EXPORT_THIS'},'resume':{'assets':{},'content':{},'attributes':[]}}
    context={'kind':'ozon_listing_generation','target_worker_id':'pc','status':'paused','stores':[snap]}
    monkeypatch.setattr(local,'profile_dir',lambda:tmp_path)
    monkeypatch.setattr(local,'local_context',lambda *args:context)
    monkeypatch.setattr(local,'definitions',lambda *args:[])
    monkeypatch.setattr(local,'reference_files',lambda *args:[])
    monkeypatch.setattr(local,'protect',lambda data,**kwargs:data)
    cache=tmp_path/'codex-dictionary-cache';cache.mkdir();(cache/'ozon-dictionaries.db').write_bytes(b'isolated')
    monkeypatch.setattr(local,'runtime',lambda:cache)
    args=Obj(batch=None,job=job,store=store,output=str(tmp_path/'export'))
    local.export(None,args)
    exported=tmp_path/'export'/job/store
    assert 'NEVER_EXPORT_THIS' not in (exported/'handoff.json').read_text(encoding='utf-8')
    args.file=str(exported/'output-template.json')
    output=json.loads(Path(args.file).read_text(encoding='utf-8'))
    output['content'].update(title_ru='Новый товар',description_ru='Новое описание',tags_ru=['#декор'],tags_shortfall_reason='隔离测试只验证格式，不生成30个真实业务标签')
    Path(args.file).write_text(json.dumps(output),encoding='utf-8')
    local.submit(None,args)
    record=json.loads((tmp_path/'catalog-ozon-preparations'/job/store/'codex-content.dpapi').read_bytes())
    assert record['binding']==codex_binding(snap) and record['producer']=='codex-desktop'
    output['revision']='old';Path(args.file).write_text(json.dumps(output),encoding='utf-8')
    with pytest.raises(ValueError):local.submit(None,args)


def test_create_codex_task_is_paused_before_first_claim(client,monkeypatch):
    from test_catalog_publications import _catalog_product,_store
    from erp.api.routes import publications
    from erp.services import jobs
    from erp.schemas import PublicationCreate
    product=_catalog_product(client);store=_store(client,'隔离店铺','801','Q-')
    original=jobs.get_settings()
    settings=original.model_copy(update={'multiuser_enabled':True})
    monkeypatch.setattr(jobs,'get_settings',lambda:settings)
    monkeypatch.setattr(publications,'get_settings',lambda:settings)
    token=task_identity.set({'id':'owner','worker_id':'pc'})
    try:
        with SessionLocal() as session:
            session.add(WorkerNode(id='pc',owner_user_id='owner',capabilities_json=dumps(['codex_handoff_v1','catalog_ozon_prepare'])));session.commit()
            result=publications.create_publications(PublicationCreate(product_ids=[product],store_ids=[store],generation_provider='codex',image_policy='reuse'),session)
            job=session.get(Job,result['items'][0]['job']['id'])
            assert job.status=='paused' and job.pause_requested
            assert job.target_worker_id=='pc' and job.created_by=='owner'
            assert result['items'][0]['publication']['stores'][0]['status']=='waiting_codex'
            assert bridge.bundle(session,result['batch_id'])['jobs'][0]['job_id']==job.id
    finally:task_identity.reset(token)
