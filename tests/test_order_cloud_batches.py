from types import SimpleNamespace
import pytest
from erp.core.errors import ConflictError
from erp.core.security import vault
from erp.domains.orders import desktop
from erp.domains.orders.legacy_engine import OrderError
from test_pod_orders import orders, posting


def task():
    return SimpleNamespace(id='a'*32,type='pod_order_sync',
        config_snapshot_json=vault.encrypt({'platform':'ozon','store_id':'A','store_name':'shop','wb_rate':0.0783}),
        steps=[SimpleNamespace(key=key,status='pending',details_json='{}',progress=0) for key in ('sync','images')])


def test_receipt_commits_with_orders_and_survives_main_db_retry(orders,monkeypatch):
    monkeypatch.setattr(desktop,'manager',lambda _:orders)
    job=task(); original=posting()
    first=desktop.accept_batch(None,job,0,[original],True)
    assert first=={'imported':1,'updated':0,'total':1}
    # Simulate loss of PostgreSQL progress after the SQLite transaction committed.
    job.steps[0].status='pending';job.steps[0].details_json='{}'
    assert desktop.accept_batch(None,job,0,[original],True)==first
    assert len(orders.list_orders()['orders'])==1
    changed={**original,'status':'cancelled'}
    with pytest.raises(ConflictError):desktop.accept_batch(None,job,0,[changed],True)
    job.steps[0].status='pending';job.steps[0].details_json='{}'
    with pytest.raises(OrderError):desktop.accept_batch(None,job,0,[changed],True)


def test_batch_order_images_and_completion_only_log_summaries(orders,monkeypatch):
    monkeypatch.setattr(desktop,'manager',lambda _:orders)
    job=task()
    with pytest.raises(ConflictError):desktop.accept_batch(None,job,1,[posting()],True)
    with pytest.raises(ConflictError):desktop.accept_images(None,job,{})
    row=posting();row['recipient']={'name':'private recipient'}
    desktop.accept_batch(None,job,0,[row],True)
    with pytest.raises(ConflictError):desktop.completion_summary(job)
    with pytest.raises(ConflictError):desktop.accept_images(None,job,{'wrong-store-offer':'https://image.test/a.jpg'})
    images={'SHOP-POD-000001-01':'https://image.test/a.jpg'}
    result=desktop.accept_images(None,job,images)
    assert desktop.accept_images(None,job,images)==result
    summary=desktop.completion_summary(job)
    assert summary['sync']['total']==1 and summary['images']['order_items_updated']==1
    assert 'private recipient' not in str(summary)
    assert all('private recipient' not in step.details_json for step in job.steps)
