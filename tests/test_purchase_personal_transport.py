import asyncio
import pytest

from erp.domains.orders import service
from erp.domains.orders.legacy_engine import HicustomOrderQueue


def test_targets_only_include_unique_submitted_or_unknown_identifiers():
    class Orders:
        def list_batch_groups(self, limit):
            assert limit == 500
            return [
                {'supplier':'s2b','out_order_id':'A','submission_status':state,'address':'private'}
                for state in ['submitted','unknown','queued','failed']
            ] + [{'supplier':'hicustom','out_order_id':'B','submission_status':'unknown'}]
    assert service.purchase_read_targets(Orders(),'s2b') == [{'supplier':'s2b','out_order_id':'A'}]
    assert len(service.purchase_read_targets(Orders())) == 2


def test_purchase_fetch_reuses_clients_closes_and_sanitizes_errors(monkeypatch):
    calls=[]; closed=[]
    class Supplier:
        async def order_by_out_order_id(self, external):
            calls.append(external)
            if external == 'bad':raise RuntimeError('SECRET recipient address')
            return {'order_id':external,'tracking_number':'tracking'}
        def close(self):closed.append(True)
    def client(session,provider,settings):
        assert session is None and provider == 's2b' and settings == {'s2b_token':'private'}
        return Supplier()
    monkeypatch.setattr(service,'supplier_client',client)
    monkeypatch.setattr(service,'manager',lambda *a:pytest.fail('Personal fetch must not open shared orders'))
    targets=[{'supplier':'s2b','out_order_id':i} for i in ['ok','bad']]
    result=service.fetch_purchase_status({'s2b_token':'private'},targets)
    assert calls == ['ok','bad'] and closed == [True]
    assert result[0]['remote']['order_id'] == 'ok'
    assert result[1] == {**targets[1],'remote':None,'failed':True}
    assert 'SECRET' not in str(result)


def test_lost_lease_stops_purchase_reads_and_closes_client(monkeypatch):
    closed=[]; checks=[]; calls=[]
    class Supplier:
        async def order_by_out_order_id(self,key):calls.append(key);return {}
        def close(self):closed.append(True)
    monkeypatch.setattr(service,'supplier_client',lambda *a:Supplier())
    def check():
        checks.append(True)
        if len(checks)==2:raise RuntimeError('lease lost')
    with pytest.raises(RuntimeError,match='lease lost'):
        service.fetch_purchase_status({},[{'supplier':'s2b','out_order_id':x} for x in ['A','B']],check)
    assert calls==['A'] and closed==[True]


@pytest.mark.parametrize('merged',[False,True])
def test_remote_facts_preserve_original_cost_and_tracking_rules(merged):
    writes=[]
    class Orders:
        def list_batch_groups(self,limit):
            return [{'id':'group','supplier':'hicustom','out_order_id':'external',
                     'purchase_id':'purchase','submission_status':'unknown'}]
        def list_purchase_groups(self,key):return [1,2] if merged else [1]
        def record_batch_costs(self,key,remote):
            writes.append(('single',key,remote));return {'paid_at':'today','tracking_number':'track'}
        def record_merged_batch_costs(self,key,remote):
            writes.append(('merged',key,remote));return [{'paid_at':'today','tracking_number':'track'}]
        def update_batch_group(self,key,**changes):writes.append(('group',key,changes))
        def update_purchase_groups(self,key,**changes):writes.append(('purchase',key,changes))
    async def forbidden(*a):pytest.fail('Cloud application must never call suppliers')
    async def facts(provider,external):
        assert (provider,external)==('hicustom','external')
        return {'order_id':'remote-id','paid_at':'today','tracking_number':'track'}
    queue=HicustomOrderQueue(Orders(),lambda:{},forbidden)
    result=asyncio.run(queue.sync_remote_orders(remote_reader=facts))
    assert result=={'checked':1,'updated':1,'paid':1,'tracking':1,'missing':0,'failed':0,'errors':[]}
    assert writes[0][0]==('merged' if merged else 'single')
    assert writes[1][2]['submission_status']=='submitted'
    assert writes[1][2]['hicustom_order_id']=='remote-id'
