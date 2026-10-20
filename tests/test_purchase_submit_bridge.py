from contextlib import closing
from types import SimpleNamespace
import json
import pytest

from erp.core.errors import ConflictError
from erp.core.security import vault
from erp.domains.orders import desktop_purchase_submit as bridge
from test_pod_orders import orders, posting
from test_purchase_cloud_status import seed


def task(identity='a'*32,target='group',kind='group'):
    return SimpleNamespace(id=identity,type='pod_purchase_submit',
        payload_json=json.dumps({'target_type':kind,'target_id':target}),
        config_snapshot_json=vault.encrypt({'s2b_store_id':1}))


def setup(orders,monkeypatch):
    seed(orders);orders.update_batch_group('group',submission_status='queued')
    monkeypatch.setattr(bridge,'manager',lambda _:orders)


def test_reserve_recovery_blocks_other_task_and_replays_writes(orders,monkeypatch):
    setup(orders,monkeypatch);job=task()
    first=bridge.snapshot(None,job)
    # Simulate the main job database losing its last commit.
    job.config_snapshot_json=vault.encrypt({'s2b_store_id':1})
    assert bridge.snapshot(None,job)==first
    with pytest.raises(ConflictError):bridge.snapshot(None,task('b'*32))
    before=bridge.operation(None,job,'update_batch_group',['group'],{'submission_status':'checking'})
    orders.update_batch_group('group',submission_status='submitted',hicustom_order_id='remote')
    assert bridge.operation(None,job,'update_batch_group',['group'],{'submission_status':'checking'})==before
    assert orders.get_batch_group('group')['submission_status']=='submitted'
    assert bridge.completed_state(None,job)=={'submitted':1}
    assert bridge.completed_state(None,job)=={'submitted':1}
    with closing(orders._connect()) as db:
        assert db.execute('select count(*) from erp_purchase_reservations').fetchone()[0]==0


def test_order_and_group_for_same_source_cannot_execute_concurrently(orders,monkeypatch):
    setup(orders,monkeypatch);orders.import_postings([posting()],store_id='A')
    key=orders.list_orders()['orders'][0]['order_key']
    with closing(orders._connect()) as db:
        db.execute('INSERT INTO batch_order_sources(group_id,posting_number,quantity) VALUES (?,?,?)',('group',key,2));db.commit()
    bridge.snapshot(None,task())
    with pytest.raises(ConflictError):bridge.snapshot(None,task('b'*32,key,'order'))


def test_unknown_target_and_premature_completion_are_blocked(orders,monkeypatch):
    seed(orders);monkeypatch.setattr(bridge,'manager',lambda _:orders)
    with pytest.raises(ConflictError,match='未知'):bridge.snapshot(None,task())
    orders.update_batch_group('group',submission_status='queued');job=task();bridge.snapshot(None,job)
    with pytest.raises(ConflictError):bridge.completed_state(None,job)
    with pytest.raises(ConflictError):bridge.operation(None,job,'update_batch_group',['other'],{})
    with closing(orders._connect()) as db:
        assert db.execute('select count(*) from erp_purchase_reservations').fetchone()[0]==1
