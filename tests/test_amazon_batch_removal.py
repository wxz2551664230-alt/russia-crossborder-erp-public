from uuid import uuid4

from sqlalchemy import func, select

from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps, loads
from erp.models import AmazonListingBatch, AmazonListingItem, CatalogProduct, Job
from test_amazon_selection_groups import setup_groups
from test_personal_job_controls import team


def created(team, monkeypatch, count=100):
    owner, worker, store, request = setup_groups(team, monkeypatch, count=count)
    result = owner.post('/api/v1/amazon/batch-groups', json=request)
    assert result.status_code == 201, result.text
    return owner, worker, store, request, result.json()


def test_codex_group_is_one_row_with_product_weighted_progress(team, monkeypatch):
    owner, worker, store, request, group = created(team, monkeypatch, count=41)
    with SessionLocal() as db:
        row = db.get(AmazonListingBatch, group['batch_ids'][0]); row.status = 'ready'
        db.get(Job, row.job_id).status = 'completed'
        db.commit()
    listing = owner.get('/api/v1/amazon/batches', params={'store_id':store, 'grouped':True}).json()
    assert listing['total'] == 1 and len(listing['items']) == 1
    row = listing['items'][0]
    assert row['count'] == 41 and row['batch_count'] == 3 and row['progress'] == 49
    assert set(row['batch_ids']) == set(group['batch_ids']) and row['can_delete']
    filtered = owner.get('/api/v1/amazon/batches', params={'grouped':True,'status':'ready'}).json()
    assert filtered['items'][0]['count'] == 41  # Filter the whole group, not its child rows.
    task = owner.get('/api/v1/jobs/overview?module=amazon').json()['items'][0]
    assert task['job_count'] == 3 and task['total_items'] == 41 and task['progress'] == 49
    assert task['counts']['completed'] == 20 and task['counts']['paused'] == 21
    assert len(owner.get('/api/v1/jobs/overview/'+task['id']).json()['children']) == 3


def test_remove_whole_group_releases_products_and_cannot_resurrect(team, monkeypatch):
    owner, worker, store, request, group = created(team, monkeypatch)
    result = owner.delete('/api/v1/amazon/batch-groups/'+group['id'])
    assert result.status_code == 200, result.text
    assert result.json()['released_count'] == 100
    assert owner.get('/api/v1/amazon/batches?grouped=true').json()['items'] == []
    assert owner.get('/api/v1/amazon/batches').json()['items'] == []
    assert owner.get('/api/v1/amazon/products',params={'store_id':store}).json()['total'] == 100
    assert owner.get('/api/v1/jobs/overview?module=amazon').json()['items'] == []
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AmazonListingItem)) == 0
        assert db.scalar(select(func.count()).select_from(CatalogProduct)) == 100
        assert all(db.get(AmazonListingBatch,id).status == 'deleted' for id in group['batch_ids'])
        assert all(loads(db.get(Job,b['job_id']).payload_json,{})['amazon_batch_removed'] for b in group['batches'])
    for batch in group['batches']:
        assert owner.post('/api/v1/jobs/'+batch['job_id']+'/retry').status_code == 409
        assert owner.get('/api/v1/codex-handoffs/'+batch['job_id']+'/local-context',params={'worker_id':worker}).status_code == 409
        assert owner.get('/api/v1/codex-handoffs/batches/'+batch['batch_id']).status_code == 409
        assert owner.post('/api/v1/amazon/batches/'+batch['batch_id']+'/validate').status_code == 409
    assert owner.delete('/api/v1/amazon/batch-groups/'+group['id']).json()['released_count'] == 0
    assert owner.post('/api/v1/amazon/batch-groups',json=request).status_code == 409
    request['request_id'] = uuid4().hex
    replacement = owner.post('/api/v1/amazon/batch-groups',json=request)
    assert replacement.status_code == 201,replacement.text
    assert [b['parent_sku'] for b in replacement.json()['batches']] == [f'US-P-{n:06d}' for n in range(6,11)]


def test_running_or_submitted_member_blocks_entire_removal(team, monkeypatch):
    owner, worker, store, request, group = created(team, monkeypatch, count=40)
    ids = group['batch_ids']
    with SessionLocal() as db:
        job = db.get(Job, group['batches'][1]['job_id']); job.status = 'running'; job.lease_owner = worker
        db.commit()
    failed = owner.post('/api/v1/amazon/batches/remove',json={'batch_ids':ids})
    assert failed.status_code == 409 and '正在执行' in failed.text
    with SessionLocal() as db:
        assert all(db.get(AmazonListingBatch,id).status == 'waiting_codex' for id in ids)
        assert db.scalar(select(func.count()).select_from(AmazonListingItem)) == 42
        job = db.get(Job, group['batches'][1]['job_id']); job.status = 'paused'; job.lease_owner = ''
        db.add(Job(type='amazon_submit',status='failed',created_by='owner',target_worker_id=worker,
                   idempotency_key=uuid4().hex,payload_json=dumps({'batch_id':ids[1],'store_id':store})))
        db.commit()
    assert owner.post('/api/v1/amazon/batches/remove',json={'batch_ids':ids}).status_code == 409
    listing = owner.get('/api/v1/amazon/batches?grouped=true').json()['items'][0]
    assert not listing['can_delete'] and '提交' in listing['delete_blocked_reason']
    assert owner.get('/api/v1/amazon/products',params={'store_id':store}).json()['total'] == 0


def test_single_removal_preserves_siblings_and_owner_controls(team, monkeypatch):
    owner, worker, store, request, group = created(team, monkeypatch, count=40)
    identity = group['batch_ids'][0]
    for name in ['other','admin','viewer']:
        result = team[0][name].delete('/api/v1/amazon/batches/'+identity)
        assert result.status_code in {403,409}
    assert owner.delete('/api/v1/amazon/batches/'+identity).status_code == 200
    remaining = owner.get('/api/v1/amazon/batch-groups/'+group['id']).json()
    assert remaining['batch_ids'] == group['batch_ids'][1:]
    assert remaining['removed_batch_ids'] == [identity] and remaining['count'] == 20
    assert owner.get('/api/v1/amazon/products',params={'store_id':store}).json()['total'] == 20
    assert owner.get('/api/v1/amazon/batches/'+group['batch_ids'][1]).status_code == 200
    # An invalid ID must not partially remove valid siblings.
    bad = owner.post('/api/v1/amazon/batches/remove',json={'batch_ids':[group['batch_ids'][1],uuid4().hex]})
    assert bad.status_code == 404
    assert owner.get('/api/v1/amazon/products',params={'store_id':store}).json()['total'] == 20
