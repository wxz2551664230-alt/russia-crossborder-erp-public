import gzip
import hashlib
import json
import sqlite3
import base64
import threading
from pathlib import Path

import pytest
from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.core.errors import ConflictError
from erp.models import AdSku, Job, Store
from erp.services.ad_desktop_snapshot import analysis_download
from erp.services.ad_sync_result import apply_sync_result
from erp.services.ad_sync_transport import store_chunk, read_result, cleanup


def test_sync_delta_applies_once_and_rejects_foreign_store(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from ad_sync_worker import export_sync_delta
    monkeypatch.setattr(get_settings(), 'data_dir', tmp_path)
    with SessionLocal() as db:
        store = Store(id='a' * 32, platform='ozon', name='Ozon', enabled=True)
        other = Store(id='b' * 32, platform='ozon', name='Other', enabled=True)
        db.add_all([store, other]); db.flush()
        sku = AdSku(id='c' * 32, store_id=store.id, sku_id='SKU', name='before')
        foreign = AdSku(id='d' * 32, store_id=other.id, sku_id='OTHER', name='private')
        job = Job(id='e' * 32, type='ad_optimization_sync',
                  idempotency_key='sync-result-test',
                  payload_json=json.dumps({'store_id': store.id, 'days': 3}))
        db.add_all([sku, foreign, job]); db.commit()
        frozen, snapshot_hash, source_hash = analysis_download(
            db, job.id, store.id, sync_input=True)
    database = tmp_path / 'client.sqlite'
    database.write_bytes(gzip.decompress(frozen.read_bytes()))
    with sqlite3.connect(database) as check:
        check.execute("update ad_skus set name='after' where id=?", ('c' * 32,))
        check.commit()
    output = tmp_path / 'sync-result.json.gz'
    counts = export_sync_delta(database, frozen, output, store_id='a' * 32,
                               snapshot_sha256=snapshot_hash, source_sha256=source_hash,
                               summary={'mode': 'live_reviewed', 'executed': False})
    assert counts['ad_skus'] == 1
    payload = json.loads(gzip.decompress(output.read_bytes()))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    with SessionLocal() as db:
        job = db.get(Job, 'e' * 32)
        bad = json.loads(json.dumps(payload))
        bad['changes']['ad_skus']['upserts'][0]['store_id'] = 'b' * 32
        with pytest.raises(ConflictError, match='其他店铺'):
            apply_sync_result(db, job, bad, digest, output.stat().st_size)
        db.rollback()
        receipt = apply_sync_result(db, job, payload, digest, output.stat().st_size)
        db.commit()
        assert receipt['changed'] == 1
        assert apply_sync_result(db, job, payload, digest, output.stat().st_size) == receipt
        assert db.get(AdSku, 'c' * 32).name == 'after'
        assert db.get(AdSku, 'd' * 32).name == 'private'


def test_sync_chunk_resume_and_integrity(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), 'data_dir', tmp_path)
    content = gzip.compress(json.dumps({'schema': 3}).encode(), mtime=0)
    digest = hashlib.sha256(content).hexdigest()
    job_id = 'f' * 32
    halfway = len(content) // 2
    pieces = (content[:halfway], content[halfway:])
    first = base64.b64encode(pieces[0]).decode()
    second = base64.b64encode(pieces[1]).decode()
    assert store_chunk(job_id, index=0, total=2, digest=digest, encoded=first)['received']
    assert store_chunk(job_id, index=0, total=2, digest=digest, encoded=first)['received']
    with pytest.raises(ConflictError, match='完整'):
        read_result(job_id, digest=digest)
    with pytest.raises(ConflictError, match='不一致'):
        store_chunk(job_id, index=0, total=2, digest=digest, encoded=second)
    store_chunk(job_id, index=1, total=2, digest=digest, encoded=second)
    assert read_result(job_id, digest=digest) == ({'schema': 3}, len(content))
    cleanup(job_id)
    assert not (tmp_path / 'ad_sync_uploads' / job_id).exists()


def test_pc_resumes_journaled_sync_result_without_rerunning_ozon(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from ad_sync_worker import run
    from erp.services.ad_sync_result import TABLES

    job_id, store_id = 'a' * 32, 'b' * 32
    work = tmp_path / 'ad-tasks' / job_id
    work.mkdir(parents=True)
    raw = tmp_path / 'source.sqlite'
    with sqlite3.connect(raw) as check:
        check.execute('create table placeholder (value text)')
        check.commit()
    frozen = gzip.compress(raw.read_bytes(), mtime=0)
    snapshot_hash = hashlib.sha256(frozen).hexdigest()
    source_hash = 'c' * 64
    (work / 'input.sqlite').write_bytes(raw.read_bytes())
    (work / 'input.sqlite.gz').write_bytes(frozen)
    (work / 'input.sha256').write_text(snapshot_hash, encoding='ascii')
    (work / 'source.sha256').write_text(source_hash, encoding='ascii')
    payload = {'schema': 3, 'store_id': store_id,
               'snapshot_sha256': snapshot_hash, 'source_sha256': source_hash,
               'summary': {'mode': 'live_reviewed', 'executed': False},
               'changes': {model.__tablename__: {'upserts': [], 'deletes': []} for model in TABLES}}
    packed = gzip.compress(json.dumps(payload).encode(), mtime=0)
    (work / 'sync-result.json.gz').write_bytes(packed)
    digest = hashlib.sha256(packed).hexdigest()
    calls = []
    class Client:
        def request(self, method, path, body=None, *, extra_headers=None):
            calls.append((method, path, body))
            if path.endswith('/ad-sync-credentials'):
                raise AssertionError('已有同步结果不能再次领取凭据或调用 Ozon')
            if path.endswith('/ad-sync-chunk'):
                return {'received': True}, {}
            if path.endswith('/ad-sync-result'):
                return {'store_id': store_id, 'changed': 0,
                        'result_sha256': digest, 'result_bytes': len(packed)}, {}
            raise AssertionError(path)
    job = {'id': job_id, 'type': 'ad_optimization_sync',
           'payload': {'store_id': store_id, 'days': 3}, 'attempts': 2}
    result = run(Client(), f'/execution-devices/local/pc-{"d" * 32}/jobs/{job_id}',
                 job, tmp_path, threading.Event())
    assert result['changed'] == 0
    assert [item[1].split('/')[-1] for item in calls] == ['ad-sync-chunk', 'ad-sync-result']
