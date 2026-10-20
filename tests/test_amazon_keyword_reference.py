import pytest
from sqlalchemy import select

from erp.core.database import SessionLocal
from erp.models import Job
from test_amazon_selection_groups import setup_groups, counts
from test_personal_job_controls import team


@pytest.mark.parametrize('reference', ['', ('shower curtain\nfabric curtain\ncortina de baño\n' * 60)[:2439], '浴帘🌲' * 6666 + 'ab'],
                         ids=['empty', '2439-characters', '20000-unicode-characters'])
def test_keyword_reference_reaches_each_codex_snapshot_without_truncation(team, monkeypatch, reference):
    owner, worker, _, request = setup_groups(team, monkeypatch, count=3)
    request['keywords'] = reference
    assert owner.get('/api/v1/amazon/pricing').json()['limits']['keyword_reference_max_chars'] == 20000
    preview = {k: v for k, v in request.items() if k != 'request_id'}
    grouped = owner.post('/api/v1/amazon/group-preview', json=preview)
    assert grouped.status_code == 200, grouped.text
    legacy = {k: v for k, v in preview.items() if k not in {'merge', 'merge_group_size'}}
    plain = owner.post('/api/v1/amazon/preview', json=legacy)
    assert plain.status_code == 200 and plain.json()['keywords'] == reference
    created = owner.post('/api/v1/amazon/batch-groups', json=request)
    assert created.status_code == 201, created.text
    for batch in created.json()['batches']:
        detail = owner.get('/api/v1/amazon/batches/' + batch['batch_id']).json()
        assert detail['snapshot']['keywords'] == reference
        context = owner.get(f"/api/v1/codex-handoffs/{batch['job_id']}/local-context?worker_id={worker}")
        assert context.status_code == 200, context.text
        assert context.json()['snapshot']['batch']['keywords'] == reference
        assert context.json()['snapshot']['rules']['keywords_max_utf8_bytes'] == 249


@pytest.mark.parametrize('endpoint', ['preview', 'group-preview', 'batches', 'batch-groups'])
def test_excessive_reference_returns_field_details_without_creating_tasks(team, monkeypatch, endpoint):
    owner, _, _, request = setup_groups(team, monkeypatch, count=3)
    request['keywords'] = 'private-reference' + 'x' * 20000
    if endpoint != 'batch-groups':
        request.pop('request_id')
    if endpoint in {'preview', 'batches'}:
        request.pop('merge'); request.pop('merge_group_size')
    failed = owner.post('/api/v1/amazon/' + endpoint, json=request)
    assert failed.status_code == 422, failed.text
    assert failed.json()['code'] == 'validation_error'
    assert failed.json()['details'][0]['loc'] == ['body', 'keywords']
    assert '20000' in failed.json()['details'][0]['msg']
    assert 'private-reference' not in failed.text
    assert counts() == [0, 0]
    with SessionLocal() as db:
        assert not db.scalar(select(Job).where(Job.type == 'amazon_prepare_codex'))
    # A rejected request may be edited and retried using the same request ID.
    if endpoint == 'batch-groups':
        request['keywords'] = '\n'.join(f'shower curtain pattern {n}' for n in range(100))
        assert len(request['keywords']) > 1500
        fixed = owner.post('/api/v1/amazon/batch-groups', json=request)
        assert fixed.status_code == 201, fixed.text
