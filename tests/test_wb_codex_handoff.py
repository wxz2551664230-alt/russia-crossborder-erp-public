import copy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.core.errors import AppError,ConflictError
from erp.core.jsonutil import dumps,loads
from erp.core.security import vault
from erp.core.task_identity import task_identity
from erp.models import CatalogProduct,CatalogVariant,Store,WorkerNode,WbPublicationItem,Asset,CatalogProductAsset
from erp.domains.pod import wb_export
from erp.services import codex_handoffs,wb_codex
from test_pod_wb_export import _template


@pytest.fixture
def wb_task(monkeypatch):
    monkeypatch.setattr(get_settings(),'multiuser_enabled',True)
    token=task_identity.set({'id':'owner','worker_id':'pc','role':'operator'})
    template=wb_export.save_template(_template(),'wb.xlsx','Пледы',321)
    with SessionLocal() as db:
        store=Store(platform='wb',name='WB 测试',offer_prefix='WB',secrets_encrypted=vault.encrypt({'token':'private-test-key'}))
        product=CatalogProduct(sku='HC-000001',source_type='pod',name='毛毯',supplier='HICUSTOM',package_length_mm=300,package_width_mm=200,package_height_mm=100,package_weight_g=500,source_snapshot_json=dumps({'material':'polyester','token':'must-not-export'}))
        device=WorkerNode(id='pc',name='电脑',owner_user_id='owner',capabilities_json=dumps(['codex_wb_v1','pod_wb_excel_export','pod_wb_api_publish']))
        db.add_all([store,product,device]);db.flush()
        variant=CatalogVariant(product_id=product.id,sku='HC-000001-01',active=True,size='125x150 cm');db.add(variant);db.flush()
        asset=Asset(kind='image',name='photo.jpg',oss_url='https://oss.example/photo.jpg');db.add(asset);db.flush();db.add(CatalogProductAsset(product_id=product.id,variant_id=variant.id,asset_id=asset.id,role='main',is_current=True));db.commit()
        def create(mode='excel'):
            job=wb_export.create_export(db,product_ids=[product.id],store_id=store.id,template_id=template['id'],brand='Нет бренда',requirement='',price_cny=99,variant_selection='selected',variant_ids=[variant.id],mode=mode,generation_provider='codex')
            db.commit();return job
        yield db,product,device,create
    task_identity.reset(token)


def outputs(snap):return {p['product_id']:{'title':'Мягкий плед для уютного дома','description':'Плед для отдыха дома. Учитывайте реальные размеры при выборе.','characteristics':[]} for p in snap['products']}


def receipt(snap):
    copies=outputs(snap)
    return {snap['store_id']:{'revision':snap['revision'],'output_sha256':wb_codex.digest(copies),'copies':copies}}


def test_created_paused_without_ai_and_context_has_no_secrets(wb_task):
    db,p,device,create=wb_task;job=create()
    assert job.status=='paused' and job.pause_requested and job.created_by=='owner' and job.target_worker_id=='pc'
    assert vault.decrypt(job.config_snapshot_json)['ai']=={}
    assert db.query(WbPublicationItem).filter_by(job_id=job.id).count()==1
    ctx=codex_handoffs.context(db,job.id,'pc')
    text=json.dumps(ctx,ensure_ascii=False)
    assert ctx['kind']=='wb_listing_copy' and 'private-test-key' not in text and 'must-not-export' not in text and 'config_snapshot_json' not in text
    bundle=codex_handoffs.bundle(db,job.id)
    assert 'WB' in bundle['prompt'] and 'export --batch' in bundle['prompt']
    assert 'tags_ru' not in bundle['prompt']
    snap=ctx['snapshot'];r=receipt(snap)
    assert codex_handoffs.resume(db,job.id,'pc',r)['status']=='queued'
    assert codex_handoffs.resume(db,job.id,'pc',r)['status']=='queued'


def test_api_waits_for_reviewed_exact_revision(wb_task):
    db,p,device,create=wb_task;job=create('api');snap=wb_codex.snapshot(db,job);r=receipt(snap)
    result=codex_handoffs.resume(db,job.id,'pc',r)
    assert result['requires_publish_approval'] and job.status=='paused'
    item=r[snap['store_id']]
    with pytest.raises(ConflictError):wb_codex.approve(db,job.id,snap['revision'],'0'*64)
    assert job.status=='paused'
    assert wb_codex.approve(db,job.id,snap['revision'],item['output_sha256'])['status']=='queued'
    assert loads(job.payload_json,{})['codex_publish_approved'] is True


@pytest.mark.parametrize('mutation',['device','owner','revision','copies','hash','facts'])
def test_wrong_binding_cannot_resume(wb_task,mutation):
    db,p,device,create=wb_task;job=create();snap=wb_codex.snapshot(db,job);r=receipt(snap)
    if mutation=='device':device.owner_user_id='other'
    if mutation=='owner':job.created_by='other'
    if mutation=='revision':r[snap['store_id']]['revision']='old'
    if mutation=='copies':r[snap['store_id']]['copies']={}
    if mutation=='hash':r[snap['store_id']]['output_sha256']='0'*64
    if mutation=='facts':p.name='商品已变';db.flush()
    with pytest.raises(ConflictError):codex_handoffs.resume(db,job.id,'pc',r)
    assert job.status=='paused'


def test_old_device_cannot_create_codex_job(wb_task):
    db,p,device,create=wb_task;device.capabilities_json=dumps(['pod_wb_excel_export']);db.commit()
    with pytest.raises(ConflictError):create()


def test_codex_never_falls_back_to_paid_ai(monkeypatch):
    monkeypatch.setattr(wb_export,'ai_post',lambda *a,**k:pytest.fail('paid model called'))
    p=SimpleNamespace(id='product')
    with pytest.raises(AppError) as failure:wb_export._generate_copy({},p,{}, {'generation_provider':'codex'})
    assert failure.value.code=='codex_output_missing'
    template={'schema':{'characteristics':[]}}
    value={'title':'Плед для отдыха','description':'Мягкий плед для дома','characteristics':[]}
    assert wb_export._generate_copy({},p,template,{'generation_provider':'codex','_codex_outputs_by_product':{'product':value}})==value


@pytest.mark.parametrize('bad',['long_title','non_russian','unknown_attribute','too_many','duplicate_copy'])
def test_wb_limits_and_fresh_copy(bad):
    template={'schema':{'characteristics':[{'id':1,'max_count':2}]}}
    value={'title':'Плед для отдыха','description':'Мягкий плед для дома','characteristics':[]};references=[]
    if bad=='long_title':value['title']='Я'*61
    if bad=='non_russian':value['title']='English only'
    if bad=='unknown_attribute':value['characteristics']=[{'id':99,'value':'v'}]
    if bad=='too_many':value['characteristics']=[{'id':1,'value':['a','b','c']}]
    if bad=='duplicate_copy':references=[copy.deepcopy(value)]
    with pytest.raises(ConflictError):wb_codex.validate_copy(value,template,references)


def test_local_bridge_export_submit_resume_same_pc(wb_task,monkeypatch,tmp_path):
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'client/desktop'))
    import codex_bridge as local,wb_codex_bridge as wb_local
    db,p,device,create=wb_task;job=create();ctx=codex_handoffs.context(db,job.id,'pc');snap=ctx['snapshot']
    monkeypatch.setattr(wb_local,'profile_dir',lambda:tmp_path)
    monkeypatch.setattr(wb_local,'protect',lambda data,**kw:data)
    monkeypatch.setattr(wb_local,'helpers',lambda:wb_codex)
    monkeypatch.setattr(local,'identity',lambda:'pc')
    item=wb_local.export(ctx,tmp_path/'export');path=Path(item['output'])
    value=json.loads(path.read_text(encoding='utf-8'));value['copies']=outputs(snap);path.write_text(json.dumps(value),encoding='utf-8')
    args=SimpleNamespace(job=job.id,store=snap['store_id'],file=str(path))
    wb_local.submit(None,args,ctx)
    class Client:
        def request(self,method,path,body):return codex_handoffs.resume(db,job.id,body['worker_id'],body['receipts']),{}
    wb_local.resume(Client(),args,ctx)
    assert job.status=='queued'


def test_codex_excel_runs_original_export_without_ai(wb_task,monkeypatch):
    from erp.services.jobs import JobContext
    db,p,device,create=wb_task;job=create();snap=wb_codex.snapshot(db,job);r=receipt(snap)
    codex_handoffs.resume(db,job.id,'pc',r)
    # This is a test-private database acting as the initiating PC, never the shared server.
    monkeypatch.setattr(get_settings(),'multiuser_enabled',False)
    monkeypatch.setattr(wb_export,'_wb_characteristics',lambda *a:[])
    monkeypatch.setattr(wb_export,'ai_post',lambda *a,**kw:pytest.fail('paid AI called'))
    payload=loads(job.payload_json,{});payload['_codex_outputs_by_product']=r[snap['store_id']]['copies'];job.payload_json=dumps(payload);db.commit()
    wb_export.wb_export_job(JobContext(db,job))
    path=wb_export.export_file(db,job.id)
    assert path.is_file() and path.stat().st_size>1000
    copy_path=path.parent/'copy.json'
    assert json.loads(copy_path.read_text(encoding='utf-8'))[p.id]['title']==outputs(snap)[p.id]['title']


def test_api_guard_before_any_platform_request(wb_task,monkeypatch):
    from erp.services.jobs import JobContext
    db,p,device,create=wb_task;job=create('api')
    monkeypatch.setattr(wb_export,'_wb_characteristics',lambda *a:pytest.fail('platform request before approval'))
    with pytest.raises(AppError) as failure:wb_export.wb_export_job(JobContext(db,job))
    assert failure.value.code=='codex_output_missing'
