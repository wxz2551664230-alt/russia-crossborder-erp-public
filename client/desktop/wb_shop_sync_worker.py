"""Read-only WB shop inventory/price reconciliation, with bounded batch requests."""
import hashlib
import base64
import json
import re
import time

import requests

from composition_worker import atomic_json


class SyncRateLimited(ValueError):
    code = 'wb_shop_sync_rate_limited'

    def __init__(self, seconds):
        self.seconds = min(86400, max(60, seconds))
        super().__init__(f'WB 店铺同步额度需要冷却，已停止请求；请至少等待 {self.seconds} 秒后人工同步')


def _raw(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def basic_token(token):
    try:
        raw = token.split('.')[1]
        value = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
        return value.get('acc') == 1 and value.get('t') is not True
    except (ValueError, IndexError, TypeError):
        return False


class Reader:
    def __init__(self, http, directory, lost, cooldown, basic=False, continuing=False, refresh=False):
        self.http, self.directory, self.lost, self.cooldown = http, directory, lost, cooldown
        self.last, self.basic = 0, basic
        self.continuing, self.refresh = continuing, refresh

    def read(self, method, url, body=None):
        if self.lost.is_set():
            raise ValueError('原电脑连接中断，停止 WB 同步请求')
        identity = hashlib.sha256(_raw([method, url, body])).hexdigest()
        path = self.directory / (identity[:32] + '.json')
        category = 'prices' if 'discounts-prices-api.' in url else 'content' if 'content-api.' in url else 'marketplace'
        # Freeze already read batches while the user continues an unfinished sync.
        ttl = float('inf') if self.continuing else 300
        if not self.refresh and path.exists() and time.time() - path.stat().st_mtime < ttl:
            return json.loads(path.read_text('utf-8'))
        gate = self.cooldown.parent / ('quota-' + category + '.json')
        if gate.exists():
            until = json.loads(gate.read_text('utf-8')).get('until', 0)
            if until > time.time():
                raise SyncRateLimited(int(until - time.time()) + 1)
        wait = max(0, self.last + 1.0 - time.monotonic())
        if wait and self.lost.wait(wait):
            raise ValueError('原电脑连接中断，停止 WB 同步请求')
        self.last = time.monotonic()
        response = self.http.request(method, url, **({'json': body} if body is not None else {}), timeout=60)
        if response.status_code == 429:
            delays = [60]
            for key in ['Retry-After', 'X-Ratelimit-Retry', 'X-Ratelimit-Reset']:
                try:
                    delays.append(int(response.headers.get(key, '0')))
                except ValueError:
                    pass
            error = SyncRateLimited(max(delays))
            atomic_json(self.cooldown, {'until': time.time() + error.seconds})
            raise error
        response.raise_for_status()
        value = response.json()
        if isinstance(value, dict) and value.get('error') is True:
            raise ValueError('WB 拒绝同步读取；请核对接口权限')
        reset = 901 if self.basic and category == 'prices' else 0
        try:
            if response.headers.get('X-Ratelimit-Remaining') == '0':
                reset = max(reset, int(response.headers.get('X-Ratelimit-Reset', '0')) + 1)
        except ValueError:
            pass
        if reset:
            atomic_json(gate, {'until': time.time() + min(86400, reset)})
        atomic_json(path, value)
        return value


def _cards(reader):
    cursor, previous, cards, seen = {'limit': 100}, None, [], {}
    for _ in range(500):
        value = reader.read('POST', 'https://content-api.wildberries.ru/content/v2/get/cards/list',
                            {'settings': {'sort': {'ascending': False}, 'filter': {'withPhoto': -1}, 'cursor': cursor}})
        page = value.get('cards')
        if not isinstance(page, list):
            raise ValueError('WB 商品卡响应不完整，未更新本轮商品身份')
        for card in page:
            if not isinstance(card, dict) or not isinstance(card.get('vendorCode'), str) or not card['vendorCode'].strip():
                raise ValueError('WB 商品卡缺少卖家货号')
            identity = int(card.get('nmID') or 0)
            if identity <= 0:
                raise ValueError('WB 商品卡编号无效')
            sizes = [{'chrtID': int(s.get('chrtID') or s.get('chrtId') or 0),
                      'techSize': str(s.get('techSize') or '')} for s in card.get('sizes') or []]
            if any(s['chrtID'] <= 0 for s in sizes):
                sizes = []
            item = {'vendorCode': card['vendorCode'].strip(), 'nmID': identity, 'sizes': sizes}
            if identity in seen and seen[identity] != item:
                raise ValueError('WB 扫描期间商品卡发生变化，请人工重新同步')
            if identity not in seen:
                cards.append(item); seen[identity] = item
        if len(page) < 100:
            return cards
        following = value.get('cursor') or {}
        next_pair = (following.get('updatedAt'), following.get('nmID'))
        if not all(next_pair) or next_pair == previous:
            raise ValueError('WB 扫描游标缺失或未前进，拒绝使用不完整商品列表')
        previous = next_pair
        cursor = {'limit': 100, 'updatedAt': next_pair[0], 'nmID': next_pair[1]}
    raise ValueError('WB 商品卡超过500页；本轮未完成，不释放商品占用')


def _related(cards, snapshot):
    skus = {str(row[key]).casefold() for row in snapshot['candidates'] for key in ['sku', 'product_sku'] if row.get(key)}
    pattern = re.compile('|'.join(re.escape(sku) for sku in sorted(skus, key=len, reverse=True)), re.I) if skus else None
    known = snapshot.get('known') or []
    offers = {row['offer_id'].casefold() for row in known}
    identities = {int(row['nm_id']) for row in known if str(row.get('nm_id', '')).isdigit()}
    return [card for card in cards if card['vendorCode'].casefold() in offers or card['nmID'] in identities
            or pattern and pattern.search(card['vendorCode'])]


def _stocks(reader, cards, result=None):
    result = result if result is not None else {}
    warehouses = reader.read('GET', 'https://marketplace-api.wildberries.ru/api/v3/warehouses')
    if not isinstance(warehouses, list):
        raise ValueError('WB 仓库列表格式无效')
    warehouse_ids = list(dict.fromkeys(int(row['id']) for row in warehouses if row.get('isDeleting') is not True and row.get('id')))
    ids = sorted({size['chrtID'] for card in cards for size in card['sizes']})
    amounts = {identity: 0 for identity in ids}
    checked = {identity: 0 for identity in ids}
    if not warehouse_ids:
        result.update({i: {'state': 'verified', 'amount': 0} for i in ids})
    for warehouse in warehouse_ids:
        for start in range(0, len(ids), 1000):
            batch = ids[start:start + 1000]
            value = reader.read('POST', f'https://marketplace-api.wildberries.ru/api/v3/stocks/{warehouse}', {'chrtIds': batch})
            rows = value.get('stocks') if isinstance(value, dict) else None
            if not isinstance(rows, list):
                raise ValueError('WB 库存响应格式无效')
            returned = {}
            for row in rows:
                identity = int(row.get('chrtId') or row.get('chrtID') or 0)
                amount = row.get('amount')
                if identity not in batch or identity in returned or type(amount) is not int or amount < 0:
                    raise ValueError('WB 返回重复或无效库存规格')
                returned[identity] = amount
            # WB omits sizes with no inventory entry. A valid successful response
            # completes this batch for every requested ID, including omitted IDs.
            # Do not count anything if the request fails or the body is malformed.
            for identity in batch:
                checked[identity] += 1
                amounts[identity] += returned.get(identity, 0)
                if checked[identity] == len(warehouse_ids):
                    result[identity] = {'state': 'verified', 'amount': amounts[identity]}
                elif amounts[identity] > 0:
                    result[identity] = {'state': 'present', 'amount': amounts[identity]}
                else:
                    result[identity] = {'state': 'unknown', 'amount': None}
    return result


def _prices(reader, cards, result=None):
    ids = sorted({card['nmID'] for card in cards})
    result = result if result is not None else {}
    for start in range(0, len(ids), 1000):
        batch = ids[start:start + 1000]
        value = reader.read('POST', 'https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter', {'nmList': batch})
        goods = (value.get('data') or {}).get('listGoods') if isinstance(value, dict) else None
        if not isinstance(goods, list):
            raise ValueError('WB 价格响应格式无效')
        for item in goods:
            identity = int(item.get('nmID') or 0)
            if identity not in batch or identity in result:
                raise ValueError('WB 返回重复或无关价格商品')
            sizes = item.get('sizes') or []
            values = [size.get('price') for size in sizes]
            discount = item.get('discount')
            if values and all(type(v) in (int, float) and 0 < v < 1e10 for v in values) and len(set(values)) == 1 and type(discount) is int and 0 <= discount <= 99:
                result[identity] = {'state': 'verified', 'price': values[0], 'discount': discount,
                                    'currency': str(item.get('currencyIso4217') or '')}
            else:
                result[identity] = {'state': 'missing', 'price': None, 'discount': None, 'currency': ''}
        for identity in batch:
            result.setdefault(identity, {'state': 'missing', 'price': None, 'discount': None, 'currency': ''})
    return result


def run(client, endpoint, job, root, lost, snapshot):
    if snapshot.get('shop_sync') != 1 or not isinstance(snapshot.get('candidates'), list):
        raise ValueError('店铺同步协议不受支持；未发起 WB 请求')
    token = snapshot['config']['store']['secrets']['token']
    binding = hashlib.sha256(_raw([snapshot['store_id'], token])).hexdigest()[:32]
    folder = root / 'wb-shop-sync-tasks' / job['id']; folder.mkdir(parents=True, exist_ok=True)
    cache = root / 'wb-shop-sync-cache' / binding; cache.mkdir(parents=True, exist_ok=True)
    cooldown = cache / 'cooldown.json'
    continuation = cache / 'continuation.json'
    stock_receipt = cache / 'stock-receipt-v2.json'
    candidate_hash = hashlib.sha256(_raw(sorted((str(r.get('id', '')), str(r.get('sku', '')), str(r.get('product_sku', ''))) for r in snapshot['candidates']))).hexdigest()
    continuing = False
    stock_finished_before = False
    if continuation.exists() and not snapshot.get('refresh'):
        previous = json.loads(continuation.read_text('utf-8'))
        continuing = previous.get('candidate_hash') == candidate_hash
        stock_finished_before = continuing and previous.get('stock_finished') is True
    headers = {'X-Worker-Attempt': str(job['attempts'])}
    warnings, retry_seconds, cards_read = [], 0, 0
    # The task journal survives uploads failing; no platform re-read is needed.
    receipt = folder / ('receipt-' + binding + '.json')
    if receipt.exists():
        saved = json.loads(receipt.read_text('utf-8'))
        cards, warnings, retry_seconds, cards_read = (saved[k] for k in ['cards', 'warnings', 'retry_seconds', 'cards_read'])
    else:
        if cooldown.exists():
            until=json.loads(cooldown.read_text('utf-8')).get('until',0)
            if until>time.time():raise SyncRateLimited(int(until-time.time())+1)
        def throttle(error):
            nonlocal retry_seconds
            retry_seconds = error.seconds
            client.request('POST', endpoint + '/wb-shop-sync-throttle', {'retry_seconds': error.seconds}, extra_headers=headers)
        with requests.Session() as http:
            http.trust_env = False
            http.headers.update({'Authorization': token})
            # A manual forced refresh bypasses the shared cache, but not this task's journal.
            basic = basic_token(token)
            reader = Reader(http, cache, lost, cooldown, basic, continuing, bool(snapshot.get('refresh')))
            try:
                complete=cache/'complete-cards.json'
                if not snapshot.get('refresh') and complete.exists() and (continuing or time.time()-complete.stat().st_mtime<300):
                    all_cards=json.loads(complete.read_text('utf-8'))
                else:
                    all_cards = _cards(Reader(http,folder,lost,cooldown)) if snapshot['candidates'] else []
                    atomic_json(complete,all_cards)
                reader.last=time.monotonic()
            except SyncRateLimited as error:
                throttle(error)
                raise
            cards_read = len(all_cards)
            cards = _related(all_cards, snapshot)
            for card in cards:
                card['stock'] = {'state': 'unknown', 'amount': None}
                card['price'] = {'state': 'unknown', 'price': None, 'discount': None, 'currency': ''}
            if cards:
                amounts, prices = {}, {}
                stock_finished = False
                try:
                    if stock_finished_before and stock_receipt.exists():
                        amounts = {int(k):v for k,v in json.loads(stock_receipt.read_text('utf-8')).items()}
                    else:
                        _stocks(reader, cards, amounts)
                        atomic_json(stock_receipt, amounts)
                    stock_finished = True
                except SyncRateLimited as error:
                    throttle(error); warnings.append(str(error))
                except (requests.RequestException, ValueError):
                    warnings.append('库存未读取成功，未将缺失结果当作零库存；请核对 Marketplace 权限或稍后人工同步')
                for card in cards:
                    values = [amounts.get(s['chrtID'], {'state': 'unknown', 'amount': None}) for s in card['sizes']]
                    if values and all(v['state'] == 'verified' for v in values):
                        card['stock'] = {'state': 'verified', 'amount': sum(v['amount'] for v in values)}
                    elif any((v['amount'] or 0) > 0 for v in values):
                        card['stock'] = {'state': 'present', 'amount': sum(v['amount'] or 0 for v in values)}
                if not retry_seconds:
                    try:
                        _prices(reader, cards, prices)
                    except SyncRateLimited as error:
                        throttle(error); warnings.append(str(error))
                    except (requests.RequestException, ValueError):
                        warnings.append('价格折扣未读取成功，未将读取失败当作未设置；请核对价格权限或稍后人工同步')
                    for card in cards:
                        if card['nmID'] in prices:
                            card['price'] = prices[card['nmID']]
                if basic and retry_seconds:
                    warnings.append('当前基础 Token 的价格接口每15分钟仅1次；已保留读完的商品、库存和价格批次，冷却后人工继续，只补未读批次。强制刷新会重新读取')
                if warnings:
                    atomic_json(continuation, {'candidate_hash': candidate_hash, 'stock_finished': stock_finished})
                elif continuation.exists():
                    continuation.unlink()
        atomic_json(receipt, {'cards': cards, 'warnings': warnings, 'retry_seconds': retry_seconds, 'cards_read': cards_read})
    digest, parts, chunk = hashlib.sha256(), 0, []
    def send(values):
        nonlocal parts
        if lost.is_set():
            raise ValueError('原电脑连接中断，停止回传同步结果')
        client.request('POST', endpoint + '/wb-history-part', {'index': parts, 'cards': values}, extra_headers=headers)
        digest.update(_raw(values)); parts += 1
    for card in cards:
        if len(chunk) >= 100 or len(_raw(chunk + [card])) > 45000:
            send(chunk); chunk = []
        chunk.append(card)
    if chunk:
        send(chunk)
    return {'chunks': parts, 'cards': len(cards), 'sha256': digest.hexdigest(),
            'shop_sync': {'warnings': warnings, 'retry_seconds': retry_seconds, 'cards_read': cards_read}}
