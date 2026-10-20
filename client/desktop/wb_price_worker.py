"""Submit WB prices on the initiating PC and verify platform processing."""
import hashlib
import json
import re
import time
from datetime import datetime, timedelta

import requests

from composition_worker import atomic_json


class PriceOutcomeUnknown(ValueError):
    code = 'wb_price_outcome_unknown'


class PriceRateLimited(ValueError):
    code = 'wb_price_rate_limited'


def _prices_match(http, rows):
    # This POST is a read-only lookup, not a price upload.
    response = http.post('https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter',
                         json={'nmList': [row['nm_id'] for row in rows]}, timeout=30)
    response.raise_for_status()
    goods = (response.json().get('data') or {}).get('listGoods') or []
    by_id = {}
    for item in goods:
        nm_id = int(item.get('nmID') or 0)
        if nm_id in by_id:
            return False
        by_id[nm_id] = item
    return all(row['nm_id'] in by_id
               and int(by_id[row['nm_id']].get('discount') or 0) == row['discount']
               and any(int(size.get('price') or 0) == row['price']
                       for size in by_id[row['nm_id']].get('sizes') or [])
               for row in rows)


def _wait(lost, seconds):
    if lost.wait(seconds):
        raise PriceOutcomeUnknown('连接中断，已停止等待；保留原上传编号，恢复后只核对')


def run(client, endpoint, job, root, lost):
    if not re.fullmatch(r'[a-f0-9]{32}', job['id']):
        raise ValueError('WB 价格任务编号无效')
    headers = {'X-Worker-Attempt': str(job['attempts'])}
    snapshot = client.request('POST', endpoint + '/wb-price-snapshot', {}, extra_headers=headers)[0]
    rows = snapshot['rows']
    if not rows or len(rows) > 1000 or any(
        not isinstance(row.get('nm_id'), int) or row['nm_id'] <= 0
        or not isinstance(row.get('price'), int) or row['price'] <= 0
        or not isinstance(row.get('discount'), int) or not 0 <= row['discount'] <= 99
        for row in rows
    ):
        raise ValueError('WB 价格任务参数无效')
    if len({row['nm_id'] for row in rows}) != len(rows):
        raise ValueError('WB 商品编号重复')
    secrets = (snapshot['config'].get('store') or {}).get('secrets') or {}
    token = next((str(secrets.get(key) or '').strip() for key in ('token', 'api_key', 'wildberries_token')
                  if str(secrets.get(key) or '').strip()), '')
    if not token:
        raise ValueError('WB 店铺未配置价格接口 Token')
    intent = {'rows': rows}
    digest = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()
    folder = root / 'wb-price-tasks' / job['id']
    folder.mkdir(parents=True, exist_ok=True)
    checkpoint = folder / 'write.json'
    store_id = str((job.get('payload') or {}).get('store_id') or '')
    cooldown = root / 'wb-price-tasks' / ('cooldown-' + store_id + '.json') if re.fullmatch(r'[a-f0-9]{32}', store_id) else None
    previous = json.loads(checkpoint.read_text(encoding='utf-8')) if checkpoint.exists() else None
    receipt = snapshot.get('upload_receipt') or {}
    if receipt.get('kind') == 'wb_price_upload':
        if receipt.get('digest') != digest:
            raise PriceOutcomeUnknown('云端原价格快照与本次参数不符，禁止上传')
        if not previous:
            previous = {'state': 'acknowledged', 'digest': digest, 'intent': intent, 'upload_id': receipt['upload_id']}
            atomic_json(checkpoint, previous)
    def progress(upload_id, wait_seconds=0):
        if snapshot.get('progress_protocol') != 1: return None
        return client.request('POST', endpoint + '/wb-price-progress',
            {'digest': digest, 'upload_id': upload_id, 'wait_seconds': wait_seconds}, extra_headers=headers)[0]
    if previous and previous.get('digest') != digest:
        raise PriceOutcomeUnknown('同一任务的价格参数已变化，拒绝重发')
    with requests.Session() as http:
        http.headers.update({'Authorization': token})
        if snapshot.get('verify_only') is True:
            try:
                matched = _prices_match(http, rows)
            except requests.RequestException as error:
                raise PriceOutcomeUnknown('WB 价格只读核对失败，请稍后重查') from error
            if not matched:
                raise PriceOutcomeUnknown('WB 实际价格或折扣与计划不一致；本次未写入')
            if lost.is_set():
                raise PriceOutcomeUnknown('价格已核实，但连接中断；等待原任务恢复回传')
            return {'verified': True, 'verified_existing': True, 'upload_id': 0, 'rows': rows}
        if previous and previous.get('state') == 'rejected_rate_limit':
            raise PriceRateLimited('WB 价格接口限流；本次未创建上传任务')
        if previous and previous.get('state') in {'acknowledged', 'done'}:
            upload_id = int(previous.get('upload_id') or 0)
            if previous.get('state') == 'done' and previous.get('verified_existing') is True:
                if not _prices_match(http, rows):
                    raise PriceOutcomeUnknown('WB 价格回读已变化，请人工核对')
                if lost.is_set():
                    raise PriceOutcomeUnknown('连接已失效，价格核对结果等待恢复回传')
                return {'verified': True, 'verified_existing': True, 'upload_id': 0, 'rows': rows}
            if upload_id <= 0:
                raise PriceOutcomeUnknown('已提交价格但上传编号丢失，请人工核对')
        elif previous:
            raise PriceOutcomeUnknown('上次价格请求结果未知，请查询 WB 平台，禁止自动重发')
        else:
            if lost.is_set():
                raise ValueError('任务连接已失效，尚未提交价格')
            if cooldown and cooldown.exists():
                state = json.loads(cooldown.read_text(encoding='utf-8'))
                remaining = max(0, int(state.get('until', 0) - time.time()))
                if remaining:
                    raise PriceRateLimited(f'WB 价格接口仍在限流，约 {remaining} 秒后再试；本次未发请求')
            atomic_json(checkpoint, {'state': 'pending', 'digest': digest, 'intent': intent})
            data = [{'nmID': row['nm_id'], 'price': row['price'], 'discount': row['discount']} for row in rows]
            try:
                response = http.post('https://discounts-prices-api.wildberries.ru/api/v2/upload/task',
                                     json={'data': data}, timeout=60)
            except requests.RequestException as error:
                raise PriceOutcomeUnknown('WB 价格请求结果未知，禁止自动重发') from error
            if response.status_code == 429:
                retry_after = response.headers.get('X-RateLimit-Retry') or ''
                seconds = max(1, int(retry_after or 60))
                if cooldown:
                    cooldown.parent.mkdir(parents=True, exist_ok=True)
                    atomic_json(cooldown, {'until': time.time() + seconds})
                atomic_json(checkpoint, {'state': 'rejected_rate_limit', 'digest': digest,
                                         'intent': intent, 'retry_after': retry_after})
                local_ready = (datetime.now().astimezone() + timedelta(seconds=seconds)).strftime('%Y-%m-%d %H:%M')
                raise PriceRateLimited(f'WB 价格接口限流，约 {seconds} 秒后重试（本机时间 {local_ready}）；本次未创建上传任务')
            if response.status_code == 400 and 'Specified prices and discounts are already set' in response.text:
                try:
                    matched = _prices_match(http, rows)
                except requests.RequestException as error:
                    raise PriceOutcomeUnknown('WB 回复价格已设置，但回读失败，请人工核对') from error
                if not matched:
                    raise PriceOutcomeUnknown('WB 回复价格已设置，但实际价格与计划不一致，请人工核对')
                atomic_json(checkpoint, {'state': 'done', 'digest': digest, 'intent': intent,
                                         'upload_id': 0, 'verified_existing': True})
                if lost.is_set():
                    raise PriceOutcomeUnknown('价格已核实，但连接中断；等待同一任务恢复回传')
                return {'verified': True, 'verified_existing': True, 'upload_id': 0, 'rows': rows}
            if response.status_code not in {200, 208}:
                raise PriceOutcomeUnknown(f'WB 价格请求 HTTP {response.status_code}；先核对平台，不自动重发')
            upload_id = int((response.json().get('data') or {}).get('id') or 0)
            if upload_id <= 0:
                raise PriceOutcomeUnknown('WB 价格上传编号缺失，请核对平台')
            atomic_json(checkpoint, {'state': 'acknowledged', 'digest': digest,
                                     'intent': intent, 'upload_id': upload_id})
        if cooldown and cooldown.exists():
            remaining = max(0, int(json.loads(cooldown.read_text(encoding='utf-8')).get('until', 0) - time.time()))
            if remaining:
                deferred = progress(upload_id, remaining)
                if deferred: return deferred
        progress(upload_id)
        poll_deadline = time.monotonic() + 630
        for attempt in range(90):
            if time.monotonic() >= poll_deadline:
                deferred = progress(upload_id, 60)
                if deferred: return deferred
                raise PriceOutcomeUnknown('WB 价格仍在处理；保留原上传编号，恢复时只核对不重发')
            if lost.is_set():
                raise PriceOutcomeUnknown('连接中断；价格任务已提交，等待同一任务恢复核对')
            if attempt:
                _wait(lost, min(60, 7 * (2 ** min(attempt - 1, 4))))
            response = http.get('https://discounts-prices-api.wildberries.ru/api/v2/history/tasks',
                                params={'uploadID': upload_id}, timeout=30)
            if response.status_code == 429:
                delay = min(86400, max(7, int(response.headers.get('X-RateLimit-Retry') or 7)))
                if cooldown: atomic_json(cooldown, {'until': time.time() + delay})
                deferred = progress(upload_id, delay)
                if deferred: return deferred
                _wait(lost, min(900, max(7, int(response.headers.get('X-RateLimit-Retry') or 7))))
                continue
            response.raise_for_status()
            status = response.json().get('data') or {}
            if int(status.get('status') or 0) == 3:
                if int(status.get('successGoodsNumber') or 0) != len(rows):
                    raise PriceOutcomeUnknown('WB 价格任务仅部分成功，请人工核对失败商品')
                break
            if int(status.get('status') or 0) in {4, 5, 6}:
                raise PriceOutcomeUnknown('WB 价格任务处理失败，请核对详情')
        else:
            raise PriceOutcomeUnknown('WB 价格任务仍在处理中，不自动重发')
        for attempt in range(6):
            if lost.is_set():
                raise PriceOutcomeUnknown('连接中断；等待原任务恢复价格核对')
            if attempt:
                _wait(lost, 7)
            try:
                if _prices_match(http, rows):
                    break
            except requests.HTTPError as error:
                response = error.response
                if response is None or response.status_code != 429:
                    raise
                delay = min(86400, max(7, int(response.headers.get('X-RateLimit-Retry') or 7)))
                if cooldown: atomic_json(cooldown, {'until': time.time() + delay})
                deferred = progress(upload_id, delay)
                if deferred: return deferred
                _wait(lost, delay)
        else:
            raise PriceOutcomeUnknown('WB 已处理价格任务，但商品价格回读不一致')
        atomic_json(checkpoint, {'state': 'done', 'digest': digest,
                                 'intent': intent, 'upload_id': upload_id})
    if lost.is_set():
        raise PriceOutcomeUnknown('价格已核实，但连接中断；等待同一任务恢复回传')
    return {'verified': True, 'upload_id': upload_id, 'rows': rows}
