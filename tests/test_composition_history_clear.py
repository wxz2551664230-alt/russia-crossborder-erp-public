"""Exercise the original clear button through cloud routing and role checks."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from deploy.pilot_gateway import allowed, app
from erp.auth_models import UserAuditEvent
from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps
from erp.domains.pod.models import PodComposition, PodCompositionSide
from erp.integrations.amazon import AmazonUSClient
from erp.models import Asset, CatalogProduct, CatalogProductAsset, Job
from test_personal_job_controls import team

PATH = '/api/v1/pod/legacy-api/jobs/finished'


def test_clear_route_exposes_only_the_existing_queue_clear_operation():
    assert allowed('DELETE', PATH)
    for path in ['/api/v1/pod/legacy-api/jobs', PATH + '/extra', PATH + '-all',
                 '/api/v1/pod/legacy-api/jobs/' + 'a' * 32,
                 '/api/v1/pod/legacy-api/library/products']:
        assert not allowed('DELETE', path)
    assert not allowed('POST', PATH)
    assert not allowed('PATCH', PATH)


@pytest.mark.parametrize('role', ['owner', 'admin'])
def test_clear_via_cloud_preserves_live_work_products_sources_and_audit(team, monkeypatch, role):
    clients, worker = team
    # Queue housekeeping must not contact any supplier or marketplace.
    monkeypatch.setattr('erp.domains.pod.composition.supplier_client', lambda *a, **k: pytest.fail('No supplier calls'))
    monkeypatch.setattr(AmazonUSClient, 'request', lambda *a, **k: pytest.fail('No marketplace calls'))
    with SessionLocal() as db:
        product = CatalogProduct(sku='CLEAR-KEEP', name='Existing product', source_type='pod')
        asset = Asset(kind='image', name='design.png', sha256='a' * 64)
        db.add_all([product, asset]); db.flush()
        db.add(CatalogProductAsset(product_id=product.id, asset_id=asset.id, role='main'))
        entries = {}
        for n, status in enumerate(['completed', 'failed', 'cancelled', 'queued', 'running', 'paused']):
            job = Job(type='pod_native_composition', status=status, target_worker_id=worker,
                      idempotency_key='clear-history:' + status, payload_json=dumps({'preserved': status}))
            db.add(job); db.flush()
            # The effective status is the Job status, not the stale composition status.
            composition = PodComposition(job_id=job.id, supplier='hicustom', status='queued',
                                         product_name=status, catalog_product_id=product.id,
                                         oss_assets_json=dumps([{'asset_id': asset.id}]))
            db.add(composition); db.flush()
            db.add(PodCompositionSide(composition_id=composition.id, supplier='hicustom', view_id=n,
                                      asset_id=asset.id, gallery_code='retained-' + status))
            entries[status] = (composition.id, job.id, job.payload_json)
        db.commit(); product_id, asset_id = product.id, asset.id
    response = clients[role].delete(PATH)
    assert response.status_code == 200, response.text
    assert response.json() == {'removed': 3, 'records_preserved': True}
    visible = clients[role].get('/api/v1/pod/legacy-api/jobs').json()['jobs']
    assert {row['job_id'] for row in visible} == {entries[status][1] for status in ['queued', 'running', 'paused']}
    assert clients[role].delete(PATH).json() == {'removed': 0, 'records_preserved': True}
    with SessionLocal() as db:
        for status, (composition_id, job_id, payload) in entries.items():
            composition, job = db.get(PodComposition, composition_id), db.get(Job, job_id)
            assert composition.status == ('cleared' if status in {'completed', 'failed', 'cancelled'} else 'queued')
            assert job.status == status and job.payload_json == payload
            assert composition.catalog_product_id == product_id
            assert composition.oss_assets_json == dumps([{'asset_id': asset_id}])
        assert db.get(CatalogProduct, product_id) and db.get(Asset, asset_id)
        assert db.scalar(select(func.count()).select_from(PodCompositionSide)) == 6
        assert db.scalar(select(func.count()).select_from(CatalogProductAsset)) == 1
        events = db.scalars(select(UserAuditEvent).where(UserAuditEvent.target == '/pod/legacy-api/jobs/finished')).all()
        assert len(events) == 2 and all(event.status_code == 200 for event in events)


def test_clear_denies_anonymous_viewer_and_cross_origin_requests(team):
    clients, _ = team
    with SessionLocal() as db:
        item = PodComposition(supplier='s2b', status='completed', product_name='Protected record')
        db.add(item); db.commit(); composition_id = item.id
    with TestClient(app) as anonymous:
        assert anonymous.delete(PATH).status_code == 401
    assert clients['viewer'].delete(PATH).status_code == 403
    rejected = clients['owner'].delete(PATH, headers={'Origin': 'https://untrusted.example'})
    assert rejected.status_code == 403 and rejected.json()['code'] == 'origin_rejected'
    with SessionLocal() as db:
        assert db.get(PodComposition, composition_id).status == 'completed'
