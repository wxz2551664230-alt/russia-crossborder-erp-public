"""Read WB cards on the initiating PC and upload only compact card identities."""
import hashlib
import json
import os
import time
from pathlib import Path

import requests


def _raw(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8')


def scan_cards(config,lost):
    secrets=(config.get('store') or {}).get('secrets') or {}
    token=next((str(secrets.get(key) or '').strip() for key in ('token','api_key','wildberries_token') if str(secrets.get(key) or '').strip()),'')
    if not token:raise ValueError('WB 店铺缺少 Content API Token')
    cards=[];cursor={'limit':100};previous=None
    with requests.Session() as http:
        http.headers.update({'Authorization':token})
        for page_number in range(500):
            if lost.is_set():raise ValueError('本机任务连接已失效，停止 WB 扫描')
            if page_number:time.sleep(.7)
            response=http.post('https://content-api.wildberries.ru/content/v2/get/cards/list',
                json={'settings':{'sort':{'ascending':False},'filter':{'withPhoto':-1},'cursor':cursor}},timeout=60)
            if response.status_code==429:raise ValueError('WB Content API 已限流；停止扫描，等待人工重新提交')
            response.raise_for_status();value=response.json()
            if value.get('error') is True:raise ValueError('WB Content API 拒绝了商品卡读取')
            page=value.get('cards') or []
            if not isinstance(page,list):raise ValueError('WB 商品卡响应格式错误')
            for card in page:
                if not isinstance(card,dict):continue
                vendor=str(card.get('vendorCode') or '').strip()
                try:identity=int(card.get('nmID') or 0)
                except (TypeError,ValueError):continue
                if vendor and len(vendor)<=200 and identity>0:cards.append({'vendorCode':vendor,'nmID':identity})
            next_cursor=value.get('cursor') if isinstance(value.get('cursor'),dict) else {}
            if len(page)<100:return cards
            if not next_cursor.get('updatedAt') or not next_cursor.get('nmID'):
                raise ValueError('WB 扫描游标缺失，拒绝把不完整结果写入台账')
            candidate=(next_cursor['updatedAt'],next_cursor['nmID'])
            if candidate==previous:raise ValueError('WB 扫描游标未前进')
            previous=candidate;cursor={'limit':100,'updatedAt':candidate[0],'nmID':candidate[1]}
    raise ValueError('WB 商品卡超过 500 页，本轮扫描未完整结束')


def run(client,endpoint,job,root,lost):
    headers={'X-Worker-Attempt':str(job['attempts'])}
    snapshot=client.request('POST',endpoint+'/wb-history-snapshot',{},extra_headers=headers)[0]
    if snapshot.get('shop_sync')==1:
        from wb_shop_sync_worker import run as sync_shop
        return sync_shop(client,endpoint,job,root,lost,snapshot)
    store_id=snapshot['store_id'];config=snapshot['config']
    secrets=(config.get('store') or {}).get('secrets') or {}
    token=next((str(secrets.get(key) or '').strip() for key in ('token','api_key','wildberries_token') if str(secrets.get(key) or '').strip()),'')
    cache_key=hashlib.sha256((store_id+':'+token).encode()).hexdigest()
    folder=root/'wb-history-cache';folder.mkdir(parents=True,exist_ok=True)
    cache=folder/(cache_key+'.json')
    cards=None
    if cache.exists() and time.time()-cache.stat().st_mtime<300:
        try:
            value=json.loads(cache.read_text(encoding='utf-8'))
            if isinstance(value,list):cards=value
        except (OSError,ValueError):pass
    if cards is None:
        cards=scan_cards(config,lost)
        temporary=cache.with_suffix('.partial');temporary.write_bytes(_raw(cards));os.replace(temporary,cache)
    digest=hashlib.sha256();chunks=0
    for index in range(0,len(cards),200):
        if lost.is_set():raise ValueError('本机任务连接已失效，停止回传')
        part=cards[index:index+200]
        raw=_raw(part)
        if len(raw)>48000:raise ValueError('WB 商品卡分片过大')
        client.request('POST',endpoint+'/wb-history-part',{'index':chunks,'cards':part},extra_headers=headers)
        digest.update(raw);chunks+=1
    return {'chunks':chunks,'cards':len(cards),'sha256':digest.hexdigest()}
