"""Read-only Ozon choices on the initiating PC; no catalog database copied."""
from pathlib import Path
import sys
from types import SimpleNamespace


def run(client, endpoint, job, root, lost):
    kind = job['payload'].get('kind')
    if kind not in {'warehouses', 'promotions', 'category_attributes','category_tree','listing_status'} or lost.is_set():
        raise ValueError('店铺资料读取任务无效或电脑已断线')
    config = client.request('GET', endpoint + '/catalog-ozon-options-snapshot',
                            extra_headers={'X-Worker-Attempt': str(job['attempts'])})[0]
    if config.get('store_id') != job['payload'].get('store_id') or config.get('platform') != 'ozon':
        raise ValueError('店铺资料与任务不一致')
    runtime = Path(__file__).parent / 'runtime'
    sys.path.insert(0, str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2] / 'backend'))
    from erp.integrations.ozon import OzonSellerAdapter, OzonApiError
    try:
        cache = SimpleNamespace(get_category_attributes_cache=lambda *args: None, save_category_attributes_cache=lambda *args: None)
        adapter=OzonSellerAdapter(config, repository=cache)
        if kind=='listing_status':
            import json
            from datetime import datetime, timezone
            from erp_desktop import protect
            receipt=Path(root)/'ozon-stock-scans'/job['id']/'read-result.dpapi'
            items=None
            if receipt.exists():
                saved=json.loads(protect(receipt.read_bytes(),decrypt=True))
                if saved['targets']==job['payload']['targets'] and 0<=(datetime.now(timezone.utc)-datetime.fromisoformat(saved['observed_at'])).total_seconds()<240:
                    items=saved['items']
            if items is None:
                items=scan_items(adapter,job['payload']['targets'],lost)
                receipt.parent.mkdir(parents=True,exist_ok=True)
                temporary=receipt.with_suffix('.partial')
                temporary.write_bytes(protect(json.dumps({'targets':job['payload']['targets'],'items':items,'observed_at':datetime.now(timezone.utc).isoformat()}).encode()))
                temporary.replace(receipt)
        else:
            items = getattr(adapter, kind)(**job['payload'].get('params', {}))
    except OzonApiError:
        raise ValueError('Ozon 资料读取失败，请检查店铺授权或平台状态') from None
    if lost.is_set(): raise ValueError('电脑连接中断，请保留原读取任务')
    return {'store_id': config['store_id'], 'kind': kind, 'items': items}


def scan_items(adapter,targets,lost):
    from datetime import datetime, timezone
    offers=list(dict.fromkeys(t['offer_id'] for t in targets))
    items=[]
    for offset in range(0,len(offers),1000):
        if lost.is_set():raise ValueError('电脑已断线')
        result=adapter.post('/v3/product/info/list',{'offer_id':offers[offset:offset+1000]})
        rows=result.get('items')
        if not isinstance(rows,list):raise ValueError('Ozon商品状态响应格式无效')
        observed=datetime.now(timezone.utc).isoformat()
        for row in rows:
            items.append({key:row[key] for key in ('offer_id','id','is_archived','is_autoarchived','statuses','errors') if key in row}|{'observed_at':observed})
    return items
