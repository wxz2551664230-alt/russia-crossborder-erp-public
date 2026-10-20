from pathlib import Path
from types import SimpleNamespace
import hashlib

import alibabacloud_oss_v2 as oss

from openpyxl import Workbook
from sqlalchemy import select

from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps, loads
from erp.models import CatalogImportRun, CatalogProduct, IntegrationConnection
from erp.services.catalog import parse_self_selected_workbook
from erp.services.catalog_personal_import import accept_workbook_rows
from erp.domains.pod.integrations.oss import PodOssService


def _rows(path: Path, *, valid: bool = True):
    book = Workbook()
    sheet = book.active
    sheet.append(['来源链接', '中文名称', 'SKU', '规格型号', '颜色', '尺码', '拿货价',
                  '长(cm)', '宽(cm)', '高(cm)', '重量(g)', '主图链接', '附图链接', '商品名称', '备注'])
    sheet.append(['https://detail.1688.com/item/1' if valid else 'bad-url', '测试包', 'TEST-BAG',
                  '蓝色 M', '蓝色', 'M', 12.5, 20, 10, 4, 300,
                  'https://oss.test/main.jpg', 'https://oss.test/extra.jpg', 'Чехол', '内部资料'])
    book.save(path)
    return parse_self_selected_workbook(path)


def _run(db, path, records, errors):
    run = CatalogImportRun(kind='self_selected', source_path=str(path), dry_run=False,
                           report_json=dumps({'ozon_category': {'store_id': 'test',
                               'description_category_id': 10, 'type_id': 20, 'display': '箱包 / 收纳包'}}))
    db.add(run)
    db.flush()
    result = accept_workbook_rows(db, run, {'schema': 1, 'source_sha256': 'a' * 64,
                                           'records': records, 'errors': errors})
    db.commit()
    return run, result


def test_personal_import_merge_preserves_original_fields_and_updates_existing(tmp_path):
    path = tmp_path / 'bag.xlsx'
    records, errors = _rows(path)
    assert not errors
    with SessionLocal() as db:
        run, result = _run(db, path, records, errors)
        assert result == {'inserted': 1, 'updated': 0, 'rejected_groups': 0, 'status': 'completed'}
        product = db.scalar(select(CatalogProduct).where(CatalogProduct.group_code == 'TEST-BAG'))
        assert product.notes == '内部资料'
        assert product.source_url == 'https://detail.1688.com/item/1'
        assert product.package_length_mm == 200
        assert product.description_category_id == 10
        assert len(product.variants) == 1
        assert product.variants[0].specification == '蓝色 M'
        assert len(product.asset_links) == 2
        assert all(not link.asset.local_path for link in product.asset_links)
        identity = product.id
        _, second = _run(db, path, records, errors)
        assert second['inserted'] == 0 and second['updated'] == 1
        assert db.scalar(select(CatalogProduct).where(CatalogProduct.group_code == 'TEST-BAG')).id == identity
        assert len(db.scalar(select(CatalogProduct).where(CatalogProduct.id == identity)).asset_links) == 2


def test_personal_import_merge_rejects_invalid_group_without_writes(tmp_path):
    path = tmp_path / 'invalid.xlsx'
    records, errors = _rows(path, valid=False)
    assert errors
    with SessionLocal() as db:
        run, result = _run(db, path, records, errors)
        assert result['status'] == 'validation_failed'
        assert loads(run.counts_json, {})['rejected_groups'] == 1
        assert db.scalar(select(CatalogProduct).where(CatalogProduct.group_code == 'TEST-BAG')) is None


def test_personal_import_merge_keeps_valid_group_when_other_group_fails(tmp_path):
    path = tmp_path / 'mixed.xlsx'
    good, no_errors = _rows(path)
    bad = dict(good[0])
    bad.update({'row': good[0]['row'] + 1, 'sku': 'BAD-BAG', 'group_code': 'BAD-BAG',
                'source_url': 'bad-url'})
    errors = [{'row': bad['row'], 'field': 'source_url', 'message': '货源链接格式不正确'}]
    with SessionLocal() as db:
        run, result = _run(db, path, [good[0], bad], errors)
        assert result == {'inserted': 1, 'updated': 0, 'rejected_groups': 1,
                          'status': 'completed_with_errors'}
        assert db.scalar(select(CatalogProduct).where(CatalogProduct.group_code == 'TEST-BAG'))
        assert db.scalar(select(CatalogProduct).where(CatalogProduct.group_code == 'BAD-BAG')) is None


def test_bundle_import_links_cloud_image_without_personal_path(tmp_path):
    path = tmp_path / 'bundle.zip'
    path.write_bytes(b'isolated-zip')
    records, errors = _rows(tmp_path / 'source.xlsx')
    digest = 'a' * 64
    key = f'ozon-auto-listing/self-selected-import/aa/{digest}.jpg'
    image = {'row': 2, 'group_code': 'TEST-BAG', 'sku': 'TEST-BAG',
             'relative_path': 'images/main.jpg', 'role': 'main', 'position': 0,
             'oss_url': f'https://bucket.oss-cn-test.aliyuncs.com/{key}',
             'sha256': digest, 'size_bytes': 123}
    with SessionLocal() as db:
        connection = IntegrationConnection(provider='oss', name='OZON 自动上架 OSS',
                                           base_url='oss-cn-test.aliyuncs.com',
                                           config_json=dumps({'bucket': 'bucket', 'region': 'cn-test',
                                                              'prefix': 'ozon-auto-listing'}), enabled=True)
        db.add(connection); db.flush()
        run = CatalogImportRun(kind='self_selected_bundle', source_path=str(path), dry_run=False,
                               report_json=dumps({'source_sha256': digest,
                                                  'oss_connection_id': connection.id,
                                                  'ozon_category': {'store_id': 'test',
                                                                    'description_category_id': 10,
                                                                    'type_id': 20, 'display': '箱包 / 收纳包'}}))
        db.add(run); db.flush()
        value = {'schema': 2, 'source_sha256': digest, 'records': records,
                 'errors': errors, 'images': [image]}
        assert accept_workbook_rows(db, run, value)['inserted'] == 1
        db.commit()
        product = db.scalar(select(CatalogProduct).where(CatalogProduct.group_code == 'TEST-BAG'))
        cloud = [link.asset for link in product.asset_links if link.asset.source == 'self_selected_bundle']
        assert len(cloud) == 1
        assert cloud[0].oss_url == image['oss_url']
        assert cloud[0].sha256 == digest and cloud[0].size_bytes == 123
        assert cloud[0].local_path == ''


def test_bundle_oss_upload_uses_content_key_and_checks_existing_object(tmp_path):
    path = tmp_path / 'main.jpg'
    path.write_bytes(b'isolated-image')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    service = PodOssService.__new__(PodOssService)
    service.bucket = 'bucket'
    service.prefix = 'ozon-auto-listing'
    service.endpoint = 'https://oss-cn-test.aliyuncs.com'
    calls = []

    class FakeClient:
        exists = False

        def head_object(self, request):
            calls.append(('head', request.key))
            if not self.exists:
                missing = oss.exceptions.ServiceError(status_code=404, code='NoSuchKey',
                                                      request_id='', message='', ec='',
                                                      timestamp='', request_target='')
                raise oss.exceptions.OperationError(name='HeadObject', error=missing)
            return SimpleNamespace(metadata={'sha256': digest}, content_length=path.stat().st_size)

        def put_object_from_file(self, request, filename):
            calls.append(('put', request.key))
            assert filename == str(path)
            self.exists = True

    service.client = FakeClient()
    first = service.upload_self_selected_asset(path, digest)
    second = service.upload_self_selected_asset(path, digest)
    assert first == second
    assert [kind for kind, _ in calls].count('put') == 1
    assert first['oss_key'].endswith('/' + digest + '.jpg')
