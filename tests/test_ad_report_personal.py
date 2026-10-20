import hashlib
import json
from datetime import timedelta
from pathlib import Path

from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.models import AdSku, Job, Store, utc_now
from erp.services.ad_desktop_snapshot import create_analysis_snapshot
from test_personal_job_controls import team


def test_original_report_archive_is_generated_on_pc_and_downloaded_from_cloud(team, tmp_path, monkeypatch):
    import sys
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from ad_report_worker import create_report_archive

    clients, worker = team
    monkeypatch.setattr(get_settings(), 'data_dir', tmp_path)
    with SessionLocal() as db:
        store = Store(platform='ozon', name='report store', enabled=True)
        db.add(store)
        db.flush()
        db.add(AdSku(store_id=store.id, sku_id='REPORT-SKU', name='Report'))
        db.commit()
        store_id = store.id
        db_path = tmp_path / 'pc-ad.sqlite'
        create_analysis_snapshot(db, store_id, db_path)
    archive = tmp_path / 'pc-report.zip'
    summary = create_report_archive(db_path, store_id, tmp_path / 'original-reports', archive)
    assert {'DATA_AUDIT.md', 'STORE_AD_OVERVIEW.md', 'THRESHOLD_REPORT.md'} <= set(summary['files'])
    owner = clients['owner']
    owner.post('/api/v1/execution-devices/local/register', json={'id': worker, 'name': 'PC', 'ad_report_version': 1})
    with SessionLocal() as db:
        from erp.models import WorkerNode
        device = db.get(WorkerNode, worker)
        job = Job(type='ad_optimization_reports', created_by=device.owner_user_id,
                  target_worker_id=worker, idempotency_key='test-ad-report-personal',
                  payload_json=json.dumps({'store_id': store_id}), status='running',
                  lease_owner=worker, attempts=1,
                  lease_expires_at=utc_now() + timedelta(minutes=5))
        db.add(job)
        db.commit()
        job_id = job.id
    url = f'/api/v1/execution-devices/local/{worker}/jobs/{job_id}'
    headers = {'X-Worker-Attempt': '1'}
    snapshot = owner.get(url + '/ad-analysis-snapshot', headers=headers)
    assert snapshot.status_code == 200, snapshot.text
    import base64
    payload = {'content': base64.b64encode(archive.read_bytes()).decode(),
               'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
               'snapshot_sha256': snapshot.headers['x-content-sha256'],
               'source_sha256': snapshot.headers['x-source-sha256'],
               'summary': summary}
    assert clients['other'].post(url + '/ad-report-result', json=payload, headers=headers).status_code == 403
    invalid = dict(payload, source_sha256='0' * 64)
    assert owner.post(url + '/ad-report-result', json=invalid, headers=headers).status_code == 409
    import io
    import zipfile
    malicious = io.BytesIO()
    with zipfile.ZipFile(malicious, 'w') as zip_file:
        for name in ('DATA_AUDIT.md', 'STORE_AD_OVERVIEW.md', 'THRESHOLD_REPORT.md', '../escape.md'):
            zip_file.writestr(name, 'test')
    bad = dict(payload, content=base64.b64encode(malicious.getvalue()).decode(),
               sha256=hashlib.sha256(malicious.getvalue()).hexdigest(),
               summary=dict(summary, files=['DATA_AUDIT.md', 'STORE_AD_OVERVIEW.md', 'THRESHOLD_REPORT.md', '../escape.md']))
    assert owner.post(url + '/ad-report-result', json=bad, headers=headers).status_code == 409
    receipt = owner.post(url + '/ad-report-result', json=payload, headers=headers)
    assert receipt.status_code == 200, receipt.text
    assert owner.post(url + '/ad-report-result', json=payload, headers=headers).json() == receipt.json()
    complete = owner.post(url + '/complete', json={'result': receipt.json()}, headers=headers)
    assert complete.status_code == 200, complete.text
    assert complete.json()['status'] == 'completed', complete.json()
    download = owner.get('/api/v1' + receipt.json()['download_path'])
    assert download.status_code == 200, download.text
    assert download.content == archive.read_bytes()
