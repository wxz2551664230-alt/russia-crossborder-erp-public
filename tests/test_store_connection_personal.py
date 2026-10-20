"""Ozon Seller check stays on the initiating PC in team mode."""
from test_personal_job_controls import team
from deploy.pilot_gateway import allowed
from erp.core.database import SessionLocal
from erp.models import Job


def test_personal_store_test_is_fenced_and_no_server_call(team, monkeypatch):
    clients, _ = team
    admin, other = clients['admin'], clients['other']
    worker = 'pc-' + 'b' * 32
    registered = admin.post('/api/v1/execution-devices/local/register', json={
        'id': worker, 'name': 'admin PC', 'store_test_version': 1,
    })
    assert registered.status_code == 200, registered.text
    assert 'store_connection_test' in registered.json()['capabilities']
    store = admin.post('/api/v1/stores', json={
        'platform': 'ozon', 'name': 'isolated-ozon-store',
        'external_account_id': '123456', 'secrets': {'api_key': 'test-private-key'},
    })
    assert store.status_code == 201, store.text
    store_id = store.json()['id']
    assert allowed('POST', f'/api/v1/stores/{store_id}/test')
    assert allowed('GET', f'/api/v1/execution-devices/local/{worker}/jobs/{"a"*32}/store-test-snapshot')
    def forbidden(*_args, **_kwargs):
        raise AssertionError('server must not contact Ozon')
    monkeypatch.setattr('erp.integrations.ozon.OzonSellerAdapter.test_connection', forbidden)
    response = admin.post(f'/api/v1/stores/{store_id}/test')
    assert response.status_code == 200, response.text
    job_id = response.json()['job_id']
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.type == 'store_connection_test'
        assert job.priority == 10
        assert job.target_worker_id == worker
        assert 'test-private-key' not in job.payload_json
        assert 'test-private-key' not in job.config_snapshot_json
    claim = admin.post(f'/api/v1/execution-devices/local/{worker}/claim')
    assert claim.status_code == 200 and claim.json()['id'] == job_id
    path = f'/api/v1/execution-devices/local/{worker}/jobs/{job_id}'
    headers = {'X-Worker-Attempt': '1'}
    assert other.get(path + '/store-test-snapshot', headers=headers).status_code == 403
    snapshot = admin.get(path + '/store-test-snapshot', headers=headers)
    assert snapshot.status_code == 200 and snapshot.json()['store_id'] == store_id
    assert snapshot.json()['secrets']['api_key'] == 'test-private-key'
    assert snapshot.headers['cache-control'] == 'no-store'
    bad = admin.post(path + '/complete', headers=headers,
                     json={'result': {'ok': True, 'store_id': store_id, 'category_roots': -1}})
    assert bad.status_code == 409
    done = admin.post(path + '/complete', headers=headers,
                      json={'result': {'ok': True, 'store_id': store_id, 'category_roots': 3}})
    assert done.status_code == 200, done.text
    assert done.json()['status'] == 'completed'
    assert done.json()['steps'][0]['details']['category_roots'] == 3


def test_pc_store_runner_uses_snapshot_and_returns_count(monkeypatch, tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from store_test_worker import run
    from erp.integrations.ozon import OzonSellerAdapter
    from erp.modules.ozon_follow.legacy.storage import ProductRepository
    class Client:
        def request(self, method, path, extra_headers=None):
            assert (method, path) == ('GET', '/local/jobs/job/store-test-snapshot')
            assert extra_headers == {'X-Worker-Attempt': '1'}
            return ({'store_id': 'store', 'platform': 'ozon', 'secrets': {'api_key': 'key'}}, {})
    class Live:
        def is_set(self):
            return False
    monkeypatch.setattr(OzonSellerAdapter, 'test_connection', lambda self: {'ok': True, 'category_roots': 4})
    monkeypatch.setattr(ProductRepository, '__init__',
                        lambda self, *args, **kwargs: (_ for _ in ()).throw(AssertionError('local product DB created')))
    assert run(Client(), '/local/jobs/job', {'attempts': 1, 'payload': {'store_id': 'store'}},
               tmp_path, Live()) == {'ok': True, 'store_id': 'store', 'category_roots': 4}
