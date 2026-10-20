from pathlib import Path
from types import SimpleNamespace
import pytest
from cryptography.fernet import Fernet
from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps
from erp.services import catalog_personal_submit as server
from test_catalog_personal_publications import prepared


@pytest.fixture
def import_flow(prepared, monkeypatch):
    session, job, publication, child, result = prepared
    job.type = 'catalog_ozon_publication'
    job.payload_json = dumps({'publication_id': publication.id, 'store_ids': [child.store_id]})
    job.external_ref = ''
    child.status = 'submit_queued'; child.external_product_id = ''
    child.assets_json = dumps(result['assets'])
    monkeypatch.setattr(server, 'get_publication', lambda *args: publication)
    return session, job, publication, child


def test_intent_task_and_result_are_bound_and_idempotent(import_flow):
    session, job, publication, child = import_flow
    snapshot = server.snapshot(session, job)
    intent = server.intent(session, job, snapshot['item_hash'])
    assert child.status == 'import_uncertain'
    assert server.intent(session, job, snapshot['item_hash']) == intent
    with pytest.raises(ConflictError): server.accept_task(session, job, 'wrong', 7)
    server.accept_task(session, job, snapshot['item_hash'], 7)
    with pytest.raises(ConflictError): server.accept_task(session, job, snapshot['item_hash'], 8)
    result = {'result': {'items': [{'offer_id': child.offer_id, 'product_id': 99, 'status': 'imported'}]}}
    receipt = server.accept_import(session, job, snapshot['item_hash'], 7, result)
    assert child.status == 'stock_pending' and child.external_product_id == '99'
    assert server.accept_task(session, job, snapshot['item_hash'], 7) == {'task_id': '7'}
    assert child.status == 'stock_pending'
    assert server.accept_import(session, job, snapshot['item_hash'], 7, result) == receipt
    assert server.finish(session, job, receipt) == {'status': 'action_required'}
    assert job.steps[-1].status == 'pending'
    assert server.finish(session, job, receipt) == {'status': 'action_required'}
    result['result']['items'][0]['product_id'] = 100
    with pytest.raises(ConflictError): server.accept_import(session, job, snapshot['item_hash'], 7, result)


def test_unknown_without_receipt_cannot_start_new_import(import_flow):
    session, job, publication, child = import_flow
    child.status = 'import_uncertain'
    with pytest.raises(ConflictError): server.intent(session, job, server.snapshot(session, job)['item_hash'])


@pytest.mark.parametrize('mode', ['success', 'task_response_lost', 'unknown', 'pending'])
def test_pc_import_resumes_without_resending(import_flow, tmp_path, monkeypatch, mode):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from catalog_ozon_submit_worker import submit, ImportOutcomeUnknown
    session, job, publication, child = import_flow
    cipher = Fernet(Fernet.generate_key())
    def protect(data, decrypt=False): return cipher.decrypt(data) if decrypt else cipher.encrypt(data)
    counts = {'import': 0, 'query': 0}; lost = []
    def create(item):
        counts['import'] += 1
        if mode == 'unknown': raise OSError('response lost')
        return 7
    def query(task):
        assert task == 7
        counts['query'] += 1
        return {'result': {'items': [{'offer_id': child.offer_id, 'product_id': 99,
                  'status': 'pending' if mode == 'pending' and counts['query'] == 1 else 'imported'}]}}
    def post(path, body):
        if path.endswith('intent'): return server.intent(session, job, body['item_hash'])
        if path.endswith('task'):
            if mode == 'task_response_lost' and not lost:
                lost.append(True); raise OSError('cloud disconnected before receipt')
            return server.accept_task(session, job, body['item_hash'], body['task_id'])
        return server.accept_import(session, job, body['item_hash'], body['task_id'], body['result'])
    adapter = SimpleNamespace(import_product=create, wait_import=query)
    initial = server.snapshot(session, job)
    if mode == 'unknown':
        with pytest.raises(ImportOutcomeUnknown): submit(initial, tmp_path, protect, post, lambda: None, adapter)
        with pytest.raises(ImportOutcomeUnknown): submit(server.snapshot(session, job), tmp_path, protect, post, lambda: None, adapter)
        assert counts == {'import': 1, 'query': 0}
        return
    if mode == 'task_response_lost':
        with pytest.raises(OSError): submit(initial, tmp_path, protect, post, lambda: None, adapter)
    else:
        submit(initial, tmp_path, protect, post, lambda: None, adapter)
    receipt = submit(server.snapshot(session, job), tmp_path, protect, post, lambda: None, adapter)
    assert receipt['status'] == 'stock_pending'
    assert counts['import'] == 1
    assert counts['query'] == (2 if mode == 'pending' else 1)


def test_store_binding_survives_other_store_submission(import_flow):
    import copy
    session, job, publication, child=import_flow
    state=__import__('json').loads(child.assets_json)
    state['personal_submit_job_id']=job.id
    child.assets_json=dumps(state)
    second=copy.deepcopy(child);second.id='second-child';second.store_id='second-store'
    second.offer_id='SECOND-SKU'
    state2=copy.deepcopy(state);state2['personal_submit_job_id']='second-job'
    state2['import_item']['offer_id']='SECOND-SKU'
    second.assets_json=dumps(state2)
    publication.stores.append(second);publication.job_id='second-job'
    first=server.snapshot(session,job)
    assert first['store_id']==child.store_id
    server.intent(session,job,first['item_hash'])
    server.accept_task(session,job,first['item_hash'],7)
    job2=copy.deepcopy(job);job2.id='second-job';job2.external_ref=''
    job2.payload_json=dumps({'publication_id':publication.id,'store_ids':['second-store']})
    assert server.snapshot(session,job2)['store_id']=='second-store'
    wrong=copy.deepcopy(job);wrong.id='unrelated'
    with pytest.raises(ConflictError):server.snapshot(session,wrong)
