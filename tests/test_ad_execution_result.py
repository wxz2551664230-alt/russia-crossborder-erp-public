import base64
import gzip
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.models import AdAction, AdExecutionBatch, AdSku, Job, Store, utc_now
from erp.schemas import AdBatchExecutionRequest
from erp.services import ad_execution
from erp.services.ad_desktop_snapshot import analysis_download, execution_source_fingerprint
from erp.services.ad_execution_result import accept_execution_result


@pytest.mark.parametrize('change_source', [False, True])
def test_pc_original_ad_write_returns_once_and_cloud_applies_same_approval(tmp_path, monkeypatch, change_source):
    monkeypatch.setattr(get_settings(), 'data_dir', tmp_path)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from ad_execution_worker import export_result

    actor = 'a' * 32
    state_id = 'b' * 32
    remote = Mock()
    remote.delete_campaign_products.return_value = {'ok': True}
    monkeypatch.setattr(ad_execution, '_client', lambda *_: remote)
    monkeypatch.setattr(ad_execution, 'analyze_store', lambda *_: None)
    with SessionLocal() as cloud:
        store = Store(platform='ozon', name='Cloud ad', enabled=True)
        cloud.add(store)
        cloud.flush()
        sku = AdSku(store_id=store.id, sku_id='SKU-1', name='One')
        cloud.add(sku)
        cloud.flush()
        item = {'state_id': state_id, 'ad_sku_id': sku.id, 'sku_id': 'SKU-1',
                'name': 'One', 'campaign_id': 'campaign-1',
                'previous_target_drr': 0.05, 'proposed_target_drr': None,
                'break_even_drr': 0.25, 'reason': 'review evidence',
                'evidence': ['facts'], 'metrics': {}}
        plan = {'state_ids': [state_id], 'items': [item], 'blocked': []}
        monkeypatch.setattr(ad_execution, '_plan', lambda *_: plan)
        batch = AdExecutionBatch(store_id=store.id, action='STOP_ADS_CANDIDATE',
                                 actor=actor, status='executing', active_store=store.id,
                                 preview_json=json.dumps(plan),
                                 expires_at=utc_now() + timedelta(minutes=10))
        cloud.add(batch)
        cloud.flush()
        job = Job(type='ad_optimization_execute', created_by=actor,
                  target_worker_id='pc-' + 'c' * 32,
                  idempotency_key='personal-ad-result',
                  payload_json=json.dumps({'store_id': store.id, 'approval_id': batch.id,
                                           'action': batch.action, 'state_ids': [state_id]}),
                  status='running', attempts=1)
        cloud.add(job)
        cloud.commit()
        store_id, batch_id, job_id = store.id, batch.id, job.id
        archive, snapshot_hash, source_hash = analysis_download(
            cloud, job_id, store_id, execution_batch=batch)
        assert execution_source_fingerprint(cloud, batch) == source_hash
        cloud.commit()
    database = tmp_path / 'pc-ad.sqlite'
    database.write_bytes(gzip.decompress(archive.read_bytes()))
    engine = create_engine('sqlite:///' + database.as_posix())
    try:
        with Session(engine) as pc:
            request = AdBatchExecutionRequest(action='STOP_ADS_CANDIDATE',
                                              state_ids=[state_id], approval_id=batch_id,
                                              confirmed=True)
            assert ad_execution.execute(pc, store_id, request, actor)['succeeded'] == 1
            assert ad_execution.execute(pc, store_id, request, actor)['succeeded'] == 1
    finally:
        engine.dispose()
    assert remote.delete_campaign_products.call_count == 1
    result_file = tmp_path / 'result.json.gz'
    result_receipt = export_result(database, {'payload': {'store_id': store_id,
                            'approval_id': batch_id}, 'created_by': actor},
                            snapshot_hash, source_hash, result_file)
    with SessionLocal() as cloud:
        job = cloud.get(Job, job_id)
        assert execution_source_fingerprint(cloud, cloud.get(AdExecutionBatch, batch_id)) == source_hash
        if change_source:
            cloud.get(AdSku, item['ad_sku_id']).name = 'Changed while PC executed'
            cloud.commit()
        received = accept_execution_result(cloud, job,
                                            base64.b64encode(result_file.read_bytes()).decode())
        if change_source:
            assert received['batch_status'] == 'unknown'
        else:
            assert received == result_receipt
        assert accept_execution_result(cloud, job,
                                       base64.b64encode(result_file.read_bytes()).decode()) == received
        cloud.commit()
    with SessionLocal() as cloud:
        batch = cloud.get(AdExecutionBatch, batch_id)
        assert batch.status == ('unknown' if change_source else 'completed')
        assert batch.active_store == (store_id if change_source else None)
        audit = cloud.scalar(select(AdAction).where(AdAction.ad_sku_id == item['ad_sku_id']))
        assert audit is not None and audit.status == ('unknown' if change_source else 'executed')
