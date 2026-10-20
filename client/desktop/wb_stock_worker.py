"""Set and verify WB stock on the initiating PC, with a write checkpoint."""
import hashlib
import json
import re
import time

import requests

from composition_worker import atomic_json
from wb_reconcile_worker import find_cards_exact


class StockOutcomeUnknown(ValueError):
    code = 'wb_stock_outcome_unknown'


class StockRateLimited(ValueError):
    code = 'wb_stock_rate_limited'


def run(client, endpoint, job, root, lost):
    if not re.fullmatch(r'[a-f0-9]{32}', job['id']):
        raise ValueError('WB 库存任务编号无效')
    headers = {'X-Worker-Attempt': str(job['attempts'])}
    snapshot = client.request('POST', endpoint + '/wb-stock-snapshot', {}, extra_headers=headers)[0]
    rows = snapshot['rows']
    warehouse = int(snapshot['warehouse_id'])
    amount = int(snapshot['amount'])
    if not rows or len(rows) > 1000 or warehouse <= 0 or amount < 0:
        raise ValueError('WB 库存任务参数无效')
    folder = root / 'wb-stock-tasks' / job['id']
    folder.mkdir(parents=True, exist_ok=True)
    checkpoint = folder / 'write.json'
    previous = json.loads(checkpoint.read_text(encoding='utf-8')) if checkpoint.exists() else None
    # A retry uses the exact identifiers already verified before the original write.
    if previous:
        intent = previous.get('intent') or {}
        items = intent.get('items') or []
        expected = {row['id']: str(row['nm_id']) for row in rows}
        if (intent.get('warehouse_id') != warehouse or intent.get('amount') != amount
            or len(items) != len(rows)
            or {item.get('id'): str(item.get('nm_id')) for item in items} != expected
            or any(not isinstance(item.get('chrt_id'), int) or item['chrt_id'] <= 0 for item in items)):
            raise StockOutcomeUnknown('原库存检查点与当前商品不符，拒绝重发')
    else:
        cards = find_cards_exact([row['offer_id'] for row in rows], snapshot['config'], lost)
        items = []
        for row in rows:
            card = cards.get(row['offer_id'])
            if not card or str(card.get('nmID') or '') != str(row['nm_id']):
                raise ValueError('WB 商品编号已变化，停止库存写入')
            sizes = card.get('sizes') or []
            if len(sizes) != 1:
                raise ValueError('无法唯一确定 WB 库存规格')
            chrt_id = int(sizes[0].get('chrtID') or sizes[0].get('chrtId') or 0)
            if chrt_id <= 0:
                raise ValueError('WB 库存规格 ID 缺失')
            items.append({'id': row['id'], 'nm_id': str(row['nm_id']), 'chrt_id': chrt_id})
    if len({item['chrt_id'] for item in items}) != len(items):
        raise ValueError('WB 库存规格 ID 重复，停止写入')
    token_values = (snapshot['config'].get('store') or {}).get('secrets') or {}
    token = next((str(token_values.get(key) or '').strip() for key in ('token', 'api_key', 'wildberries_token')
                  if str(token_values.get(key) or '').strip()), '')
    if not token:
        raise ValueError('WB 店铺缺少 Marketplace Token')
    intent = {'warehouse_id': warehouse, 'amount': amount, 'items': items}
    digest = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()
    if previous and previous.get('digest') != digest:
        raise StockOutcomeUnknown('同一任务的库存参数已变化，拒绝重发')
    url = f'https://marketplace-api.wildberries.ru/api/v3/stocks/{warehouse}'
    with requests.Session() as http:
        http.headers.update({'Authorization': token})

        def readback():
            response = http.post(url, json={'chrtIds': [item['chrt_id'] for item in items]}, timeout=30)
            if response.status_code == 429:
                raise StockRateLimited('WB 库存查询限流；已停止自动重试，请人工核对平台库存后提交')
            response.raise_for_status()
            return {int(row['chrtId']): int(row['amount']) for row in response.json().get('stocks') or []}

        current = readback()
        wanted = {item['chrt_id']: amount for item in items}
        if current != wanted:
            if previous:
                raise StockOutcomeUnknown('上次库存写入结果与 WB 当前库存不一致，请核对后人工处理')
            if lost.is_set():
                raise ValueError('任务连接已失效，尚未写入库存')
            atomic_json(checkpoint, {'state': 'pending', 'digest': digest, 'intent': intent})
            try:
                response = http.put(url, json={'stocks': [{'chrtId': item['chrt_id'], 'amount': amount}
                                                           for item in items]}, timeout=30)
            except requests.RequestException as error:
                raise StockOutcomeUnknown('WB 库存请求结果未知，请先查询实际库存，禁止自动重发') from error
            if response.status_code != 204:
                if response.status_code == 429:
                    raise StockRateLimited('WB 库存写入限流；已停止自动重试，请人工核对平台库存后提交')
                raise StockOutcomeUnknown(f'WB 库存请求未确认（HTTP {response.status_code}），请先核对平台')
            atomic_json(checkpoint, {'state': 'acknowledged', 'digest': digest, 'intent': intent})
            for attempt in range(4):
                if attempt:
                    time.sleep(2)
                current = readback()
                if current == wanted:
                    break
            if current != wanted:
                raise StockOutcomeUnknown('WB 已接收库存请求，但回读尚未达到目标值；禁止自动重发')
        atomic_json(checkpoint, {'state': 'done', 'digest': digest, 'intent': intent})
    if lost.is_set():
        raise ValueError('任务连接已失效，库存已核实；等待同一任务恢复回传')
    return {'verified': True, 'warehouse_id': warehouse, 'amount': amount, 'items': items}
