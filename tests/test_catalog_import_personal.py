"""A self-listing preview runs on the initiating PC and writes no product rows."""
import hashlib
import base64
import gzip
import io
import json
import threading
import zipfile
import sys
from contextlib import ExitStack
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
from catalog_import_worker import _extract_bundle

from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.core.security import vault
from erp.main import app
from erp.models import CatalogProduct, IntegrationConnection
from erp.services.accounts import create_user
from erp.services.catalog import parse_self_selected_workbook
from sqlalchemy import select


def _workbook(*, bad: bool = False) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(['来源链接', '中文名称', 'SKU', '拿货价', '长(cm)', '宽(cm)', '高(cm)', '重量(g)', '主图链接', '附图链接', '商品名称'])
    sheet.append(['invalid-link' if bad else 'https://detail.1688.com/item/1', '测试收纳包', 'TEST-1', 12.5, 20, 10, 4, 300,
                  'https://oss.test/main.jpg', 'https://oss.test/extra.jpg', 'Чехол'])
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


def test_bundle_extraction_stays_on_personal_computer(tmp_path):
    source = tmp_path / 'bundle.zip'
    expected_workbook = _workbook()
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('product.xlsx', expected_workbook)
        archive.writestr('images/main.jpg', b'image-bytes')
    workbook = _extract_bundle(source, tmp_path / 'task')
    assert workbook.read_bytes() == expected_workbook
    assert (workbook.parent / 'images/main.jpg').read_bytes() == b'image-bytes'


def test_bundle_extraction_rejects_escape_and_duplicate_names(tmp_path):
    for index, names in enumerate((['../outside.txt', 'product.xlsx'],
                                   ['product.xlsx', 'PRODUCT.xlsx'])):
        source = tmp_path / f'bad-{index}.zip'
        with zipfile.ZipFile(source, 'w') as archive:
            for name in names:
                archive.writestr(name, _workbook() if name.endswith('.xlsx') else b'bad')
        try:
            _extract_bundle(source, tmp_path / f'task-{index}')
        except ValueError:
            pass
        else:
            raise AssertionError('不安全的资料包路径未被拒绝')


def test_bundle_preview_is_leased_and_server_keeps_zip_unextracted(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, 'multiuser_enabled', True)
    monkeypatch.setattr(settings, 'runtime_mode', 'server')
    monkeypatch.setattr(settings, 'session_cookie_secure', False)
    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    monkeypatch.setattr('erp.api.routes.catalog._selected_category_report',
                        lambda *args: {'ozon_category': {'store_id': 'test', 'description_category_id': 10,
                                                        'type_id': 20, 'display': '测试'}})
    with SessionLocal() as db:
        create_user(db, username='bundle-importer', display_name='bundle-importer',
                    password='isolated-long-test-password', role='operator')
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as archive:
        archive.writestr('product.xlsx', _workbook())
        archive.writestr('images/main.jpg', b'image-bytes')
    source = content.getvalue()
    worker_id = 'pc-' + 'b' * 32
    prefix = '/api/v1/execution-devices'
    with TestClient(app) as browser, TestClient(app) as worker:
        for client in (browser, worker):
            assert client.post('/api/v1/auth/login', json={'username': 'bundle-importer',
                               'password': 'isolated-long-test-password'}).status_code == 200
        assert worker.post(prefix + '/local/register', json={'id': worker_id, 'name': 'Bundle PC',
                           'catalog_import_version': 1}).status_code == 200
        assert browser.post(prefix + '/select', json={'worker_id': worker_id}).status_code == 200
        form = {'store_id': 'test', 'description_category_id': '10', 'type_id': '20'}
        queued = browser.post('/api/v1/catalog/imports/self-selected/bundle',
                              data={'dry_run': 'true', **form}, files={'file': ('bundle.zip', source)})
        assert queued.status_code == 202, queued.text
        item = queued.json()
        assert Path(item['import']['source_path']).suffix == '.zip'
        assert list((tmp_path / 'imports').iterdir()) == [Path(item['import']['source_path'])]
        job_id = item['job']['id']
        assert worker.post(prefix + '/local/' + worker_id + '/claim', json={}).json()['id'] == job_id
        endpoint = prefix + '/local/' + worker_id + '/jobs/' + job_id
        headers = {'X-Worker-Attempt': '1'}
        fetched = worker.get(endpoint + '/catalog-import-source', headers=headers)
        assert fetched.status_code == 200 and fetched.content == source
        local = tmp_path / 'personal' / 'source.zip'
        local.parent.mkdir()
        local.write_bytes(fetched.content)
        workbook = _extract_bundle(local, local.parent)
        rows, errors = parse_self_selected_workbook(workbook)
        result = {'sha256': hashlib.sha256(source).hexdigest(), 'rows': len(rows),
                  'product_groups': len({row['group_code'] for row in rows}),
                  'local_images': 0, 'errors': errors}
        completed = worker.post(endpoint + '/complete', json={'result': result}, headers=headers)
        assert completed.status_code == 200, completed.text
        assert completed.json()['status'] == 'completed'


def test_bundle_formal_result_requires_own_lease_and_merges_once(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, 'multiuser_enabled', True)
    monkeypatch.setattr(settings, 'runtime_mode', 'server')
    monkeypatch.setattr(settings, 'session_cookie_secure', False)
    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    monkeypatch.setattr('erp.api.routes.catalog._selected_category_report',
                        lambda *args: {'ozon_category': {'store_id': 'test', 'description_category_id': 10,
                                                        'type_id': 20, 'display': '测试'}})
    with SessionLocal() as db:
        create_user(db, username='bundle-formal', display_name='bundle-formal',
                    password='isolated-long-test-password', role='operator')
        create_user(db, username='bundle-other', display_name='bundle-other',
                    password='isolated-long-test-password', role='operator')
        db.add(IntegrationConnection(provider='oss', name='OZON 自动上架 OSS', enabled=True,
                                     base_url='https://oss-cn-test.aliyuncs.com',
                                     config_json=json.dumps({'bucket': 'bucket', 'region': 'cn-test',
                                                             'prefix': 'ozon-auto-listing'}),
                                     secrets_encrypted=vault.encrypt({'access_key_id': 'isolated',
                                                                      'access_key_secret': 'secret'})))
        db.commit()
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as archive:
        archive.writestr('product.xlsx', _workbook())
    source = content.getvalue()
    worker_id = 'pc-' + 'c' * 32
    prefix = '/api/v1/execution-devices'
    with TestClient(app) as browser, TestClient(app) as worker, TestClient(app) as other:
        for client, username in ((browser, 'bundle-formal'), (worker, 'bundle-formal'),
                                 (other, 'bundle-other')):
            assert client.post('/api/v1/auth/login', json={'username': username,
                               'password': 'isolated-long-test-password'}).status_code == 200
        assert worker.post(prefix + '/local/register', json={'id': worker_id, 'name': 'Bundle PC',
                           'catalog_import_version': 1}).status_code == 200
        assert browser.post(prefix + '/select', json={'worker_id': worker_id}).status_code == 200
        form = {'store_id': 'test', 'description_category_id': '10', 'type_id': '20'}
        queued = browser.post('/api/v1/catalog/imports/self-selected/bundle',
                              data={'dry_run': 'false', **form}, files={'file': ('bundle.zip', source)})
        assert queued.status_code == 202, queued.text
        job_id = queued.json()['job']['id']
        assert worker.post(prefix + '/local/' + worker_id + '/claim', json={}).json()['id'] == job_id
        endpoint = prefix + '/local/' + worker_id + '/jobs/' + job_id
        headers = {'X-Worker-Attempt': '1'}
        assert other.get(endpoint + '/catalog-import-oss', headers=headers).status_code == 403
        credentials = worker.get(endpoint + '/catalog-import-oss', headers=headers)
        assert credentials.status_code == 200
        assert credentials.headers['cache-control'] == 'no-store'
        assert credentials.json()['secrets']['access_key_id'] == 'isolated'
        workbook = tmp_path / 'local.xlsx'
        workbook.write_bytes(_workbook())
        records, errors = parse_self_selected_workbook(workbook)
        payload = {'schema': 2, 'source_sha256': hashlib.sha256(source).hexdigest(),
                   'records': records, 'errors': errors, 'images': []}
        packed = gzip.compress(json.dumps(payload, ensure_ascii=False).encode(), mtime=0)
        body = {'sha256': hashlib.sha256(packed).hexdigest(),
                'content': base64.b64encode(packed).decode()}
        assert other.post(endpoint + '/catalog-import-result', json=body, headers=headers).status_code == 403
        first = worker.post(endpoint + '/catalog-import-result', json=body, headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()['inserted'] == 1
        repeated = worker.post(endpoint + '/catalog-import-result', json=body, headers=headers)
        assert repeated.status_code == 200 and repeated.json() == first.json()
        with SessionLocal() as db:
            assert len(db.scalars(select(CatalogProduct)).all()) == 1


def test_personal_self_selected_preview_is_leased_and_read_only(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, 'multiuser_enabled', True)
    monkeypatch.setattr(settings, 'runtime_mode', 'server')
    monkeypatch.setattr(settings, 'session_cookie_secure', False)
    monkeypatch.setattr('erp.api.routes.catalog._selected_category_report',
                        lambda *args: {'ozon_category': {'store_id': 'test', 'description_category_id': 10,
                                                        'type_id': 20, 'display': '测试'}})
    with SessionLocal() as db:
        for name in ('importer', 'other'):
            create_user(db, username=name, display_name=name, password='isolated-long-test-password', role='operator')
    worker_id = 'pc-' + 'a' * 32
    prefix = '/api/v1/execution-devices'
    content = _workbook()
    with ExitStack() as stack:
        browser, worker, other = [stack.enter_context(TestClient(app)) for _ in range(3)]
        for client, username in ((browser, 'importer'), (worker, 'importer'), (other, 'other')):
            assert client.post('/api/v1/auth/login', json={'username': username,
                               'password': 'isolated-long-test-password'}).status_code == 200
        assert worker.post(prefix + '/local/register', json={'id': worker_id, 'name': 'Import PC',
                           'catalog_import_version': 1}).status_code == 200
        assert browser.post(prefix + '/select', json={'worker_id': worker_id}).status_code == 200
        form = {'store_id': 'test', 'description_category_id': '10', 'type_id': '20'}
        queued = browser.post('/api/v1/catalog/imports/self-selected', data={'dry_run': 'true', **form},
                              files={'file': ('sample.xlsx', content)})
        assert queued.status_code == 202, queued.text
        item = queued.json()
        job_id = item['job']['id']
        assert worker.post(prefix + '/local/' + worker_id + '/claim', json={}).json()['id'] == job_id
        endpoint = prefix + '/local/' + worker_id + '/jobs/' + job_id
        headers = {'X-Worker-Attempt': '1'}
        assert other.get(endpoint + '/catalog-import-source', headers=headers).status_code == 403
        source = worker.get(endpoint + '/catalog-import-source', headers=headers)
        assert source.status_code == 200
        assert source.content == content
        assert source.headers['x-content-sha256'] == hashlib.sha256(content).hexdigest()
        workbook_path = tmp_path / 'sample.xlsx'
        workbook_path.write_bytes(source.content)
        rows, errors = parse_self_selected_workbook(workbook_path)
        result = {'sha256': hashlib.sha256(content).hexdigest(), 'rows': len(rows),
                  'product_groups': len({row['group_code'] for row in rows}),
                  'local_images': 0, 'errors': errors}
        assert not errors
        assert worker.post(endpoint + '/complete', json={'result': {**result, 'sha256': '0' * 64}},
                           headers=headers).status_code == 409
        assert other.post(endpoint + '/complete', json={'result': result}, headers=headers).status_code == 403
        completed = worker.post(endpoint + '/complete', json={'result': result}, headers=headers)
        assert completed.status_code == 200, completed.text
        assert completed.json()['status'] == 'completed'
        assert worker.post(endpoint + '/complete', json={'result': result}, headers=headers).status_code == 200
        listing = browser.get('/api/v1/catalog/imports').json()['items']
        assert listing[0]['id'] == item['import']['id']
        assert listing[0]['status'] == 'completed'
        assert listing[0]['counts'] == {'rows': 1, 'product_groups': 1, 'local_images': 0, 'errors': 0}
        with SessionLocal() as db:
            assert db.scalars(select(CatalogProduct)).first() is None
        invalid_content = _workbook(bad=True)
        invalid = browser.post('/api/v1/catalog/imports/self-selected', data={'dry_run': 'true', **form},
                               files={'file': ('invalid.xlsx', invalid_content)})
        assert invalid.status_code == 202
        invalid_job = invalid.json()['job']['id']
        assert worker.post(prefix + '/local/' + worker_id + '/claim', json={}).json()['id'] == invalid_job
        invalid_path = tmp_path / 'invalid.xlsx'
        invalid_path.write_bytes(invalid_content)
        invalid_rows, invalid_errors = parse_self_selected_workbook(invalid_path)
        assert invalid_errors
        invalid_result = {'sha256': hashlib.sha256(invalid_content).hexdigest(), 'rows': len(invalid_rows),
                          'product_groups': 1, 'local_images': 0, 'errors': invalid_errors}
        invalid_endpoint = prefix + '/local/' + worker_id + '/jobs/' + invalid_job
        assert worker.post(invalid_endpoint + '/complete', json={'result': invalid_result},
                           headers=headers).status_code == 200
        assert browser.get('/api/v1/catalog/imports').json()['items'][0]['status'] == 'validation_failed'


def test_personal_formal_excel_result_is_atomic_and_idempotent(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, 'multiuser_enabled', True)
    monkeypatch.setattr(settings, 'runtime_mode', 'server')
    monkeypatch.setattr(settings, 'session_cookie_secure', False)
    monkeypatch.setattr('erp.api.routes.catalog._selected_category_report',
                        lambda *args: {'ozon_category': {'store_id': 'test', 'description_category_id': 10,
                                                        'type_id': 20, 'display': '箱包 / 收纳包'}})
    with SessionLocal() as db:
        for name in ('importer', 'other'):
            create_user(db, username=name, display_name=name, password='isolated-long-test-password', role='operator')
    worker_id = 'pc-' + 'a' * 32
    prefix = '/api/v1/execution-devices'
    content = _workbook()
    with ExitStack() as stack:
        browser, worker, other = [stack.enter_context(TestClient(app)) for _ in range(3)]
        for client, username in ((browser, 'importer'), (worker, 'importer'), (other, 'other')):
            assert client.post('/api/v1/auth/login', json={'username': username,
                               'password': 'isolated-long-test-password'}).status_code == 200
        assert worker.post(prefix + '/local/register', json={'id': worker_id, 'name': 'Import PC',
                           'catalog_import_version': 1}).status_code == 200
        assert browser.post(prefix + '/select', json={'worker_id': worker_id}).status_code == 200
        queued = browser.post('/api/v1/catalog/imports/self-selected',
                              data={'dry_run': 'false', 'store_id': 'test',
                                    'description_category_id': '10', 'type_id': '20'},
                              files={'file': ('sample.xlsx', content)})
        assert queued.status_code == 202, queued.text
        job_id = queued.json()['job']['id']
        assert worker.post(prefix + '/local/' + worker_id + '/claim', json={}).json()['id'] == job_id
        endpoint = prefix + '/local/' + worker_id + '/jobs/' + job_id
        headers = {'X-Worker-Attempt': '1'}
        source = worker.get(endpoint + '/catalog-import-source', headers=headers)
        assert source.status_code == 200 and source.content == content
        workbook_path = tmp_path / 'formal.xlsx'
        workbook_path.write_bytes(content)
        rows, errors = parse_self_selected_workbook(workbook_path)
        value = {'schema': 1, 'source_sha256': hashlib.sha256(content).hexdigest(),
                 'records': rows, 'errors': errors}
        packed = gzip.compress(json.dumps(value, ensure_ascii=False).encode(), mtime=0)
        body = {'sha256': hashlib.sha256(packed).hexdigest(),
                'content': base64.b64encode(packed).decode('ascii')}
        assert worker.post(endpoint + '/complete', json={'result': {'ok': True}}, headers=headers).status_code == 409
        assert other.post(endpoint + '/catalog-import-result', json=body, headers=headers).status_code == 403
        assert worker.post(endpoint + '/catalog-import-result', json={**body, 'sha256': '0' * 64},
                           headers=headers).status_code == 409
        result = worker.post(endpoint + '/catalog-import-result', json=body, headers=headers)
        assert result.status_code == 200, result.text
        assert result.json() == {'inserted': 1, 'updated': 0, 'rejected_groups': 0, 'status': 'completed'}
        assert worker.post(endpoint + '/catalog-import-result', json=body, headers=headers).json() == result.json()
        assert worker.post(endpoint + '/complete', json={'result': result.json()}, headers=headers).status_code == 200
        with SessionLocal() as db:
            products = db.scalars(select(CatalogProduct).where(CatalogProduct.sku == 'TEST-1')).all()
            assert len(products) == 1
            assert len(products[0].variants) == 1
            assert len(products[0].asset_links) == 2
            assert all(not link.asset.local_path for link in products[0].asset_links)


def test_personal_workbook_validator_checks_download_hash(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    import catalog_import_worker

    content = _workbook()
    expected = hashlib.sha256(content).hexdigest()

    class Response(io.BytesIO):
        def __init__(self, digest):
            super().__init__(content)
            self.headers = {'X-Content-SHA256': digest}

    class Client:
        base = 'https://erp.example.test'
        profile = {'cookie': 'test-cookie'}

    job = {'id': 'b' * 32, 'type': 'catalog_self_selected_import', 'attempts': 1,
           'payload': {'dry_run': True, 'source_sha256': expected}}
    endpoint = '/execution-devices/local/pc-' + 'a' * 32 + '/jobs/' + job['id']

    def opener(digest):
        class Opener:
            def open(self, *args, **kwargs):
                return Response(digest)
        return Opener()

    monkeypatch.setattr(catalog_import_worker.urllib.request, 'build_opener', lambda *args: opener('0' * 64))
    try:
        catalog_import_worker.run(Client(), endpoint, job, tmp_path, threading.Event())
        assert False, 'wrong download hash must fail'
    except ValueError as error:
        assert '校验值' in str(error)
    assert not (tmp_path / 'catalog-import-tasks' / job['id'] / 'validation.json').exists()
    monkeypatch.setattr(catalog_import_worker.urllib.request, 'build_opener', lambda *args: opener(expected))
    result = catalog_import_worker.run(Client(), endpoint, job, tmp_path, threading.Event())
    assert result['sha256'] == expected
    assert result['rows'] == result['product_groups'] == 1
    assert result['errors'] == []
    assert catalog_import_worker.run(Client(), endpoint, job, tmp_path, threading.Event()) == result


def test_personal_formal_worker_journals_same_result_for_retry(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    import catalog_import_worker

    content = _workbook()
    expected = hashlib.sha256(content).hexdigest()

    class Response(io.BytesIO):
        headers = {'X-Content-SHA256': expected}
        def __init__(self):
            super().__init__(content)

    class Opener:
        def open(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(catalog_import_worker.urllib.request, 'build_opener', lambda *args: Opener())
    calls = []

    class Client:
        base = 'https://erp.example.test'
        profile = {'cookie': 'test-cookie'}
        def request(self, method, path, body, *, extra_headers):
            calls.append((method, path, body['sha256'], body['content']))
            return ({'inserted': 1, 'updated': 0, 'rejected_groups': 0, 'status': 'completed'}, {})

    job = {'id': 'c' * 32, 'type': 'catalog_self_selected_import', 'attempts': 1,
           'payload': {'dry_run': False, 'source_sha256': expected}}
    endpoint = '/execution-devices/local/pc-' + 'a' * 32 + '/jobs/' + job['id']
    first = catalog_import_worker.run(Client(), endpoint, job, tmp_path, threading.Event())
    assert first['inserted'] == 1
    journal = tmp_path / 'catalog-import-tasks' / job['id'] / 'import-result.json.gz'
    assert journal.is_file()
    assert catalog_import_worker.run(Client(), endpoint, job, tmp_path, threading.Event()) == first
    assert len(calls) == 2 and calls[0] == calls[1]
    value = json.loads(gzip.decompress(journal.read_bytes()))
    assert value['source_sha256'] == expected and value['records'][0]['sku'] == 'TEST-1'
