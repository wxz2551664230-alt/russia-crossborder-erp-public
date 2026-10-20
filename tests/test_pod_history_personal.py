"""POD history preview reads the old database only on the initiating PC."""
import hashlib
import base64
import gzip
import json
from pathlib import Path
import sqlite3
import sys
import threading

from fastapi.testclient import TestClient
from sqlalchemy import select

from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps
from erp.main import app
from erp.models import CatalogImportRun, CatalogProduct
from erp.services.accounts import create_user
from erp.services.pod_personal_import import accept_pod_history

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
from pod_import_worker import run as inspect_on_pc


def _source(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript('''
            create table products(id text, sku text, current_version_id text);
            create table product_versions(id text);
            create table product_variants(id text);
            create table product_oss_assets(id text);
            insert into products values('p1','HC-TEST','v1');
            insert into product_versions values('v1');
            insert into product_variants values('s1');
            insert into product_oss_assets values('a1');
        ''')


def test_pod_history_preview_uses_personal_source(monkeypatch, tmp_path):
    source = tmp_path / 'product_library.db'
    _source(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    settings = get_settings()
    monkeypatch.setattr(settings, 'multiuser_enabled', True)
    monkeypatch.setattr(settings, 'runtime_mode', 'server')
    monkeypatch.setattr(settings, 'session_cookie_secure', False)
    with SessionLocal() as db:
        create_user(db, username='pod-importer', display_name='pod-importer',
                    password='isolated-long-test-password', role='operator')
    worker_id = 'pc-' + 'd' * 32
    prefix = '/api/v1/execution-devices'
    with SessionLocal() as db:
        create_user(db, username='pod-other', display_name='pod-other',
                    password='isolated-long-test-password', role='operator')
    with TestClient(app) as browser, TestClient(app) as worker, TestClient(app) as other:
        for client, username in ((browser, 'pod-importer'), (worker, 'pod-importer'),
                                 (other, 'pod-other')):
            assert client.post('/api/v1/auth/login', json={'username': username,
                               'password': 'isolated-long-test-password'}).status_code == 200
        assert worker.post(prefix + '/local/register', json={'id': worker_id, 'name': 'POD PC',
                           'pod_import_version': 1}).status_code == 200
        assert browser.post(prefix + '/select', json={'worker_id': worker_id}).status_code == 200
        queued = browser.post('/api/v1/catalog/imports/pod',
                              json={'dry_run': True, 'source_database': str(source)})
        assert queued.status_code == 202, queued.text
        item = queued.json()
        job_id = item['job']['id']
        claimed = worker.post(prefix + '/local/' + worker_id + '/claim', json={}).json()
        assert claimed['id'] == job_id
        result = inspect_on_pc(None, '', claimed, tmp_path, threading.Event())
        assert result['counts'] == {'products': 1, 'product_versions': 1,
                                    'product_variants': 1, 'product_oss_assets': 1}
        assert result['broken_current_versions'] == 0
        assert worker.post(prefix + '/local/' + worker_id + '/jobs/' + job_id + '/complete',
                           json={'result': result}, headers={'X-Worker-Attempt': '1'}).status_code == 200
        listing = browser.get('/api/v1/catalog/imports').json()['items']
        assert listing[0]['status'] == 'completed'
        assert listing[0]['counts']['products'] == 1
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        with SessionLocal() as db:
            assert db.scalars(select(CatalogProduct)).first() is None

        formal = browser.post('/api/v1/catalog/imports/pod',
                              json={'dry_run': False, 'source_database': str(source)})
        assert formal.status_code == 202, formal.text
        formal_job = formal.json()['job']['id']
        assert worker.post(prefix + '/local/' + worker_id + '/claim', json={}).json()['id'] == formal_job
        packed = gzip.compress(json.dumps(_formal_value(), ensure_ascii=False).encode(), mtime=0)
        body = {'sha256': hashlib.sha256(packed).hexdigest(),
                'content': base64.b64encode(packed).decode()}
        formal_endpoint = prefix + '/local/' + worker_id + '/jobs/' + formal_job
        assert other.post(formal_endpoint + '/pod-import-result', json=body,
                          headers={'X-Worker-Attempt': '1'}).status_code == 403
        wrong = worker.post(formal_endpoint + '/pod-import-result',
                            json={**body, 'sha256': '0' * 64}, headers={'X-Worker-Attempt': '1'})
        assert wrong.status_code == 409
        accepted = worker.post(formal_endpoint + '/pod-import-result', json=body,
                               headers={'X-Worker-Attempt': '1'})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()['inserted'] == 1
        repeated = worker.post(formal_endpoint + '/pod-import-result', json=body,
                               headers={'X-Worker-Attempt': '1'})
        assert repeated.status_code == 200 and repeated.json() == accepted.json()
        with SessionLocal() as db:
            assert len(db.scalars(select(CatalogProduct)).all()) == 1


def _formal_value(name='Old product'):
    product = {'id': 'p1', 'sku': 'HC-TEST', 'current_version_id': 'v1',
               'family_no': 'HC-TEST', 'name': name, 'notes': 'Old notes',
               'status': 'ready', 'tags_json': '[]'}
    version = {'id': 'v1', 'version_no': 1, 'product_type_id': 'type1',
               'assets_json': '{}', 'recipe_json': '{}'}
    variant = {'id': 's1', 'product_id': 'p1', 'sku': 'HC-TEST-01',
               'active': 1, 'size_attributes_json': '[]', 'prices_json': '{}'}
    asset = {'id': 'a1', 'version_id': 'v1', 'asset_type': 'rendering',
             'name': 'main.jpg', 'oss_bucket': 'bucket', 'oss_key': 'pod/main.jpg',
             'oss_url': 'https://bucket.oss-cn-test.aliyuncs.com/pod/main.jpg',
             'sha256': 'a' * 64, 'size': 123, 'content_type': 'image/jpeg'}
    return {'schema': 1, 'source_sha256': 'b' * 64,
            'report': {'counts': {'products': 1, 'product_versions': 1,
                                  'product_variants': 1, 'product_oss_assets': 1},
                       'source_database': 'old.db'},
            'entries': [{'product': product, 'category': 'Home', 'version': version,
                         'variants': [variant], 'media': [asset]}]}


def test_pod_history_formal_adds_missing_and_preserves_shared_edits(tmp_path):
    with SessionLocal() as db:
        first = CatalogImportRun(kind='pod_history', source_path='old.db', dry_run=False)
        db.add(first); db.flush()
        result = accept_pod_history(db, first, _formal_value())
        assert result['inserted'] == 1 and result['variants'] == 1 and result['assets'] == 1
        db.commit()
        product = db.scalar(select(CatalogProduct).where(CatalogProduct.legacy_id == 'p1'))
        assert product.name == 'Old product'
        assert len(product.variants) == 1 and len(product.asset_links) == 1
        assert product.asset_links[0].asset.local_path == ''
        product.name = 'Cloud edited name'
        second = CatalogImportRun(kind='pod_history', source_path='old.db', dry_run=False)
        db.add(second); db.flush()
        again = accept_pod_history(db, second, _formal_value('Stale source name'))
        db.commit()
        assert again['inserted'] == again['variants'] == again['assets'] == again['links'] == 0
        assert db.scalar(select(CatalogProduct).where(CatalogProduct.legacy_id == 'p1')).name == 'Cloud edited name'


def test_pod_formal_replays_saved_result_without_reopening_source(tmp_path):
    job = {'type': 'catalog_pod_import', 'id': 'e' * 32, 'attempts': 2,
           'payload': {'dry_run': False, 'source_database': str(tmp_path / 'missing.db')}}
    journal = tmp_path / 'pod-import-tasks' / job['id'] / 'import-result.json.gz'
    journal.parent.mkdir(parents=True)
    journal.write_bytes(gzip.compress(b'isolated-result', mtime=0))

    class Client:
        def request(self, method, path, body, extra_headers):
            assert method == 'POST' and path.endswith('/pod-import-result')
            assert base64.b64decode(body['content']) == journal.read_bytes()
            assert extra_headers['X-Worker-Attempt'] == '2'
            return ({'status': 'completed', 'inserted': 0}, {})

    assert inspect_on_pc(Client(), '/execution-devices/local/test/jobs/' + job['id'],
                         job, tmp_path, threading.Event())['status'] == 'completed'
