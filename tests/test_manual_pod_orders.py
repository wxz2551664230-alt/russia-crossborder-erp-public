import pytest
from pydantic import ValidationError

from erp.core.database import SessionLocal
from erp.domains.orders import api
from erp.domains.orders.legacy_engine import OrderError
from erp.models import Store
from test_pod_orders import orders, posting


def create(orders, **overrides):
    args = dict(order_number='N1', store_id='A', store_name='Shop', platform='ozon',
                items=[{'sku': 'POD-000001-01', 'quantity': 3}])
    args.update(overrides)
    return orders.create_manual_order(**args)


@pytest.mark.parametrize('platform', ['ozon', 'wb'])
def test_manual_order_ready_for_purchase_without_recipient(orders, platform):
    row = create(orders, platform=platform)
    assert row['workflow_status'] == 'pending_purchase'
    assert row['submission_status'] == 'not_submitted'
    assert row['recipient_ready'] is False
    assert row['items'][0]['quantity'] == 3
    assert row['items'][0]['matched_sku'] == 'POD-000001-01'
    assert row['order_status'] == 'manual'
    assert orders.preview_batch([row['order_key']], 'smart')['groups'][0]['quantity'] == 3


def test_duplicate_save_idempotent_and_changed_quantity_rejected(orders):
    first = create(orders)
    assert create(orders)['order_key'] == first['order_key']
    with pytest.raises(OrderError):
        create(orders, items=[{'sku': 'POD-000001-01', 'quantity': 4}])
    assert orders.get_order(first['order_key'])['items'][0]['quantity'] == 3
    assert len(orders.list_orders()['orders']) == 1


def test_manual_cannot_overwrite_synced_order(orders):
    orders.import_postings([posting()], store_id='A')
    with pytest.raises(OrderError, match='已存在'):
        create(orders)
    assert orders.list_orders()['orders'][0]['items'][0]['quantity'] == 2


def test_same_number_other_store_allowed(orders):
    assert create(orders)['order_key'] != create(orders, store_id='B')['order_key']


@pytest.mark.parametrize('items', [
    [{'sku': 'missing', 'quantity': 1}],
    [{'sku': 'POD-000001', 'quantity': 1}],
    [{'sku': 'SHOP-POD-000001-01', 'quantity': 1}],
    [{'sku': 'POD-000001-01', 'quantity': 1}]*2,
])
def test_invalid_selection_writes_nothing(orders, items):
    with pytest.raises(OrderError):
        create(orders, items=items)
    assert orders.list_orders()['orders'] == []


@pytest.mark.parametrize('quantity', [0, -1, 1.5, True, 10001])
def test_invalid_quantity(quantity):
    with pytest.raises(ValidationError):
        api.ManualOrder(store_id='A', order_number='N1', items=[{'sku':'S', 'quantity':quantity}])


def test_route_without_platform_credentials(client, orders, monkeypatch):
    monkeypatch.setattr(api, 'manager', lambda _: orders)
    with SessionLocal() as db:
        store = Store(platform='wb', name='WB', enabled=True)
        db.add(store); db.commit(); identity = store.id
    body = {'store_id':identity, 'order_number':'WB-100', 'items':[{'sku':'POD-000001-01','quantity':2}]}
    response = client.post('/api/v1/orders/pod/manual', json=body)
    assert response.status_code == 200, response.text
    assert response.json()['workflow_status'] == 'pending_purchase'
    body['store_id'] = 'missing'
    assert client.post('/api/v1/orders/pod/manual', json=body).status_code == 400


def test_gateway_and_role_boundary():
    from deploy.pilot_gateway import allowed
    from erp.core.access import permitted
    assert allowed('POST', '/api/v1/orders/pod/manual')
    assert not permitted({'role':'viewer'}, 'POST', '/orders/pod/manual')
    assert permitted({'role':'operator'}, 'POST', '/orders/pod/manual')
