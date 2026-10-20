from contextlib import closing
from types import SimpleNamespace
import pytest
from erp.core.errors import ConflictError
from erp.core.security import vault
from erp.domains.orders import desktop_purchase as bridge
from test_pod_orders import orders, posting
from test_personal_job_controls import team


def seed(orders):
    with closing(orders._connect()) as db:
        db.execute("INSERT INTO batch_order_groups (id,batch_id,sku,product_code,stock_item_id,quantity,address_enc,created_at,updated_at,submission_status,out_order_id,supplier) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   ('group','batch','SKU','CODE',1,1,orders.cipher.encrypt(b'{}'),'now','now','unknown','external','s2b'))
        db.commit()


def task():
    return SimpleNamespace(id='a'*32,type='pod_purchase_sync',payload_json='{}',
        config_snapshot_json=vault.encrypt({'s2b_token':'private'}),
        steps=[SimpleNamespace(key='sync',status='pending',details_json='{}',progress=0)])


def test_original_cost_and_paid_updates_commit_together(orders,monkeypatch):
    seed(orders);orders.import_postings([posting()],store_id='A')
    key=orders.list_orders()['orders'][0]['order_key']
    with closing(orders._connect()) as db:
        db.execute('INSERT INTO batch_order_sources(group_id,posting_number,quantity) VALUES (?,?,?)',('group',key,2));db.commit()
    monkeypatch.setattr(bridge,'manager',lambda _:orders);job=task();bridge.snapshot(None,job)
    records=[{'supplier':'s2b','out_order_id':'external','failed':False,'remote':{
        'order_id':'supplier-id','subtotal':'20','shipping_amount':'3','grand_total':'23','payment_time':'today'}}]
    summary=bridge.accept(None,job,records)
    group=orders.get_batch_group('group')
    assert summary['updated']==1 and summary['paid']==1
    assert group['actual_grand_total']=='23.00'
    assert group['source_orders'][0]['total_cost']=='23.00'
    assert orders.get_order(key)['submission_status']=='submitted'


def test_apply_failure_rolls_back_cost_changes(orders,monkeypatch):
    seed(orders);monkeypatch.setattr(bridge,'manager',lambda _:orders)
    job=task();bridge.snapshot(None,job)
    records=[{'supplier':'s2b','out_order_id':'external','failed':False,'remote':{'order_id':'id','subtotal':'20'}}]
    original=type(orders).update_batch_group
    def fail(self,*args,**kwargs):
        if kwargs.get('submission_status')=='submitted':raise RuntimeError('simulated after cost update')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(type(orders),'update_batch_group',fail)
    with pytest.raises(RuntimeError,match='simulated'):bridge.accept(None,job,records)
    assert orders.get_batch_group('group')['actual_product_amount']=='0'
    assert orders.get_batch_group('group')['submission_status']=='unknown'


def test_status_snapshot_is_frozen_and_replay_preserves_later_changes(orders,monkeypatch):
    seed(orders);monkeypatch.setattr(bridge,'manager',lambda _:orders);job=task()
    snap=bridge.snapshot(None,job)
    assert snap['targets']==[{'supplier':'s2b','out_order_id':'external'}]
    assert '_status_targets' not in snap['config']
    rows=[{'supplier':'s2b','out_order_id':'external','failed':False,'remote':{'error_msg':'supplier rejected'}}]
    summary=bridge.accept(None,job,rows)
    assert summary['failed']==1 and 'errors' not in summary
    assert orders.get_batch_group('group')['submission_status']=='failed'
    orders.update_batch_group('group',submission_status='submitted',hicustom_order_id='newer')
    job.steps[0].status='pending';job.steps[0].details_json='{}'
    assert bridge.accept(None,job,rows)==summary
    assert orders.get_batch_group('group')['hicustom_order_id']=='newer'


def test_missing_foreign_duplicate_and_stale_purchase_results_rejected(orders,monkeypatch):
    seed(orders);monkeypatch.setattr(bridge,'manager',lambda _:orders);job=task()
    bridge.snapshot(None,job)
    row={'supplier':'s2b','out_order_id':'external','failed':False,'remote':None}
    for records in ([],[row,row],[{**row,'out_order_id':'foreign'}]):
        with pytest.raises(ConflictError):bridge.accept(None,job,records)
    orders.update_batch_group('group',tracking_number='newer')
    with pytest.raises(ConflictError):bridge.accept(None,job,[row])
    assert orders.get_batch_group('group')['tracking_number']=='newer'


def test_new_merged_group_after_snapshot_rejected(orders,monkeypatch):
    seed(orders)
    with closing(orders._connect()) as db:
        db.execute("UPDATE batch_order_groups SET supplier='hicustom',purchase_id='purchase'");db.commit()
    monkeypatch.setattr(bridge,'manager',lambda _:orders);job=task();bridge.snapshot(None,job)
    with closing(orders._connect()) as db:
        columns=[r[1] for r in db.execute('PRAGMA table_info(batch_order_groups)')]
        row=dict(db.execute('SELECT * FROM batch_order_groups').fetchone());row['id']='new-group'
        db.execute('INSERT INTO batch_order_groups('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',[row[k] for k in columns]);db.commit()
    with pytest.raises(ConflictError,match='范围'):
        bridge.accept(None,job,[{'supplier':'hicustom','out_order_id':'external','failed':False,'remote':None}])


def test_unchanged_purchase_detail_address_does_not_look_like_edit(orders,monkeypatch):
    seed(orders)
    with closing(orders._connect()) as db:
        db.execute("UPDATE batch_order_groups SET supplier='hicustom',purchase_id='purchase'");db.commit()
    monkeypatch.setattr(bridge,'manager',lambda _:orders);job=task();bridge.snapshot(None,job)
    result=bridge.accept(None,job,[{'supplier':'hicustom','out_order_id':'external','failed':False,'remote':None}])
    assert result['missing']==1 and result['failed']==0


def test_existing_detail_fingerprint_resumes_without_requery(orders,monkeypatch):
    import hashlib,json
    seed(orders)
    with closing(orders._connect()) as db:
        db.execute("UPDATE batch_order_groups SET supplier='hicustom',purchase_id='purchase'");db.commit()
    monkeypatch.setattr(bridge,'manager',lambda _:orders);job=task();bridge.snapshot(None,job)
    config=vault.decrypt(job.config_snapshot_json)
    config['_status_guards']['group']=hashlib.sha256(json.dumps(orders.get_batch_group('group'),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    job.config_snapshot_json=vault.encrypt(config)
    result=bridge.accept(None,job,[{'supplier':'hicustom','out_order_id':'external','failed':False,'remote':None}])
    assert result['missing']==1


def test_purchase_routes_device_ownership_lease_and_completion(team,orders,monkeypatch):
    monkeypatch.setattr(bridge,'manager',lambda _:orders)
    clients,worker=team;owner=clients['owner'];base=f'/api/v1/execution-devices/local/{worker}'
    assert owner.post('/api/v1/orders/pod/purchase/sync').status_code==409
    reg=owner.post('/api/v1/execution-devices/local/register',json={'id':worker,'name':'PC','purchase_read_version':1})
    assert 'pod_purchase_sync' in reg.json()['capabilities']
    queued=owner.post('/api/v1/orders/pod/purchase/sync')
    assert queued.status_code==200,queued.text
    identity=queued.json()['job_id'];assert owner.post(base+'/claim').json()['id']==identity
    endpoint=base+'/jobs/'+identity;headers={'X-Worker-Attempt':'1'}
    for role in ('admin','other','viewer'):
        assert clients[role].post(endpoint+'/purchase-snapshot',headers=headers).status_code==403
    assert owner.post(endpoint+'/purchase-snapshot',headers={'X-Worker-Attempt':'2'}).status_code==409
    snap=owner.post(endpoint+'/purchase-snapshot',headers=headers)
    assert snap.status_code==200 and snap.headers['cache-control']=='no-store'
    assert owner.post(endpoint+'/complete',headers=headers,json={'result':{}}).status_code==409
    result=owner.post(endpoint+'/purchase-statuses',headers=headers,json={'records':[]})
    assert result.status_code==200,result.text
    done=owner.post(endpoint+'/complete',headers=headers,json={'result':result.json()})
    assert done.status_code==200,done.text
    assert done.json()['status']=='completed'
    assert owner.post(endpoint+'/purchase-statuses',headers=headers,json={'records':[]}).status_code==409
