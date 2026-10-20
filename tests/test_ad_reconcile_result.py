import base64
import gzip
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock
from threading import Event

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.models import AdAction, AdExecutionBatch, AdSku, Job, Store, utc_now
from erp.services import ad_execution
from erp.services.ad_desktop_snapshot import analysis_download
from erp.services.ad_reconcile_result import accept_reconcile_result


def test_lost_upload_reuses_durable_pc_receipt_without_requery(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    import ad_reconcile_worker
    from erp_desktop import TemporaryConnectionError
    job_id = 'a' * 32
    approval_id = 'b' * 32
    store_id = 'c' * 32
    job = {'id': job_id, 'type': 'ad_optimization_reconcile', 'attempts': 1,
           'created_by': 'd' * 32,
           'payload': {'store_id': store_id, 'approval_id': approval_id}}
    monkeypatch.setattr(ad_reconcile_worker, 'download_snapshot',
                        lambda *_args, **_kwargs: (tmp_path / 'input.sqlite', 'e' * 64, 'f' * 64))
    queried = []
    monkeypatch.setattr(ad_reconcile_worker, 'reconcile_snapshot',
                        lambda *_args: queried.append(True))
    def export(_database, _job, _snapshot, _source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b'durable-read-only-result')
    monkeypatch.setattr(ad_reconcile_worker, 'export_result', export)
    class Client:
        attempts = 0
        def request(self, method, path, payload=None, extra_headers=None):
            if method == 'GET':
                return ({'store_id': store_id, 'performance_client_id': 'id',
                         'performance_client_secret': 'secret'}, None)
            self.attempts += 1
            if self.attempts <= 3:
                raise TemporaryConnectionError('offline')
            return ({'approval_id': approval_id,
                     'result_sha256': hashlib.sha256(b'durable-read-only-result').hexdigest()}, None)
    client = Client()
    with pytest.raises(TemporaryConnectionError):
        ad_reconcile_worker.run(client, '/execution-devices/local/pc-' + 'd' * 32 + '/jobs/' + job_id,
                                job, tmp_path, Event())
    assert len(queried) == 1
    ad_reconcile_worker.run(client, '/execution-devices/local/pc-' + 'd' * 32 + '/jobs/' + job_id,
                            job, tmp_path, Event())
    assert len(queried) == 1


@pytest.mark.parametrize('change_source', [False, True])
def test_original_pc_read_only_check_updates_cloud_only_after_matching_receipt(tmp_path, monkeypatch, change_source):
    monkeypatch.setattr(get_settings(), 'data_dir', tmp_path)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from ad_execution_worker import export_result
    actor = 'a' * 32
    remote = Mock()
    remote.campaign_products.return_value = []
    monkeypatch.setattr(ad_execution, '_client', lambda *_: remote)
    monkeypatch.setattr(ad_execution, 'analyze_store', lambda *_: None)
    with SessionLocal() as cloud:
        store = Store(platform='ozon', name='Ad reconciliation', enabled=True)
        cloud.add(store)
        cloud.flush()
        sku = AdSku(store_id=store.id, sku_id='SKU-1', name='One')
        cloud.add(sku)
        cloud.flush()
        item = {'state_id': 'b' * 32, 'ad_sku_id': sku.id, 'sku_id': 'SKU-1',
                'campaign_id': 'campaign-1', 'previous_target_drr': 0.05,
                'proposed_target_drr': None, 'break_even_drr': 0.25,
                'reason': 'review evidence', 'evidence': ['facts'], 'metrics': {}}
        batch = AdExecutionBatch(store_id=store.id, action='STOP_ADS_CANDIDATE',
                                 actor=actor, status='unknown', active_store=store.id,
                                 preview_json=json.dumps({'state_ids': [item['state_id']],
                                                          'items': [item], 'blocked': []}),
                                 expires_at=utc_now() + timedelta(minutes=5))
        cloud.add(batch)
        cloud.flush()
        audit = AdAction(ad_sku_id=sku.id, action=batch.action, status='unknown',
                         previous_target_drr=0.05, proposed_target_drr=None,
                         reason='review evidence',
                         before_metrics_json=json.dumps({'approval_id': batch.id,
                                                          'proposal': item}),
                         decided_at=utc_now())
        cloud.add(audit)
        cloud.flush()
        batch.results_json = json.dumps([{'sku_id': 'SKU-1', 'campaign_id': 'campaign-1',
                                          'status': 'unknown', 'audit_id': audit.id}])
        job = Job(type='ad_optimization_reconcile', created_by=actor,
                  target_worker_id='pc-' + 'c' * 32,
                  idempotency_key='ad-reconcile-result-' + str(change_source),
                  payload_json=json.dumps({'store_id': store.id, 'approval_id': batch.id}),
                  status='running', attempts=1)
        cloud.add(job)
        cloud.commit()
        store_id, batch_id, job_id, sku_id = store.id, batch.id, job.id, sku.id
        archive, snapshot_hash, source_hash = analysis_download(
            cloud, job_id, store_id, reconcile_batch=batch)
    database = tmp_path / 'pc-reconcile.sqlite'
    database.write_bytes(gzip.decompress(archive.read_bytes()))
    engine = create_engine('sqlite:///' + database.as_posix())
    try:
        with Session(engine) as pc:
            result = ad_execution.reconcile(pc, batch_id, store_id)
            assert result['status'] == 'completed'
    finally:
        engine.dispose()
    remote.campaign_products.assert_called_once_with('campaign-1')
    remote.delete_campaign_products.assert_not_called()
    remote.update_campaign_products.assert_not_called()
    result_file = tmp_path / 'reconcile.json.gz'
    export_result(database, {'payload': {'store_id': store_id, 'approval_id': batch_id},
                             'created_by': actor}, snapshot_hash, source_hash, result_file)
    encoded = base64.b64encode(result_file.read_bytes()).decode('ascii')
    with SessionLocal() as cloud:
        if change_source:
            cloud.get(AdSku, sku_id).name = 'Changed after snapshot'
            cloud.commit()
        job = cloud.get(Job, job_id)
        received = accept_reconcile_result(cloud, job, encoded)
        assert received['batch_status'] == ('unknown' if change_source else 'completed')
        assert accept_reconcile_result(cloud, job, encoded) == received
        cloud.commit()
    with SessionLocal() as cloud:
        batch = cloud.get(AdExecutionBatch, batch_id)
        audit = cloud.scalar(select(AdAction).where(AdAction.ad_sku_id == sku_id))
        assert batch.status == ('unknown' if change_source else 'completed')
        assert batch.active_store == (store_id if change_source else None)
        assert audit.status == ('unknown' if change_source else 'executed')
