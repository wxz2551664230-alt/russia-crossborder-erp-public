import copy
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps, loads
from erp.models import Job, AmazonListingBatch, SystemSetting
from erp.services import amazon_listings as listings
from erp.services.amazon_listing_payload import digest
from erp.integrations.amazon import AmazonUSClient
from test_personal_job_controls import team
from test_amazon_listing_flow import setup_flow, definition, content, lease, finish


def create(team, monkeypatch, *, modern=True, required_fact=False):
    owner, worker, store_id, request = setup_flow(team)
    # Model and marketplace traffic is forbidden throughout material preparation.
    monkeypatch.setattr(AmazonUSClient, 'request', lambda *a, **k: pytest.fail('Unexpected platform call'))
    with SessionLocal() as db:
        d = definition()
        if required_fact:
            d['schema']['properties']['fabric_type'] = {'type': 'array'}
            d['schema']['required'].append('fabric_type')
        db.add(SystemSetting(key=listings.cache_key(listings.store(db, store_id), 'MOUSE_PAD', 'NONE'), value_json=dumps(d)))
        db.commit()
    if modern:
        r = owner.post('/api/v1/execution-devices/local/register', json={
            'id': worker, 'name': 'PC', 'amazon_version': 1, 'codex_generation_version': 4})
        assert r.status_code == 200, r.text
    request['generation_provider'] = 'codex'
    r = owner.post('/api/v1/amazon/batches', json=request)
    return owner, worker, store_id, r


def context(owner, worker, job):
    r = owner.get(f'/api/v1/codex-handoffs/{job}/local-context?worker_id={worker}')
    assert r.status_code == 200, r.text
    return r.json()


def receipt(ctx):
    snap = ctx['snapshot']; copies = {i['sku']: content() for i in snap['batch']['items']}
    return {snap['store_id']: {'revision': snap['revision'], 'output_sha256': digest(copies), 'copies': copies}}


def resume(owner, worker, job, value):
    return owner.post(f'/api/v1/codex-handoffs/{job}/resume', json={'worker_id': worker, 'receipts': value})


def test_old_client_cannot_claim_new_codex_work(team, monkeypatch):
    owner, worker, store_id, r = create(team, monkeypatch, modern=False)
    assert r.status_code == 409, r.text
    with SessionLocal() as db:
        assert not db.query(AmazonListingBatch).count()
        assert not db.query(Job).filter_by(type='amazon_prepare_codex').count()


def test_task_waits_without_external_requests_and_prompt_is_recoverable(team, monkeypatch):
    owner, worker, store_id, r = create(team, monkeypatch)
    assert r.status_code == 201, r.text
    batch, job = r.json()['batch_id'], r.json()['job_id']
    assert owner.get(f'/api/v1/amazon/batches/{batch}').json()['status'] == 'waiting_codex'
    assert owner.post(f'/api/v1/execution-devices/local/{worker}/claim').json() is None
    ctx = context(owner, worker, job)
    assert ctx['kind'] == 'amazon_listing_copy' and ctx['status'] == 'paused'
    raw = json.dumps(ctx)
    assert 'private' not in raw and 'lwa_client_secret' not in raw and 'config_snapshot_json' not in raw
    assert ctx['snapshot']['batch']['items'][0]['pricing']['price'] == '20.92'
    bundle = owner.get(f'/api/v1/codex-handoffs/batches/{batch}').json()
    assert bundle['jobs'][0]['job_id'] == job and '英文' in bundle['prompt']
    assert '249' in bundle['prompt'] and 'output-template.json' in bundle['prompt']
    assert 'tags_ru' not in bundle['prompt'] and 'poster_text_lines_ru' not in bundle['prompt']
    assert team[0]['other'].get(f'/api/v1/codex-handoffs/{job}/local-context?worker_id={worker}').status_code in {403, 409}
    assert team[0]['admin'].get(f'/api/v1/codex-handoffs/batches/{batch}').status_code == 409
    assert resume(owner, 'another-pc', job, receipt(ctx)).status_code == 409
    assert owner.post(f'/api/v1/amazon/batches/{batch}/validate').status_code == 409


@pytest.mark.parametrize('change', ['hash', 'revision', 'missing_sku', 'extra_sku', 'protected', 'keywords', 'schema'])
def test_invalid_or_changed_output_stays_waiting(team, monkeypatch, change):
    owner, worker, store_id, r = create(team, monkeypatch)
    job = r.json()['job_id']; ctx = context(owner, worker, job); value = receipt(ctx)
    entry = value[store_id]; sku = next(iter(entry['copies']))
    if change == 'hash': entry['output_sha256'] = '0' * 64
    if change == 'revision': entry['revision'] = '0' * 64
    if change == 'missing_sku': entry['copies'] = {}
    if change == 'extra_sku': entry['copies']['unrelated'] = content()
    if change == 'protected': entry['copies'][sku]['attributes'] = {'purchasable_offer': []}
    if change == 'keywords': entry['copies'][sku]['keywords'] = 'é' * 125
    if change == 'schema':
        with SessionLocal() as db:
            setting = db.get(SystemSetting, listings.cache_key(listings.store(db, store_id), 'MOUSE_PAD', 'NONE'))
            data = loads(setting.value_json, {}); data['checksum'] = 'changed'; setting.value_json = dumps(data); db.commit()
    if change not in {'hash', 'revision'}: entry['output_sha256'] = digest(entry['copies'])
    assert resume(owner, worker, job, value).status_code == 409
    assert owner.get('/api/v1/jobs/' + job).json()['status'] == 'paused'


def test_replay_exact_output_then_explicit_review_preview_and_submit(team, monkeypatch):
    owner, worker, store_id, r = create(team, monkeypatch)
    job, batch = r.json()['job_id'], r.json()['batch_id']; ctx = context(owner, worker, job)
    value = receipt(ctx)
    assert resume(owner, worker, job, value).json()['status'] == 'queued'
    assert resume(owner, worker, job, value).json()['status'] == 'queued'
    changed = copy.deepcopy(value)
    next(iter(changed[store_id]['copies'].values()))['description'] = 'Different material copy.'
    changed[store_id]['output_sha256'] = digest(changed[store_id]['copies'])
    assert resume(owner, worker, job, changed).status_code == 409
    endpoint, headers = lease(owner, worker, job)
    assert owner.post(endpoint + '/amazon-result', json={'result': {'content': changed[store_id]['copies']}}, headers=headers).status_code == 409
    result = finish(owner, endpoint, headers, {'content': value[store_id]['copies']})
    assert result['status'] == 'ready'
    assert resume(owner, worker, job, value).json()['status'] == 'completed'
    url = f'/api/v1/amazon/batches/{batch}'
    assert owner.post(url + '/submit', json={'draft_digest': '0' * 64}).status_code == 409
    v = owner.post(url + '/validate'); assert v.status_code == 200, v.text
    endpoint, headers = lease(owner, worker, v.json()['job_id'])
    sku = next(iter(value[store_id]['copies']))
    assert finish(owner, endpoint, headers, {'items': [{'sku': sku, 'response': {'status': 'VALID'}}]})['status'] == 'validated'
    detail = owner.get(url).json()
    s = owner.post(url + '/submit', json={'draft_digest': detail['result']['validated_digest']})
    assert s.status_code == 200, s.text
    # This isolated task is queued only; no actual platform write occurs in test.
    assert owner.get('/api/v1/jobs/' + s.json()['job_id']).json()['type'] == 'amazon_submit'
    assert owner.get(f'/api/v1/codex-handoffs/batches/{batch}').json()['jobs'][0]['job_id'] == job


def test_unverified_required_facts_are_displayed_for_human_completion(team, monkeypatch):
    owner, worker, store_id, r = create(team, monkeypatch, required_fact=True)
    job = r.json()['job_id']; value = receipt(context(owner, worker, job))
    assert resume(owner, worker, job, value).status_code == 200
    endpoint, headers = lease(owner, worker, job)
    assert finish(owner, endpoint, headers, {'content': value[store_id]['copies']})['status'] == 'needs_facts'
    detail = owner.get('/api/v1/amazon/batches/' + r.json()['batch_id']).json()
    assert any('fabric_type' in e['path'] + e['message'] for e in detail['draft']['errors'])


def test_desktop_export_submit_resume_and_worker_consume_without_models(team, monkeypatch, tmp_path):
    desktop = str(Path(__file__).resolve().parents[1] / 'client' / 'desktop')
    monkeypatch.syspath_prepend(desktop)
    import codex_bridge, amazon_codex_bridge, amazon_worker, erp_desktop
    owner, worker, store_id, r = create(team, monkeypatch)
    job, batch = r.json()['job_id'], r.json()['batch_id']
    monkeypatch.setattr(erp_desktop, 'protect', lambda b, decrypt=False: b)
    monkeypatch.setattr(amazon_codex_bridge, 'protect', lambda b, decrypt=False: b)
    monkeypatch.setattr(amazon_codex_bridge, 'profile_dir', lambda: tmp_path)
    monkeypatch.setattr(codex_bridge, 'identity', lambda: worker)
    monkeypatch.setattr(codex_bridge, 'runtime', lambda: None)
    monkeypatch.setattr(codex_bridge, 'reference_files', lambda *a: ['isolated-reference.jpg'])
    monkeypatch.setattr(amazon_worker, 'generate', lambda *a, **k: pytest.fail('Unexpected paid generation'))
    monkeypatch.setattr(AmazonUSClient, '__init__', lambda *a, **k: pytest.fail('No marketplace client in Codex preparation'))
    class Client:
        def request(self, method, path, data=None, extra_headers=None):
            response = owner.request(method, '/api/v1' + path, json=data, headers=extra_headers)
            assert response.status_code < 300, response.text
            return response.json(), response.headers
        def get(self, path): return self.request('GET', path)[0]
    client = Client(); ctx = context(owner, worker, job)
    exported = amazon_codex_bridge.export(ctx, tmp_path / 'exports')
    output = Path(exported['output']); value = json.loads(output.read_text(encoding='utf-8'))
    value['copies'] = receipt(ctx)[store_id]['copies']; output.write_text(json.dumps(value), encoding='utf-8')
    args = SimpleNamespace(job=job, store=store_id, file=str(output))
    codex_bridge.submit(client, args)
    assert owner.get('/api/v1/jobs/' + job).json()['status'] == 'paused'
    codex_bridge.resume(client, args)
    endpoint, headers = lease(owner, worker, job)
    leased = owner.get('/api/v1/jobs/' + job).json()
    result = amazon_worker.run(client, endpoint.removeprefix('/api/v1'), leased, tmp_path, threading.Event())
    assert result['amazon']['status'] == 'ready'
    assert owner.post(endpoint + '/complete', json={'result': result}, headers=headers).status_code == 200
    assert not list(tmp_path.rglob('*paid*'))
    with pytest.raises(ValueError): codex_bridge.submit(client, args)


def test_missing_desktop_output_does_not_fall_back(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from amazon_codex_bridge import consume, CodexOutputMissing
    with pytest.raises(CodexOutputMissing): consume({}, tmp_path, 'job', lambda b, decrypt=False: b)
