from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from openpyxl import load_workbook

from erp.core.database import SessionLocal
from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps
from erp.core.security import vault
from erp.domains.pod import wb_export, desktop_wb
from erp.models import Base, Store, IntegrationConnection, CatalogProduct, CatalogVariant, Asset, CatalogProductAsset, Job, WbPublicationItem
from erp.services.jobs import JobContext
from test_pod_wb_export import _template
from test_personal_job_controls import team


def source(session, mode='excel'):
    template = wb_export.save_template(_template(), 'wb.xlsx', 'Пледы', 321)
    store = Store(platform='wb', name='isolated', offer_prefix='W', secrets_encrypted=vault.encrypt({'token': 'private-token'}))
    ai = IntegrationConnection(provider='ai_chat', name='AI', base_url='https://ai.invalid', secrets_encrypted=vault.encrypt({'api_key': 'private-key'}))
    product = CatalogProduct(sku='PRIVATE-1', source_type='pod', name='毛毯', package_length_mm=300, package_width_mm=200, package_height_mm=100, package_weight_g=500)
    session.add_all([store, ai, product]); session.flush()
    variant = CatalogVariant(product_id=product.id, sku='PRIVATE-1-01', size='125x150 cm', active=True)
    asset = Asset(name='blue.jpg', kind='rendering', oss_url='https://oss.example/blue.jpg', local_path='D:/not-available.jpg')
    session.add_all([variant, asset]); session.flush()
    session.add(CatalogProductAsset(product_id=product.id, variant_id=variant.id, asset_id=asset.id, role='main', is_current=True)); session.commit()
    job = wb_export.create_export(session, product_ids=[product.id], store_id=store.id, template_id=template['id'], brand='No brand', requirement='', price_cny=100, variant_selection='all', mode=mode)
    return job


def test_job_and_ledger_rollback_together():
    with SessionLocal() as db:
        job = source(db)
        identity = job.id
        db.rollback()
    with SessionLocal() as db:
        assert db.get(Job, identity) is None
        assert db.scalar(select(WbPublicationItem.id)) is None


@pytest.mark.parametrize('stage', ['pending_stock', 'pending_price'])
def test_store_filter_applies_before_pagination_and_total(client, stage):
    with SessionLocal() as db:
        original = source(db); db.flush()
        row = db.scalar(select(WbPublicationItem).where(WbPublicationItem.job_id == original.id))
        row.status = stage
        other = Store(platform='wb', name='Other shop', offer_prefix='OTHER')
        db.add(other); db.flush()
        db.add(WbPublicationItem(job_id=original.id, store_id=other.id,
            product_id=row.product_id, variant_id=row.variant_id, template_id=row.template_id,
            offer_id='OTHER-1', mode='excel', status=stage))
        db.commit(); store_id = row.store_id
    result = client.get('/api/v1/pod/wb/publications', params={
        'stage': stage, 'store_id': store_id, 'page_size': 1}).json()
    assert result['total'] == 1
    assert len(result['items']) == 1
    assert result['items'][0]['store_id'] == store_id
    empty = client.get('/api/v1/pod/wb/publications', params={
        'stage': stage, 'store_id': 'x' * 32}).json()
    assert empty['total'] == 0 and empty['items'] == []


def test_reconcile_mixed_batch_only_checks_unresolved_and_preserves_completed():
    with SessionLocal() as db:
        original = source(db)
        db.flush()
        row = db.scalar(select(WbPublicationItem).where(WbPublicationItem.job_id == original.id))
        row.status = 'submitted'
        original.status = 'completed'
        other_variant = CatalogVariant(product_id=row.product_id, sku='ALREADY-COMPLETE', active=True)
        db.add(other_variant); db.flush()
        completed = WbPublicationItem(job_id=original.id, store_id=row.store_id,
            product_id=row.product_id, variant_id=other_variant.id, template_id=row.template_id,
            offer_id='ALREADY-COMPLETE', mode='excel', status='completed', external_product_id='555')
        db.add(completed); db.commit()
        job = wb_export.enqueue_excel_reconcile(db, original.id)
        assert wb_export.enqueue_excel_reconcile(db, original.id).id == job.id
        snapshot = desktop_wb.reconcile_snapshot(db, job)
        assert snapshot['offer_ids'] == [row.offer_id]
        desktop_wb.accept_reconcile_result(db, job, {'found': {row.offer_id: 123}})
        assert row.status == 'pending_stock'
        assert row.external_product_id == '123'
        assert completed.status == 'completed'
        assert completed.external_product_id == '555'


def test_verified_existing_wb_price_completes_matching_ledger():
    with SessionLocal() as db:
        source_job = source(db)
        db.flush()
        ledger = db.scalar(select(WbPublicationItem).where(WbPublicationItem.job_id == source_job.id))
        ledger.status = 'pending_price'
        ledger.external_product_id = '12345'
        db.commit()
        job = wb_export.enqueue_price(db, [ledger.id], None, 30)
        snapshot = desktop_wb.price_snapshot(db, job)
        desktop_wb.accept_price_result(db, job, {
            'verified': True, 'verified_existing': True, 'upload_id': 0,
            'rows': snapshot['rows'],
        })
        assert ledger.status == 'completed'
        assert ledger.confirmed_at is not None


def test_failed_wb_price_can_create_read_only_reconciliation(client):
    with SessionLocal() as db:
        source_job = source(db)
        db.flush()
        ledger = db.scalar(select(WbPublicationItem).where(WbPublicationItem.job_id == source_job.id))
        ledger.status = 'price_failed'
        ledger.external_product_id = '12345'
        ledger_id = ledger.id
        db.commit()
    reconcile = client.post('/api/v1/pod/wb/publications/price/reconcile', json={
        'ledger_ids': [ledger_id], 'price': 124, 'discount_percent': 30})
    assert reconcile.status_code == 202
    with SessionLocal() as db:
        job = db.get(Job, reconcile.json()['job']['id'])
        assert desktop_wb.price_snapshot(db, job)['verify_only'] is True
        assert db.get(WbPublicationItem, ledger_id).status == 'price_processing'


def test_snapshot_private_database_runs_original_excel(tmp_path, monkeypatch):
    with SessionLocal() as db:
        job = source(db); db.commit()
        value = desktop_wb.snapshot(db, job)
        content = Path(wb_export._template(value['template']['id'])['path']).read_bytes()
        assert value['assets'][0]['local_path'] == ''
        assert value['store']['secrets_encrypted'] == ''
        assert value['job']['config_snapshot_json'] == ''
        assert 'path' not in value['template']
        assert value['config']['store']['secrets']['token'] == 'private-token'
        product_id = value['products'][0]['id']
        value['copy_references'][product_id] = [{'title': 'old title', 'description': 'old description'}]
        # Remove the source product: global SessionLocal must not be consulted
        # by the copy thread, even though it points at this different database.
        db.query(WbPublicationItem).delete(); db.query(CatalogProductAsset).delete()
        db.query(CatalogVariant).delete(); db.query(CatalogProduct).delete(); db.commit()
    templates, exports = tmp_path/'templates', tmp_path/'exports'
    templates.mkdir(); exports.mkdir()
    monkeypatch.setattr(wb_export, '_roots', lambda: (templates, exports))
    calls = []
    def generate(config, product, template, payload):
        calls.append((product.id, payload['_copy_references']))
        return {'title': 'Новый плед', 'description': 'Новое описание', 'characteristics': []}
    monkeypatch.setattr(wb_export, '_generate_copy', generate)
    monkeypatch.setattr(wb_export, '_wb_characteristics', lambda *_: [])
    engine = create_engine('sqlite:///' + (tmp_path/'task.db').as_posix())
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            local_job = desktop_wb.install_snapshot(db, value, content); db.commit()
            wb_export.wb_export_job(JobContext(db, local_job))
            path = wb_export.export_file(db, local_job.id)
            workbook = load_workbook(path, read_only=True, data_only=True)
            assert workbook.active['B5'].value == 'W-PRIVATE-1-01'
            assert workbook.active['F5'].value == 'Новый плед'
            workbook.close()
            assert db.scalar(select(WbPublicationItem)).status == 'exported_pending_confirmation'
            assert calls == [(product_id, value['copy_references'][product_id])]
            with pytest.raises(ConflictError, match='空的任务数据库'):
                desktop_wb.install_snapshot(db, value, content)
    finally:
        engine.dispose()


def test_private_api_publish_runs_original_card_flow(tmp_path, monkeypatch):
    with SessionLocal() as db:
        job = source(db, mode='api'); db.commit()
        value = desktop_wb.snapshot(db, job)
        content = Path(wb_export._template(value['template']['id'])['path']).read_bytes()
    templates, exports = tmp_path/'templates', tmp_path/'exports'
    templates.mkdir(); exports.mkdir()
    monkeypatch.setattr(wb_export, '_roots', lambda: (templates, exports))
    monkeypatch.setattr(wb_export, '_generate_copy', lambda *_: {'title':'Новый плед','description':'Новое описание','characteristics':[]})
    monkeypatch.setattr(wb_export, '_wb_characteristics', lambda *_: [])
    writes=[]
    class FakeContent:
        def __init__(self, config): self.created=False
        def generate_barcodes(self, count): return ['1234567890123']*count
        def find_cards_exact(self, vendors):
            return {vendor:{'nmID':123456} for vendor in vendors} if self.created else {}
        def find_card(self, vendor, aliases): return None
        def create_cards(self, groups):
            writes.append(('create',groups)); self.created=True
        def save_media(self, nm_id, images): writes.append(('media',nm_id,images))
        def close(self): pass
    monkeypatch.setattr(wb_export, '_WbContentClient', FakeContent)
    engine = create_engine('sqlite:///' + (tmp_path/'api-task.db').as_posix())
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            local_job = desktop_wb.install_snapshot(db, value, content); db.commit()
            wb_export.wb_export_job(JobContext(db, local_job))
            ledger = db.scalar(select(WbPublicationItem))
            assert ledger.status == 'pending_stock' and ledger.external_product_id == '123456'
            assert [row[0] for row in writes] == ['create','media']
            assert (exports/local_job.id/'api-submit-intent.json').exists()
    finally:
        engine.dispose()


def test_snapshot_rejects_incomplete_ledger():
    with SessionLocal() as db:
        job = source(db); db.commit()
        db.query(WbPublicationItem).delete(); db.flush()
        with pytest.raises(ConflictError, match='台账不一致'):
            desktop_wb.snapshot(db, job)


def test_personal_wb_lease_and_result_isolation(team, monkeypatch):
    import base64
    from erp.core.config import get_settings
    clients, worker = team
    owner = clients['owner']
    owner.post('/api/v1/execution-devices/local/register',json={'id':worker,'name':'WB PC','wb_version':1})
    with monkeypatch.context() as temporary:
        temporary.setattr(get_settings(),'multiuser_enabled',False)
        with SessionLocal() as db:
            job=source(db);job.target_worker_id=worker
            job.created_by=''
            identity=job.id;db.commit()
    user=owner.get('/api/v1/auth/session').json()['user']['id']
    with SessionLocal() as db: db.get(Job,identity).created_by=user;db.commit()
    base='/api/v1/execution-devices/local/'+worker
    assert owner.post(base+'/claim').json()['id']==identity
    endpoint=base+'/jobs/'+identity
    headers={'X-Worker-Attempt':'1'}
    for name in ('other','admin','viewer'):
        assert clients[name].post(endpoint+'/wb-snapshot',headers=headers).status_code==403
    assert owner.post(endpoint+'/wb-snapshot',headers={'X-Worker-Attempt':'2'}).status_code==409
    response=owner.post(endpoint+'/wb-snapshot',headers=headers)
    assert response.status_code==200,response.text
    assert response.headers['cache-control']=='no-store'
    assert response.json()['config']['store']['secrets']['token']=='private-token'
    bad=owner.post(endpoint+'/wb-file/wb_copy',headers=headers,json={'content':'!bad!'})
    assert bad.status_code==422
    import json
    snap=response.json()
    copies={p['id']:{'title':'Новый плед','description':'Новое описание'} for p in snap['products']}
    body={'content':base64.b64encode(json.dumps(copies).encode()).decode()}
    staged=owner.post(endpoint+'/wb-file/wb_copy',headers=headers,json=body)
    assert staged.status_code==200
    assert owner.post(endpoint+'/wb-file/wb_copy',headers=headers,json=body).json()==staged.json()
    assert owner.post(endpoint+'/complete',headers=headers,json={'result':{'ledgers':[]}}).status_code==409
    assert owner.get('/api/v1/jobs/'+identity).json()['status']=='running'
    ledger_results=[{'id':r['id'],'status':'exported_pending_confirmation'} for r in snap['ledgers']]
    package={'payload':json.loads(snap['job']['payload_json']),'groups':[{'variants':[{'sizes':[{'variant_id':r['variant_id']} for r in snap['ledgers']]}]}]}
    receipts=[staged.json()]
    for role,content in [('wb_excel',_template()),('wb_package',json.dumps(package).encode())]:
        response=owner.post(endpoint+'/wb-file/'+role,headers=headers,json={'content':base64.b64encode(content).decode()})
        assert response.status_code==200,response.text
        receipts.append(response.json())
    result={'ledgers':ledger_results,'files':receipts,'folder':'C:/isolated'}
    finished=owner.post(endpoint+'/complete',headers=headers,json={'result':result})
    assert finished.status_code==200,finished.text
    assert finished.json()['status']=='completed'
    assert owner.post(endpoint+'/complete',headers=headers,json={'result':result}).status_code==200
    result['folder']='changed'
    assert owner.post(endpoint+'/complete',headers=headers,json={'result':result}).status_code==409
    assert owner.get('/api/v1/pod/wb/exports/'+identity+'/download').status_code==200


def test_wb_paid_calls_resume_known_and_stop_unknown(tmp_path, monkeypatch):
    import httpx
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    from wb_worker import paid_transport
    calls=[]
    def known(*args,**kwargs):
        calls.append(1); return httpx.Response(200,json={'choices':[]})
    for _ in range(2):
        request=paid_transport(known,tmp_path/'known',lambda:None)
        assert request('text',None,'https://ai.invalid',json={'prompt':'same'}).status_code==200
    assert len(calls)==1
    def unknown(*args,**kwargs): calls.append(1); raise TimeoutError()
    for _ in range(2):
        request=paid_transport(unknown,tmp_path/'unknown',lambda:None)
        with pytest.raises(Exception) as error: request('text',None,'https://ai.invalid',json={})
        assert error.value.code=='ai_write_outcome_unknown'
    assert len(calls)==2
    def unavailable(*args,**kwargs):calls.append(1);return httpx.Response(502)
    for _ in range(2):
        request=paid_transport(unavailable,tmp_path/'unavailable',lambda:None)
        with pytest.raises(Exception) as error:request('text',None,'https://ai.invalid',json={})
        assert error.value.code=='ai_write_outcome_unknown'
    assert len(calls)==3


def test_fresh_process_private_worker_and_resume(tmp_path):
    import base64, json, os, subprocess, sys
    root=Path(__file__).resolve().parents[1]
    with SessionLocal() as db:
        job=source(db); db.commit(); snap=desktop_wb.snapshot(db,job)
        snap['template_content']=base64.b64encode(Path(wb_export._template(snap['template']['id'])['path']).read_bytes()).decode()
        job_value={'id':job.id,'type':job.type,'attempts':1}
    work=tmp_path/'wb-tasks'/job_value['id']; work.mkdir(parents=True)
    (work/'snapshot.json').write_text(json.dumps(snap),encoding='utf-8')
    (work/'input.json').write_text(json.dumps({'root':str(tmp_path),'endpoint':'/isolated','job':job_value}),encoding='utf-8')
    # A new interpreter catches process-global DB/config leakage that an
    # in-process test using the application's preloaded modules cannot catch.
    script=r'''
import os,sys,json,base64,hashlib,time
from pathlib import Path
root,work=map(Path,sys.argv[1:])
sys.path[:0]=[str(root/'client/desktop'),str(root/'backend')]
os.environ.update(ERP_DATA_DIR=str(work),ERP_DATABASE_URL='sqlite:///'+(work/'task.db').as_posix(),ERP_MULTIUSER_ENABLED='false',ERP_RUNTIME_MODE='worker')
import erp_desktop
class Cloud:
 calls=[]
 def request(self,method,path,body=None,**kwargs):
  Cloud.calls.append(path)
  if path.endswith('/wb-snapshot'):return json.loads((work/'snapshot.json').read_text()),{}
  if '/wb-file/' in path:
   content=base64.b64decode(body['content']);return {'role':path.rsplit('/',1)[1],'sha256':hashlib.sha256(content).hexdigest(),'size_bytes':len(content)},{}
  return {},{}
erp_desktop.Client=Cloud
from erp.domains.pod import wb_export
wb_export._wb_characteristics=lambda *args:[]
calls=[]
def generate(*args):
 calls.append(1);time.sleep(1.4);return {'title':'Test title','description':'Test description','characteristics':[]}
wb_export._generate_copy=generate
import wb_worker
first=wb_worker.execute(work/'input.json')
second=wb_worker.execute(work/'input.json')
assert first==second and len(calls)==1
assert sum(path.endswith('/heartbeat') for path in Cloud.calls)<=12
assert {r['role'] for r in first['files']}=={'wb_copy','wb_package','wb_excel'}
assert all(r['status']=='exported_pending_confirmation' for r in first['ledgers'])
print('isolated child and resume passed')
'''
    result=subprocess.run([sys.executable,'-c',script,str(root),str(work)],capture_output=True,text=True,timeout=60,env={**os.environ,'PYTHONUTF8':'1'})
    assert result.returncode==0,result.stdout+result.stderr


def test_web_submission_requires_capable_personal_computer(team, monkeypatch):
    import json
    from erp.core.config import get_settings
    clients, worker=team;owner=clients['owner']
    with monkeypatch.context() as temporary:
        temporary.setattr(get_settings(),'multiuser_enabled',False)
        with SessionLocal() as db:
            job=source(db);payload=json.loads(job.payload_json);db.rollback()
    fields=('product_ids','store_id','template_id','brand','requirement','price_cny','variant_selection','mode')
    body={k:payload[k] for k in fields}
    assert owner.post('/api/v1/pod/wb/exports',json=body).status_code==409
    owner.post('/api/v1/execution-devices/local/register',json={'id':worker,'name':'WB PC','wb_version':1})
    queued=owner.post('/api/v1/pod/wb/exports',json=body)
    assert queued.status_code==202,queued.text
    assert queued.json()['job']['target_worker_id']==worker
    assert clients['viewer'].post('/api/v1/pod/wb/exports',json=body).status_code==403
    assert owner.post('/api/v1/pod/wb/exports',json=body).status_code==400


def test_api_publish_requires_capable_pc_and_verifies_cloud_result(team, monkeypatch):
    import base64, json
    from erp.core.config import get_settings
    clients, worker = team; owner = clients['owner']
    with monkeypatch.context() as temporary:
        temporary.setattr(get_settings(), 'multiuser_enabled', False)
        with SessionLocal() as db:
            sample = source(db); payload = json.loads(sample.payload_json); db.rollback()
    fields = ('product_ids','store_id','template_id','brand','requirement','price_cny','variant_selection')
    body = {key: payload[key] for key in fields}; body['mode'] = 'api'
    owner.post('/api/v1/execution-devices/local/register', json={'id':worker,'name':'WB PC','wb_version':1})
    assert owner.post('/api/v1/pod/wb/exports', json=body).status_code == 409
    owner.post('/api/v1/execution-devices/local/register', json={'id':worker,'name':'WB PC','wb_version':1,'wb_api_version':1})
    queued = owner.post('/api/v1/pod/wb/exports', json=body)
    assert queued.status_code == 202, queued.text
    identity = queued.json()['job']['id']
    endpoint = f'/api/v1/execution-devices/local/{worker}/jobs/{identity}'
    assert owner.post(f'/api/v1/execution-devices/local/{worker}/claim').json()['id'] == identity
    headers = {'X-Worker-Attempt':'1'}
    snap_response = owner.post(endpoint+'/wb-snapshot', headers=headers)
    assert snap_response.status_code == 200, snap_response.text
    snap = snap_response.json(); assert snap['job']['type'] == 'pod_wb_api_publish'
    copies = {p['id']:{'title':'Новый плед','description':'Новое описание'} for p in snap['products']}
    package = {'payload':json.loads(snap['job']['payload_json']),
               'groups':[{'variants':[{'sizes':[{'variant_id':r['variant_id']} for r in snap['ledgers']]}]}]}
    receipts = []
    for role, value in [('wb_copy', copies), ('wb_package', package)]:
        response = owner.post(endpoint+'/wb-file/'+role, headers=headers,
                              json={'content':base64.b64encode(json.dumps(value).encode()).decode()})
        assert response.status_code == 200, response.text
        receipts.append(response.json())
    result = {'ledgers':[{'id':r['id'],'status':'pending_stock','external_product_id':'123456'} for r in snap['ledgers']],
              'files':receipts}
    invalid = {**result, 'ledgers':[{**row,'external_product_id':''} for row in result['ledgers']]}
    assert owner.post(endpoint+'/complete', headers=headers, json={'result':invalid}).status_code == 409
    completed = owner.post(endpoint+'/complete', headers=headers, json={'result':result})
    assert completed.status_code == 200, completed.text
    with SessionLocal() as db:
        rows = db.scalars(select(WbPublicationItem).where(WbPublicationItem.job_id == identity)).all()
        assert rows and all(row.status == 'pending_stock' and row.external_product_id == '123456' for row in rows)
