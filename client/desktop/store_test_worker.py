"""Read-only Ozon Seller connection check on the initiating computer."""
from pathlib import Path
import sys


def run(client, endpoint, job, root, lost):
    if lost.is_set():
        raise ValueError('执行电脑连接失效，店铺测试已停止')
    headers = {'X-Worker-Attempt': str(job['attempts'])}
    config = client.request('GET', endpoint + '/store-test-snapshot',
                            extra_headers=headers)[0]
    if config.get('store_id') != job['payload'].get('store_id') or config.get('platform') != 'ozon':
        raise ValueError('店铺测试快照与任务不一致')
    runtime = Path(__file__).parent / 'runtime'
    sys.path.insert(0, str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2] / 'backend'))
    from erp.integrations.ozon import OzonApiError, OzonSellerAdapter
    # The connectivity check does not use ProductRepository. Supplying an
    # inert object prevents its constructor from creating a local products DB.
    try:
        result = OzonSellerAdapter(config, repository=object()).test_connection()
    except OzonApiError:
        raise ValueError('Ozon Seller API 连接失败，请检查本店铺凭据和平台状态') from None
    if lost.is_set():
        raise ValueError('执行电脑连接失效，店铺测试结果尚未回传')
    return {'ok': True, 'store_id': config['store_id'],
            'category_roots': int(result['category_roots'])}
