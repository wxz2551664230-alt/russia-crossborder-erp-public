from test_personal_job_controls import team, submit
from erp.core.database import SessionLocal
from erp.models import Job
from erp.services import catalog_personal_publications as bridge


def test_catalog_transport_keeps_owner_and_attempt_fence(team, monkeypatch):
    clients, worker = team
    owner = clients['owner']
    identity = submit(owner)
    base = f'/api/v1/execution-devices/local/{worker}'
    assert owner.post(base + '/claim').json()['id'] == identity
    # Construct an isolated future adapted job; production registration still
    # does not advertise this capability, and normal creation remains blocked.
    with SessionLocal() as session:
        session.get(Job, identity).type = 'catalog_ozon_prepare'
        session.commit()
    monkeypatch.setattr(bridge, 'snapshot', lambda db, job, store: {'job_id': job.id, 'store_id': store})
    endpoint = base + '/jobs/' + identity
    from deploy import pilot_gateway
    original_allow = pilot_gateway.allowed
    # Exercise the future backend transport in isolation; keep its production
    # gateway closed until the complete installed client is available.
    monkeypatch.setattr(pilot_gateway, 'allowed', lambda method, path:
        True if path.startswith(endpoint + '/catalog-ozon-') else original_allow(method, path))
    headers = {'X-Worker-Attempt': '1'}
    for name in ('other', 'admin', 'viewer'):
        assert clients[name].get(endpoint + '/catalog-ozon-snapshot/store', headers=headers).status_code == 403
    assert owner.get(endpoint + '/catalog-ozon-snapshot/store', headers={'X-Worker-Attempt': '2'}).status_code == 409
    response = owner.get(endpoint + '/catalog-ozon-snapshot/store', headers=headers)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert response.json() == {'job_id': identity, 'store_id': 'store'}
    assert owner.post(endpoint + '/complete', headers=headers, json={'result': {}}).status_code == 409
    assert owner.post(endpoint + '/catalog-ozon-prepared/store', headers={'X-Worker-Attempt': '2'},
        json={'revision': 'a' * 64, 'result': {}}).status_code == 409


def test_catalog_capability_registration_and_original_pc_claim(team):
    clients, worker = team
    owner = clients['owner']
    base = f'/api/v1/execution-devices/local/{worker}'
    response = owner.post('/api/v1/execution-devices/local/register', json={
        'id': worker, 'name': 'original PC', 'catalog_ozon_version': 1})
    assert response.status_code == 200
    assert {'catalog_ozon_prepare', 'catalog_ozon_publication', 'catalog_ozon_followup'} <= set(response.json()['capabilities'])
    identity = submit(owner)
    with SessionLocal() as session:
        session.get(Job, identity).type = 'catalog_ozon_followup'
        session.commit()
    assert clients['other'].post(base + '/claim').status_code == 403
    assert owner.post(base + '/claim').json()['id'] == identity
    assert owner.post(base + '/jobs/' + identity + '/complete', headers={'X-Worker-Attempt': '1'}, json={'result': {'ok': True}}).status_code == 409


def test_copy_revision_route_is_bound_to_owner_and_original_pc(team, monkeypatch):
    clients, worker = team
    owner = clients['owner']
    identity = submit(owner)
    with SessionLocal() as session:
        session.get(Job, identity).type = 'catalog_ozon_prepare'
        session.commit()
    seen = []
    monkeypatch.setattr(bridge, 'revise_pending_copy',
        lambda db, job, store, actor, computer, revision, candidate_hash, content_hash, content:
            seen.append((job.id, store, actor, computer, content)) or {'status': 'image_review'})
    path = f'/api/v1/execution-devices/local/{worker}/jobs/{identity}/catalog-ozon-copy-revision/' + 'c' * 32
    payload = {'revision': 'a' * 64, 'expected_candidate_hash': 'b' * 64,
               'expected_content_hash': 'd' * 64,
               'content': {'title': 'Павлин', 'description_ru': 'Описание', 'tags': ['#павлин'],
                           'tags_shortfall_reason': 'Других тем нет.'}}
    assert clients['other'].post(path, json=payload).status_code == 403
    assert owner.post(path, json=payload).status_code == 200
    assert seen[0][0:2] == (identity, 'c' * 32)
    assert seen[0][3] == worker
    assert seen[0][4] == payload['content']
