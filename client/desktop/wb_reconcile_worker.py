"""Read WB card IDs on the initiating PC; return only exact offer matches."""
from __future__ import annotations

import time
import requests


def find_cards_exact(offers, config, lost):
    if not isinstance(offers, list) or not offers or len(offers) > 1000 or any(not isinstance(x, str) or not x for x in offers):
        raise ValueError('WB 核对商品清单无效')
    secrets = (config.get('store') or {}).get('secrets') or {}
    token = next((str(secrets.get(key) or '').strip() for key in ('token', 'api_key', 'wildberries_token')
                  if str(secrets.get(key) or '').strip()), '')
    if not token:
        raise ValueError('WB 店铺未配置 Content API Token')
    wanted = {offer.strip().casefold(): offer for offer in offers}
    found = {}
    cursor = {'limit': 100}
    with requests.Session() as http:
        http.headers.update({'Authorization': token})
        for page_number in range(500):
            if lost.is_set():
                raise ValueError('任务连接已失效，停止核对')
            if page_number:
                time.sleep(.7)
            response = http.post('https://content-api.wildberries.ru/content/v2/get/cards/list',
                                 json={'settings': {'sort': {'ascending': False},
                                                    'filter': {'withPhoto': -1}, 'cursor': cursor}}, timeout=30)
            response.raise_for_status()
            data = response.json()
            page = [card for card in data.get('cards') or [] if isinstance(card, dict)]
            for card in page:
                offer = wanted.get(str(card.get('vendorCode') or '').strip().casefold())
                if offer and int(card.get('nmID') or 0) > 0:
                    found[offer] = card
            if len(found) == len(wanted):
                break
            next_cursor = data.get('cursor') if isinstance(data.get('cursor'), dict) else {}
            if len(page) < 100 or not next_cursor.get('updatedAt') or not next_cursor.get('nmID'):
                break
            cursor = {'limit': 100, 'updatedAt': next_cursor['updatedAt'], 'nmID': next_cursor['nmID']}
    if lost.is_set():
        raise ValueError('任务连接已失效，停止回传')
    return found


def run(client, endpoint, job, root, lost):
    headers = {'X-Worker-Attempt': str(job['attempts'])}
    snapshot = client.request('POST', endpoint + '/wb-reconcile-snapshot', {}, extra_headers=headers)[0]
    if lost.is_set():
        raise ValueError('任务连接已失效，停止核对')
    cards = find_cards_exact(snapshot['offer_ids'], snapshot['config'], lost)
    found = {offer: int(card['nmID']) for offer, card in cards.items()}
    return {'found': found, 'checked': len(snapshot['offer_ids'])}
