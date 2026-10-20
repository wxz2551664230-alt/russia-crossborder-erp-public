import os
from pathlib import Path
import subprocess
import sys


def test_fresh_process_original_purchase_and_completion_recovery(tmp_path):
    root=Path(__file__).resolve().parents[1]
    script=r'''
import os,sys,json
from pathlib import Path
from types import SimpleNamespace
from contextlib import closing
root,work=map(Path,sys.argv[1:])
sys.path[:0]=[str(root/'client/desktop'),str(root/'backend')]
os.environ.update(ERP_DATA_DIR=str(work),ERP_DATABASE_URL='sqlite:///'+(work/'unused.db').as_posix(),ERP_MULTIUSER_ENABLED='false',ERP_RUNTIME_MODE='worker')
import erp_desktop,order_worker
from cryptography.fernet import Fernet
from erp.core.security import vault
from erp.domains.orders import desktop_purchase_submit as bridge,service
from erp.domains.orders.legacy_engine import OrderManager
cloud=work/'isolated-cloud';orders=OrderManager(cloud,None,Fernet(Fernet.generate_key()));orders.initialize()
with closing(orders._connect()) as db:
 db.execute("INSERT INTO batch_order_groups(id,batch_id,sku,product_code,stock_item_id,quantity,address_enc,created_at,updated_at,submission_status,out_order_id,supplier,fulfillment_product_code,remote_product_id,logistics_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",('group','batch','SKU','CODE',1,1,orders.cipher.encrypt(b'{"firstname":"PRIVATE_RECIPIENT"}'),'2026-09-24','now','queued','external','s2b','ready',123,109));db.commit()
job=SimpleNamespace(id='c'*32,type='pod_purchase_submit',payload_json=json.dumps({'target_type':'group','target_id':'group'}),config_snapshot_json=vault.encrypt({'s2b_store_id':1}))
bridge.manager=lambda _:orders
calls=[]
class Supplier:
 async def order_by_out_order_id(self,key):return None
 async def create_order(self,payload):
  assert payload['address']['firstname']=='PRIVATE_RECIPIENT'
  calls.append(True);return {'order_id':'remote','id':'remote','subtotal':'10'}
 def close(self):pass
service.supplier_client=lambda *a:Supplier()
class Cloud:
 def request(self,method,path,body,**kwargs):
  assert kwargs['extra_headers']['X-Worker-Attempt'] in {'1','2'}
  if path.endswith('/heartbeat'):return {},{}
  if path.endswith('/purchase-submit-snapshot'):return bridge.snapshot(None,job),{}
  if path.endswith('/purchase-operation'):return bridge.operation(None,job,body['method'],body['args'],body['kwargs']),{}
  if path.endswith('/purchase-write-intent'):return bridge.write_intent(None,job),{}
  if path.endswith('/purchase-submit-result'):return bridge.completed_state(None,job),{}
  raise AssertionError(path)
erp_desktop.Client=Cloud
source=work/'input.json';value={'job':{'id':job.id,'type':job.type,'attempts':1},'endpoint':'/private','root':str(work)}
source.write_text(json.dumps(value));first=order_worker.execute(source)
value['job']['attempts']=2;source.write_text(json.dumps(value))
assert order_worker.execute(source)==first=={'submitted':1}
assert calls==[True] and orders.get_batch_group('group')['submission_status']=='submitted'
assert not (work/'unused.db').exists()
for p in (work/'purchase-writes').iterdir():assert b'PRIVATE_RECIPIENT' not in p.read_bytes()
with closing(orders._connect()) as db:
 assert all('PRIVATE_RECIPIENT' not in r[0] for r in db.execute('SELECT summary_json FROM erp_operation_receipts'))
'''
    result=subprocess.run([sys.executable,'-c',script,str(root),str(tmp_path)],capture_output=True,text=True,timeout=60,env={**os.environ,'PYTHONUTF8':'1'})
    assert result.returncode==0,result.stdout+result.stderr
