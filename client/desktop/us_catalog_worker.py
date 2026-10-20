"""Read verified US S2B catalog on the originating PC and journal before upload."""
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sys


def fetch(remote, request, lost):
    if request.get('market') != 'US':
        raise ValueError('美国货盘任务地区不匹配')
    if request['kind'] == 'freight':
        if lost.is_set(): raise ValueError('美国运费租约已失效')
        return {'market': 'US', 'kind': 'freight', 'fingerprint': request['fingerprint'],
                'options': remote.logistics_calculation(request['params'])}
    if request['kind'] == 'detail':
        if lost.is_set(): raise ValueError('美国货盘租约已失效')
        return {'market': 'US', 'kind': 'detail', 'product_key': request['product_key'],
                'detail': remote.product_type(int(request['source_id']), country='US')}
    if request['kind'] != 'sync':
        raise ValueError('美国货盘任务类型无效')
    from erp.domains.pod.supplier_catalog import _product_items, _product_total
    products = {}
    categories = remote.product_categories()
    for page in range(1, 1001):
        if lost.is_set(): raise ValueError('美国货盘租约已失效')
        payload = remote.product_types(page, 100, country='US')
        items = _product_items(payload)
        for item in items:
            identity = str(item.get('id') or '')
            if not identity.isdigit() or str(item.get('produce_country') or '').upper() != 'US':
                raise ValueError('美国货盘商品缺少可核实的生产国家或编号')
            products[identity] = {**item, 'supplier': 's2b', 'source_id': identity, 'catalog_key': f's2b:{identity}'}
        count = int(payload.get('_page_item_count', len(items)))
        total = _product_total(payload)
        if count < 100 or (total is not None and len(products) >= total):
            break
    else:
        raise ValueError('美国货盘超过分页上限，未上传不完整列表')
    return {'market': 'US', 'kind': 'sync', 'products': list(products.values()), 'categories': categories}


def run(client, endpoint, job, root: Path, lost):
    if job.get('type') not in {'pod_supplier_us_catalog_sync', 'pod_supplier_us_freight_quote'} or not re.fullmatch('[a-f0-9]{32}', job.get('id', '')):
        raise ValueError('美国货盘任务无效')
    headers = {'X-Worker-Attempt': str(job['attempts'])}
    work = root / 'supplier-us-catalog-tasks' / job['id']
    work.mkdir(parents=True, exist_ok=True)
    journal = work / 'result.json.gz'
    if not journal.exists():
        snapshot = client.request('GET', endpoint + '/supplier-catalog-snapshot', extra_headers=headers)[0]
        request = snapshot.get('request')
        if not isinstance(request, dict) or request != job.get('payload') or set(snapshot.get('sources') or {}) != {'s2b'}:
            raise ValueError('美国货盘任务快照不匹配')
        source = snapshot['sources']['s2b']
        if source.get('error'): raise ValueError('S2B 美国货盘连接未配置或已停用')
        connection = source.get('connection')
        if not isinstance(connection, dict) or connection.get('provider') != 's2b':
            raise ValueError('美国货盘供应商不匹配')
        runtime = Path(__file__).parent / 'runtime'
        sys.path.insert(0, str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2] / 'backend'))
        from erp.domains.pod.integrations.common import SupplierConnectionConfig
        from erp.domains.pod.integrations.s2b import S2BClient
        with S2BClient(SupplierConnectionConfig(**connection)) as remote:
            result = fetch(remote, request, lost)
        packed = gzip.compress(json.dumps(result, ensure_ascii=False, default=str, separators=(',', ':')).encode(), mtime=0)
        if len(packed) > 10_000_000: raise ValueError('美国货盘结果超过上传限制')
        temporary = journal.with_suffix('.tmp')
        try:
            temporary.write_bytes(packed)
            os.replace(temporary, journal)
        finally:
            temporary.unlink(missing_ok=True)
    if lost.is_set(): raise ValueError('美国货盘租约已失效')
    packed = journal.read_bytes()
    return client.request('POST', endpoint + '/supplier-catalog-result', {
        'sha256': hashlib.sha256(packed).hexdigest(), 'content': base64.b64encode(packed).decode(),
    }, extra_headers=headers)[0]
