"""Consolidate validated personal-computer workbook rows in the shared catalog."""
import math
import re
from urllib.parse import urlparse, unquote

from sqlalchemy.orm import Session

from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps, loads
from erp.models import CatalogImportRun, IntegrationConnection
from erp.services.catalog import IMAGE_ROLES, _http_url, apply_self_selected_records


def accept_workbook_rows(session: Session, run: CatalogImportRun, value: dict) -> dict:
    if run.kind not in {'self_selected', 'self_selected_bundle'} or run.dry_run:
        raise ConflictError('这不是自主上架正式 Excel 导入')
    bundle = run.kind == 'self_selected_bundle'
    required_keys = {'schema', 'source_sha256', 'records', 'errors', 'images'} if bundle else {'schema', 'source_sha256', 'records', 'errors'}
    if not isinstance(value, dict) or set(value) != required_keys or value['schema'] != (2 if bundle else 1):
        raise ConflictError('自主上架导入结果格式无效')
    records, errors = value['records'], value['errors']
    if (not isinstance(records, list) or len(records) > 100_000
            or not isinstance(errors, list) or len(errors) > 5000):
        raise ConflictError('自主上架导入行数或错误清单无效')
    required = {'row', 'group_code', 'sku', 'name', 'source_url', 'title_ru',
                'purchase_price_cny', 'package_length_mm', 'package_width_mm',
                'package_height_mm', 'package_weight_g', 'main_oss_url', 'additional_oss_urls'}
    for error in errors:
        if (not isinstance(error, dict) or set(error) != {'row', 'field', 'message'}
                or type(error['row']) is not int or error['row'] < 1
                or not isinstance(error['field'], str) or not isinstance(error['message'], str)):
            raise ConflictError('自主上架导入错误记录无效')
    invalid_rows = {error['row'] for error in errors}
    seen = set()
    for record in records:
        if not isinstance(record, dict) or not required.issubset(record):
            raise ConflictError('自主上架导入产品字段缺失')
        if type(record['row']) is not int or record['row'] < 1:
            raise ConflictError('自主上架导入行号无效')
        if record['row'] in invalid_rows:
            continue
        for key in ('group_code', 'sku', 'name', 'source_url', 'title_ru', 'main_oss_url'):
            if not isinstance(record[key], str):
                raise ConflictError('自主上架导入文本字段无效')
        if (not record['sku'].strip() or record['group_code'] != record['sku'].strip()
                or not record['name'].strip() or not record['title_ru'].strip()
                or not _http_url(record['source_url'])
                or (record['main_oss_url'] and not _http_url(record['main_oss_url']))):
            raise ConflictError('自主上架导入产品或链接无效')
        sku = record['sku'].casefold()
        if sku in seen:
            # The original parser reports duplicate SKU as a row error. The
            # client must include that error before either group is imported.
            if not any(item.get('row') == record['row'] and item.get('field') == 'sku'
                       for item in errors if isinstance(item, dict)):
                raise ConflictError('自主上架导入重复 SKU 缺少原始错误记录')
        seen.add(sku)
        for key in ('purchase_price_cny', 'package_length_mm', 'package_width_mm',
                    'package_height_mm', 'package_weight_g'):
            number = record[key]
            if not isinstance(number, (int, float)) or isinstance(number, bool) or not math.isfinite(number) or number <= 0:
                if not any(item.get('row') == record['row'] and item.get('field') == key
                           for item in errors if isinstance(item, dict)):
                    raise ConflictError('自主上架导入数值字段无效')
        urls = record['additional_oss_urls']
        if (not isinstance(urls, list) or not urls or
                any(not isinstance(url, str) or not _http_url(url) for url in urls)):
            if not any(item.get('row') == record['row'] and item.get('field') == 'additional_oss_urls'
                       for item in errors if isinstance(item, dict)):
                raise ConflictError('自主上架导入附图链接无效')
    groups = {str(row['group_code']) for row in records}
    images = value['images'] if bundle else []
    if bundle:
        connection = session.get(IntegrationConnection, loads(run.report_json, {}).get('oss_connection_id'))
        if not connection or connection.provider != 'oss' or connection.name != 'OZON 自动上架 OSS':
            raise ConflictError('正式导入 OSS 配置不匹配')
        config = loads(connection.config_json, {})
        prefix = str(config.get('prefix') or 'hicustom-products').strip('/')
        endpoint = str(connection.base_url or config.get('endpoint') or f"oss-{config.get('region')}.aliyuncs.com")
        if '://' not in endpoint:
            endpoint = 'https://' + endpoint
        endpoint_host = urlparse(endpoint).hostname
        expected_host = f"{config.get('bucket')}.{endpoint_host}".casefold()
        if not isinstance(images, list) or len(images) > 5000:
            raise ConflictError('自主上架图片清单过大')
        seen_images = set()
        sku_groups = {(str(row['group_code']), str(row['sku'])) for row in records}
        for image in images:
            if not isinstance(image, dict) or set(image) != {'row', 'group_code', 'sku', 'relative_path', 'role', 'position', 'oss_url', 'sha256', 'size_bytes'}:
                raise ConflictError('自主上架图片字段无效')
            if (type(image['row']) is not int or image['row'] < 2
                    or type(image['position']) is not int or image['position'] < 0
                    or image['role'] not in IMAGE_ROLES
                    or not isinstance(image['group_code'], str) or image['group_code'] not in groups
                    or not isinstance(image['sku'], str)
                    or (image['sku'] and (image['group_code'], image['sku']) not in sku_groups)
                    or not isinstance(image['relative_path'], str) or not image['relative_path']
                    or not re.fullmatch(r'[a-f0-9]{64}', str(image['sha256']))
                    or type(image['size_bytes']) is not int or image['size_bytes'] <= 0):
                raise ConflictError('自主上架图片关联或摘要无效')
            suffix = '.' + image['relative_path'].rsplit('.', 1)[-1].casefold()
            url = urlparse(str(image['oss_url']))
            key = unquote(url.path.lstrip('/'))
            expected_key = f"{prefix}/self-selected-import/{image['sha256'][:2]}/{image['sha256']}{suffix}"
            if url.scheme != 'https' or (url.hostname or '').casefold() != expected_host or key != expected_key:
                raise ConflictError('自主上架图片不属于指定 OSS 对象')
            identity = (image['group_code'], image['sku'], image['role'], image['position'], image['relative_path'])
            if identity in seen_images:
                raise ConflictError('自主上架图片清单重复')
            seen_images.add(identity)
    counts = {'rows': len(records), 'product_groups': len(groups), 'local_images': len(images),
              'errors': len(errors)}
    run.errors_json = dumps(errors)
    invalid_groups = {str(row['group_code']) for row in records if row['row'] in invalid_rows}
    if invalid_groups:
        records = [row for row in records if str(row['group_code']) not in invalid_groups]
        counts['rejected_groups'] = len(invalid_groups)
        counts['accepted_groups'] = len({str(row['group_code']) for row in records})
    if not records:
        run.status = 'validation_failed'
        run.counts_json = dumps(counts)
        return {'inserted': 0, 'updated': 0, 'rejected_groups': counts.get('rejected_groups', 0),
                'status': run.status}
    images = [image for image in images if image['group_code'] not in invalid_groups]
    result = apply_self_selected_records(session, run, records, errors, counts, images)
    return {**result, 'status': run.status}
