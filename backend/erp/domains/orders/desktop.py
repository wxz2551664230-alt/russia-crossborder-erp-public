"""Private, leased personal-computer order transport; no platform calls here."""
import hashlib

from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps, loads
from erp.core.security import vault
from erp.domains.orders.service import manager

TYPES = {'pod_order_sync', 'pod_order_image_sync'}


def snapshot(session, job):
    if job.type not in TYPES:
        raise ConflictError('不是订单读取任务')
    config = vault.decrypt(job.config_snapshot_json)
    return {'config': config, 'payload': loads(job.payload_json, {}),
            'offers': manager(session).platform_image_offers(config['store_id'])}


def accept_batch(session, job, index, postings, final):
    if job.type != 'pod_order_sync' or type(index) is not int or not 0 <= index < 10000:
        raise ConflictError('订单批次编号无效')
    if not isinstance(postings, list) or len(postings) > 200 or any(not isinstance(row, dict) for row in postings):
        raise ConflictError('每个回传批次最多 200 条订单')
    step = next(s for s in job.steps if s.key == 'sync')
    state = loads(step.details_json, {})
    receipts = state.get('batches', [])
    digest = hashlib.sha256(dumps([postings, bool(final)]).encode()).hexdigest()
    if index < len(receipts):
        if receipts[index]['digest'] != digest:
            raise ConflictError('已接收批次内容不同，拒绝覆盖')
        return receipts[index]['summary']
    if step.status == 'completed' or index != len(receipts):
        raise ConflictError('订单批次必须按原顺序回传')
    config = vault.decrypt(job.config_snapshot_json)
    summary = manager(session).import_postings(postings, platform=config['platform'],
        store_id=config['store_id'], store_name=config['store_name'], wb_rub_to_cny_rate=config['wb_rate'],
        receipt_key=f'{job.id}:orders:{index}')
    receipts.append({'digest':digest, 'summary':summary})
    step.details_json = dumps({'batches':receipts})
    step.status = 'completed' if final else 'running'
    step.progress = 100 if final else 0
    return summary


def accept_images(session, job, images):
    if job.type not in TYPES or not isinstance(images, dict):
        raise ConflictError('订单图片结果无效')
    if job.type == 'pod_order_sync' and not any(s.key == 'sync' and s.status == 'completed' for s in job.steps):
        raise ConflictError('先完成订单数据回传')
    config = vault.decrypt(job.config_snapshot_json)
    orders = manager(session)
    offers = set(orders.platform_image_offers(config['store_id']))
    if not set(images) <= offers or any(not isinstance(url, str) or not url.startswith(('https://','http://')) or len(url)>4000 for url in images.values()):
        raise ConflictError('图片结果不属于当前店铺订单')
    step = next(s for s in job.steps if s.key == 'images')
    digest = hashlib.sha256(dumps(images).encode()).hexdigest()
    previous = loads(step.details_json, {})
    if step.status == 'completed':
        if previous.get('digest') != digest:
            raise ConflictError('已接收图片结果不同，拒绝覆盖')
        return previous['summary']
    summary = {'offers':len(offers), 'with_platform_image':len(images),
               'order_items_updated':orders.update_platform_images(config['store_id'],images)}
    step.details_json = dumps({'digest':digest,'summary':summary})
    step.status = 'completed'; step.progress = 100
    return summary


def completion_summary(job):
    if job.type not in TYPES or any(s.status != 'completed' for s in job.steps):
        raise ConflictError('订单读取步骤尚未全部回传')
    result = {}
    for step in job.steps:
        state = loads(step.details_json, {})
        if step.key == 'sync':
            result['sync'] = {key:sum(row['summary'][key] for row in state['batches']) for key in ('imported','updated','total')}
        elif step.key == 'images':
            result['images'] = state['summary']
    return result
