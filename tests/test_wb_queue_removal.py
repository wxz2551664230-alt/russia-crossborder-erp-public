import pytest

from erp.core.database import SessionLocal
from erp.models import CatalogProduct, CatalogVariant, Store, WbPublicationItem, Job, JobStep, utc_now
from erp.services.jobs import enqueue_job


def seed(status='cancelled', submitted=False):
    with SessionLocal() as session:
        store=Store(platform='wb',name='test',enabled=True)
        product=CatalogProduct(sku='S2B-test',name='桌布',source_type='pod')
        session.add_all([store,product]);session.flush()
        variant=CatalogVariant(product_id=product.id,sku='S2B-test-1')
        session.add(variant);session.flush()
        job=enqueue_job(session,job_type='pod_wb_api_publish',payload={},config_snapshot='{}',idempotency_key='test-wb-remove',steps=[('copy','文案'),('submit','提交')])
        job.status=status
        if submitted:
            step=next(s for s in job.steps if s.key=='submit')
            step.started_at=utc_now();step.status='failed'
        row=WbPublicationItem(store_id=store.id,product_id=product.id,variant_id=variant.id,job_id=job.id,offer_id='test-offer',status='running',mode='api')
        session.add(row);session.commit()
        return row.id,job.id,product.id,store.id


def test_stopped_queue_can_be_removed_without_deleting_product(client):
    identity,job_id,product_id,store_id=seed()
    rows=client.get('/api/v1/pod/wb/publications?stage=processing').json()['items']
    assert rows[0]['status']=='cancelled' and rows[0]['can_remove']
    for _ in range(2):
        assert client.post(f'/api/v1/pod/wb/publications/{identity}/remove').status_code==200
    assert client.get('/api/v1/pod/wb/publications?stage=processing').json()['total']==0
    assert client.post(f'/api/v1/jobs/{job_id}/retry').status_code==409
    with SessionLocal() as session:
        assert session.get(CatalogProduct,product_id)
        assert session.get(Job,job_id)
        assert session.get(WbPublicationItem,identity).status=='withdrawn'
    available=client.post('/api/v1/pod/wb/availability',json={'store_id':store_id,'product_ids':[product_id]}).json()
    assert not available['products'][product_id]['blocked']


@pytest.mark.parametrize('status,submitted',[('running',False),('paused',False),('cancelled',True),('failed',True)])
def test_active_or_possibly_submitted_record_is_not_removable(client,status,submitted):
    identity,_,_,_=seed(status,submitted)
    assert client.post(f'/api/v1/pod/wb/publications/{identity}/remove').status_code==409
    with SessionLocal() as session:
        assert session.get(WbPublicationItem,identity).status=='running'
