import hashlib
import json
from types import SimpleNamespace as Obj
import pytest
from erp.domains.pod import desktop_wb as server
from erp.core.errors import ConflictError


def test_wait_releases_lease_and_preserves_exact_upload(monkeypatch):
    rows = [{'id': 'item', 'nm_id': 123, 'price': 124, 'discount': 30}]
    monkeypatch.setattr(server, 'price_snapshot', lambda *args: {'rows': rows})
    digest = hashlib.sha256(json.dumps({'rows': rows}, sort_keys=True).encode()).hexdigest()
    steps = [Obj(key='submit', status='pending', progress=0), Obj(key='verify', status='running', progress=0)]
    job = Obj(id='job', external_ref='', payload_json='{"ledger_ids":[]}', max_attempts=1, steps=steps, status='running', lease_owner='pc')
    session = Obj(add=lambda event: None, scalars=lambda query: [])
    value = {'upload_id': 777, 'digest': digest, 'wait_seconds': 835}
    assert server.price_progress(session, job, value)['wb_price_deferred']
    assert job.status == 'action_required' and not job.lease_owner and job.lease_expires_at is None
    assert job.error_code == 'wb_price_manual_review'
    assert job.max_attempts == 1
    assert steps[0].status == 'completed' and steps[1].status == 'pending'
    with pytest.raises(ConflictError): server.price_progress(session, job, {**value, 'upload_id': 778})
    with pytest.raises(ConflictError): server.price_progress(session, job, {**value, 'digest': 'other'})
