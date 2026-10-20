"""Read the old POD source database on the initiating computer only."""
import hashlib
import base64
import gzip
import json
import os
import sqlite3
from pathlib import Path
import re
import sys
import uuid


def _submit(client, endpoint: str, job: dict, journal: Path, lost) -> dict:
    packed = journal.read_bytes()
    if not packed or len(packed) > 25 * 1024**2:
        raise ValueError('本机 POD 历史回执无效')
    if lost.is_set():
        raise ValueError('任务租约已失效，停止提交 POD 历史结果')
    return client.request('POST', endpoint + '/pod-import-result',
                          {'sha256': hashlib.sha256(packed).hexdigest(),
                           'content': base64.b64encode(packed).decode('ascii')},
                          extra_headers={'X-Worker-Attempt': str(job['attempts'])})[0]


def run(client, endpoint: str, job: dict, root: Path, lost) -> dict:
    if (job.get('type') != 'catalog_pod_import'
            or not re.fullmatch(r'[a-f0-9]{32}', job.get('id', ''))
            or type(job.get('payload', {}).get('dry_run')) is not bool):
        raise ValueError('POD 历史任务无效')
    work = root / 'pod-import-tasks' / job['id']
    journal = work / 'import-result.json.gz'
    if not job['payload']['dry_run'] and journal.is_file():
        return _submit(client, endpoint, job, journal, lost)
    runtime = Path(__file__).parent / 'runtime'
    sys.path.insert(0, str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2] / 'backend'))
    from erp.core.config import get_settings
    from erp.services.catalog import inspect_pod_database
    requested = str(job.get('payload', {}).get('source_database') or '').strip()
    if requested:
        source = Path(requested)
    else:
        candidates = [
            get_settings().legacy_composer_root / 'data' / 'product_library.db',
            Path('D:/Desktop/指纹科技图片合成/data/product_library.db'),
            Path.home() / 'Desktop' / '指纹科技图片合成' / 'data' / 'product_library.db',
        ]
        source = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
    source = source.expanduser().resolve()
    if lost.is_set():
        raise ValueError('任务租约已失效，停止读取旧 POD 数据库')
    report = inspect_pod_database(source)
    connection = sqlite3.connect(f'file:{source.as_posix()}?mode=ro', uri=True)
    try:
        identity = {}
        for table in ('products', 'product_variants', 'product_oss_assets'):
            if lost.is_set():
                raise ValueError('任务租约已失效，停止核对旧 POD 数据库')
            ids = [str(row[0]) for row in connection.execute('select id from ' + table)]
            identity[table] = hashlib.sha256('\n'.join(sorted(ids)).encode()).hexdigest()
    finally:
        connection.close()
    report = {**report, 'identity_sha256': identity}
    if job['payload']['dry_run']:
        return report
    work.mkdir(parents=True, exist_ok=True)
    if not journal.exists():
        with source.open('rb') as stream:
            source_digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        connection = sqlite3.connect(f'file:{source.as_posix()}?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            categories = {str(row['id']): str(row['name']) for row in connection.execute('select id,name from product_categories')}
            versions = {str(row['id']): dict(row) for row in connection.execute('select * from product_versions')}
            entries = []
            for index, raw in enumerate(connection.execute('select * from products order by created_at,id'), 1):
                if lost.is_set():
                    raise ValueError('任务租约已失效，停止读取旧 POD 数据库')
                product = dict(raw)
                version = versions.get(str(product.get('current_version_id') or ''), {})
                variants = [dict(row) for row in connection.execute(
                    'select * from product_variants where product_id=? order by variant_no,id', (product['id'],))]
                media = []
                if version:
                    for row in connection.execute('select * from product_oss_assets where version_id=?', (version['id'],)):
                        item = dict(row)
                        item['name'] = Path(str(item.pop('local_path', '') or item.get('oss_key') or item['id'])).name
                        media.append(item)
                    try:
                        archived = json.loads(str(version.get('assets_json') or '{}')).get('oss_assets') or []
                    except (ValueError, AttributeError):
                        archived = []
                    order = {str(row.get('asset_id') or ''): position for position, row in enumerate(archived)
                             if isinstance(row, dict)}
                    media.sort(key=lambda row: (order.get(str(row.get('asset_id')), 1_000_000),
                                                str(row.get('asset_id')), str(row['id'])))
                entries.append({'product': product, 'category': categories.get(str(product.get('category_id')),
                                                                              str(product.get('category_id') or '')),
                                'version': version, 'variants': variants, 'media': media})
                if index % 100 == 0:
                    client.request('POST', endpoint + '/heartbeat', {},
                                   extra_headers={'X-Worker-Attempt': str(job['attempts'])})
        finally:
            connection.close()
        with source.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != source_digest:
                raise ValueError('POD 源数据库读取期间发生变化，拒绝回传')
        value = {'schema': 1, 'source_sha256': source_digest, 'report': report, 'entries': entries}
        packed = gzip.compress(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'), mtime=0)
        if not packed or len(packed) > 25 * 1024**2:
            raise ValueError('POD 历史结果超过安全传输上限')
        temporary = work / (uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_bytes(packed)
            os.replace(temporary, journal)
        finally:
            temporary.unlink(missing_ok=True)
    return _submit(client, endpoint, job, journal, lost)
