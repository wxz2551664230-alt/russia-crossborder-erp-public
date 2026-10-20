import json
from pathlib import Path
from erp.domains.orders import service


def test_batches_bound_wire_size_and_preserve_order(monkeypatch):
    import pytest
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    from order_worker import order_chunks
    rows=[{'id':i,'address':'中'*10000} for i in range(40)]
    chunks=order_chunks(rows)
    assert len(chunks)>1
    assert [row for chunk in chunks for row in chunk]==rows
    assert all(len(json.dumps(chunk).encode())<=1024*1024 for chunk in chunks)
    assert order_chunks([])==[[]]
    with pytest.raises(ValueError):order_chunks([{'address':'x'*(1024*1024)}])


def test_order_worker_resume_uses_encrypted_snapshot_and_returns_only_summary(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    import erp_desktop
    import order_worker
    for key in ('ERP_DATA_DIR','ERP_DATABASE_URL','ERP_MULTIUSER_ENABLED','ERP_RUNTIME_MODE'):
        monkeypatch.setenv(key,'isolated')
    calls=[];fetches=[]
    class Cloud:
        def request(self,method,path,body,**kwargs):
            calls.append((path,body))
            if path.endswith('/order-snapshot'):return {'config':{'platform':'wb'},'payload':{'days':3},'offers':['offer']},{}
            if path.endswith('/order-batch'):return {'imported':len(body['postings']),'updated':0,'total':len(body['postings'])},{}
            if path.endswith('/order-images'):return {'offers':1,'with_platform_image':1,'order_items_updated':1},{}
            return {},{}
    monkeypatch.setattr(erp_desktop,'Client',Cloud)
    def fetch(*args):fetches.append('orders');return [{'id':i,'recipient':'private recipient'} for i in range(201)]
    def images(*args):fetches.append('images');return {'offer':'https://image.test/one.jpg'}
    monkeypatch.setattr(service,'fetch_platform_orders',fetch)
    monkeypatch.setattr(service,'fetch_platform_images',images)
    source=tmp_path/'input.json'
    source.write_text(json.dumps({'job':{'id':'a'*32,'type':'pod_order_sync','attempts':1},'endpoint':'/private','root':str(tmp_path)}))
    first=order_worker.execute(source);second=order_worker.execute(source)
    assert first==second and first['sync']['total']==201
    assert fetches==['orders','images']
    batches=[body for path,body in calls if path.endswith('/order-batch')]
    assert [(len(x['postings']),x['final']) for x in batches]==[(200,False),(1,True)]*2
    assert b'private recipient' not in (tmp_path/'postings.dpapi').read_bytes()
    assert 'private recipient' not in json.dumps(first)


def test_order_worker_fresh_process_resume(tmp_path):
    import os
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    script = r'''
import json, os, sys
from pathlib import Path
root, work = map(Path, sys.argv[1:])
sys.path[:0] = [str(root/'client/desktop'), str(root/'backend')]
os.environ.update(ERP_DATA_DIR=str(work), ERP_DATABASE_URL='sqlite:///'+(work/'unused.db').as_posix(),
                  ERP_MULTIUSER_ENABLED='false', ERP_RUNTIME_MODE='worker')
import erp_desktop, order_worker
from erp.domains.orders import service
calls = []
class Cloud:
    def request(self, method, path, body, **kwargs):
        assert kwargs['extra_headers']['X-Worker-Attempt'] in {'1', '2'}
        if path.endswith('/order-snapshot'):
            return {'config':{}, 'payload':{}, 'offers':['offer']}, {}
        if path.endswith('/order-batch'):
            calls.append(body)
            return {'imported':len(body['postings']), 'updated':0, 'total':len(body['postings'])}, {}
        if path.endswith('/order-images'):
            return {'offers':1, 'with_platform_image':1, 'order_items_updated':1}, {}
        return {}, {}
erp_desktop.Client = Cloud
service.fetch_platform_orders = lambda *a: [{'id':i, 'recipient':'PRIVATE_ADDRESS'} for i in range(201)]
service.fetch_platform_images = lambda *a: {'offer':'https://example.test/a.jpg'}
source = work/'input.json'
value = {'job':{'id':'a'*32, 'type':'pod_order_sync', 'attempts':1}, 'endpoint':'/private', 'root':str(work)}
source.write_text(json.dumps(value))
first = order_worker.execute(source)
def forbidden(*a):
    raise AssertionError('Resume must reuse same-task encrypted snapshot')
service.fetch_platform_orders = service.fetch_platform_images = forbidden
value['job']['attempts'] = 2
source.write_text(json.dumps(value))
assert order_worker.execute(source) == first
assert first['sync']['total'] == 201
assert [(len(c['postings']),c['final']) for c in calls] == [(200,False),(1,True)]*2
assert not (work/'unused.db').exists()
for path in work.iterdir():
    if path.is_file(): assert b'PRIVATE_ADDRESS' not in path.read_bytes()
'''
    result = subprocess.run([sys.executable, '-c', script, str(root), str(tmp_path)],
                            capture_output=True, text=True, timeout=60,
                            env={**os.environ, 'PYTHONUTF8':'1'})
    assert result.returncode == 0, result.stdout + result.stderr


def test_purchase_worker_fresh_process_resume(tmp_path):
    import os, subprocess, sys
    root=Path(__file__).resolve().parents[1]
    script=r'''
import sys,os,json
from pathlib import Path
root,work=map(Path,sys.argv[1:])
sys.path[:0]=[str(root/'client/desktop'),str(root/'backend')]
os.environ.update(ERP_DATA_DIR=str(work),ERP_DATABASE_URL='sqlite:///'+(work/'unused.db').as_posix(),ERP_MULTIUSER_ENABLED='false',ERP_RUNTIME_MODE='worker')
import erp_desktop,order_worker
from erp.domains.orders import service
calls=[]
class Cloud:
 def request(self,method,path,body,**kwargs):
  assert kwargs['extra_headers']['X-Worker-Attempt'] in {'1','2'}
  if path.endswith('/purchase-snapshot'):return {'config':{},'targets':[{'supplier':'s2b','out_order_id':'external'}]},{}
  if path.endswith('/purchase-statuses'):
   calls.append(body)
   return {'checked':1,'updated':1,'paid':1,'tracking':1,'missing':0,'failed':0},{}
  assert path.endswith('/heartbeat')
  return {},{}
erp_desktop.Client=Cloud
service.fetch_purchase_status=lambda *a:[{'supplier':'s2b','out_order_id':'external','failed':False,'remote':{'address':'PRIVATE_ADDRESS'}}]
source=work/'input.json';value={'job':{'id':'b'*32,'type':'pod_purchase_sync','attempts':1},'endpoint':'/private','root':str(work)}
source.write_text(json.dumps(value));first=order_worker.execute(source)
def forbidden(*a):raise AssertionError('must reuse encrypted same-task result')
service.fetch_purchase_status=forbidden
value['job']['attempts']=2;source.write_text(json.dumps(value))
assert order_worker.execute(source)==first
assert len(calls)==2 and calls[0]==calls[1]
assert (work/'purchase-statuses.dpapi').is_file()
assert not (work/'unused.db').exists()
for p in work.iterdir():
 if p.is_file():assert b'PRIVATE_ADDRESS' not in p.read_bytes()
'''
    result=subprocess.run([sys.executable,'-c',script,str(root),str(tmp_path)],capture_output=True,text=True,timeout=60,env={**os.environ,'PYTHONUTF8':'1'})
    assert result.returncode==0,result.stdout+result.stderr


def test_order_scope_skips_image_api(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    import erp_desktop, order_worker
    for key in ('ERP_DATA_DIR','ERP_DATABASE_URL','ERP_MULTIUSER_ENABLED','ERP_RUNTIME_MODE'):
        monkeypatch.setenv(key,'isolated')
    class Cloud:
        def request(self,method,path,body,**kwargs):
            assert kwargs['extra_headers']['X-Order-Scope-Version']=='1'
            if path.endswith('/order-snapshot'):return {'config':{},'payload':{'sync_images':False},'offers':['offer']},{}
            if path.endswith('/order-batch'):return {'imported':0,'updated':0,'total':0},{}
            if path.endswith('/order-images'):
                assert body=={'images':{}}
                return {'with_platform_image':0},{}
            return {},{}
    monkeypatch.setattr(erp_desktop,'Client',Cloud)
    monkeypatch.setattr(service,'fetch_platform_orders',lambda *a:[])
    def forbidden(*a):raise AssertionError('Images disabled but platform called')
    monkeypatch.setattr(service,'fetch_platform_images',forbidden)
    source=tmp_path/'input.json'
    source.write_text(json.dumps({'job':{'id':'a'*32,'type':'pod_order_sync','attempts':1},'endpoint':'/private','root':str(tmp_path)}))
    assert order_worker.execute(source)['images']['with_platform_image']==0
