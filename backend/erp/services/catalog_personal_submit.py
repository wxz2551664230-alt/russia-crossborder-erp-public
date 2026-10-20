"""Lease-scoped Ozon import intentions and receipts; never calls Seller API."""
from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps, loads
from erp.services.catalog_personal_publications import digest
from erp.services.publications import get_publication, resolve_publication_snapshot, publication_status


def target(session, job):
    if job.type != 'catalog_ozon_publication' or not job.created_by or not job.target_worker_id:
        raise ConflictError('不是发起人电脑的产品库 Ozon 提交任务')
    payload = loads(job.payload_json, {})
    stores = payload.get('store_ids') or []
    if len(stores) != 1:
        raise ConflictError('提交任务必须绑定一个明确店铺')
    publication = get_publication(session, payload.get('publication_id', ''))
    child = next((row for row in publication.stores if row.store_id == stores[0]), None)
    if not child:
        raise ConflictError('发布记录或店铺已不属于本次任务')
    assets = loads(child.assets_json, {})
    if (assets.get('personal_submit_job_id') or publication.job_id) != job.id:
        raise ConflictError('店铺已有不同的提交任务')
    if not assets.get('package_ready') or not isinstance(assets.get('import_item'), dict):
        raise ConflictError('商品发布包尚未完成')
    if child.status not in {'ready_for_submit', 'submit_queued', 'import_uncertain', 'import_pending',
                             'import_submitted', 'stock_pending', 'failed'}:
        raise ConflictError('该商品当前不在建品阶段')
    if child.status == 'failed' and loads(job.external_ref, {}).get('state') != 'failed':
        raise ConflictError('失败记录没有本次原导入回执')
    return publication, child, assets['import_item']


def snapshot(session, job):
    publication, child, item = target(session, job)
    return {'schema': 1, 'job_id': job.id, 'publication_id': publication.id, 'store_id': child.store_id,
        'item': item, 'item_hash': digest(item), 'task_id': child.external_task_id,
        'status': child.status, 'config': resolve_publication_snapshot(child.settings_snapshot_json)}


def intent(session, job, item_hash):
    publication, child, item = target(session, job)
    if item_hash != digest(item):
        raise ConflictError('发布包已变化，禁止提交旧快照')
    prior = loads(job.external_ref, {})
    if prior:
        if prior.get('kind') != 'catalog_ozon_import' or prior.get('item_hash') != item_hash:
            raise ConflictError('已有不同的提交意图')
        return prior
    if child.status == 'import_uncertain' and not child.external_task_id:
        raise ConflictError('原建品结果未知，请先绑定原 task_id')
    value = {'kind': 'catalog_ozon_import', 'item_hash': item_hash, 'store_id': child.store_id,
             'task_id': child.external_task_id, 'state': 'pending'}
    job.external_ref = dumps(value)
    for step in job.steps:
        if step.key == child.store_id + ':validate':
            step.status = 'completed'; step.progress = 100
            step.details_json = dumps({'item_hash': item_hash})
    if not child.external_task_id:
        child.status = 'import_uncertain'
        child.last_error = '原电脑已保存提交意图，等待 Ozon task_id'
    publication.status = publication_status(publication.stores)
    return value


def accept_task(session, job, item_hash, task_id):
    publication, child, item = target(session, job)
    if type(task_id) is not int or task_id <= 0:
        raise ConflictError('Ozon task_id 无效')
    prior = loads(job.external_ref, {})
    if prior.get('kind') != 'catalog_ozon_import' or prior.get('item_hash') != item_hash or digest(item) != item_hash:
        raise ConflictError('没有匹配的建品提交意图')
    if child.external_task_id and child.external_task_id != str(task_id):
        raise ConflictError('原 task_id 不同，拒绝覆盖')
    if prior.get('state') in {'imported', 'failed'}:
        return {'task_id': child.external_task_id}
    child.external_task_id = str(task_id)
    child.status = 'import_pending'; child.last_error = ''
    prior.update(task_id=str(task_id), state='task_received')
    job.external_ref = dumps(prior)
    publication.status = publication_status(publication.stores)
    return {'task_id': str(task_id)}


def accept_import(session, job, item_hash, task_id, result):
    from erp.integrations.ozon import import_failed, import_succeeded, import_items
    publication, child, item = target(session, job)
    prior = loads(job.external_ref, {})
    if (str(task_id) != child.external_task_id or not child.external_task_id
            or prior.get('item_hash') != item_hash or digest(item) != item_hash):
        raise ConflictError('回查结果与原 task_id 或发布包不一致')
    if not isinstance(result, dict) or len(dumps(result).encode()) > 1024 * 1024:
        raise ConflictError('导入回查格式无效')
    if prior.get('state') in {'imported', 'failed'}:
        if prior.get('result_hash') != digest(result):
            raise ConflictError('原导入已有不同的最终回执')
        return {'task_id': child.external_task_id, 'status': child.status,
                'product_id': child.external_product_id, 'result_hash': digest(result)}
    rows = import_items(result)
    if any(str(row.get('offer_id') or '') != child.offer_id for row in rows):
        raise ConflictError('回查包含其他商品货号')
    if import_failed(result):
        child.status = 'failed'; child.last_error = 'Ozon 返回建品失败，请查看原导入任务'
        prior.update(state='failed', result_hash=digest(result))
    elif import_succeeded(result):
        if len(rows) != 1 or not str(rows[0].get('product_id') or rows[0].get('id') or '').isdigit():
            raise ConflictError('建品成功回执缺少唯一商品编号')
        child.external_product_id = str(rows[0].get('product_id') or rows[0].get('id'))
        child.status = 'stock_pending'; child.stock_status = 'pending'; child.promotion_status = 'pending'
        child.last_error = ''
        prior.update(state='imported', product_id=child.external_product_id, result_hash=digest(result))
        for step in job.steps:
            if step.key == child.store_id + ':submit':
                step.status = 'completed'; step.progress = 100
                step.details_json = dumps({'task_id': child.external_task_id, 'product_id': child.external_product_id})
    else:
        child.status = 'import_pending'
        prior.update(state='pending_result', result_hash=digest(result))
    job.external_ref = dumps(prior)
    publication.status = publication_status(publication.stores)
    return {'task_id': child.external_task_id, 'status': child.status,
            'product_id': child.external_product_id, 'result_hash': digest(result)}


def finish(session, job, receipt):
    from erp.models import utc_now
    publication, child, item = target(session, job)
    prior = loads(job.external_ref, {})
    expected = {'task_id': child.external_task_id, 'status': child.status,
                'product_id': child.external_product_id, 'result_hash': prior.get('result_hash')}
    if (receipt != expected or prior.get('state') not in {'imported', 'failed', 'pending_result'}
            or prior.get('item_hash') != digest(item)):
        raise ConflictError('建品阶段完成摘要与原 task_id 回执不一致')
    if prior.get('finished_hash'):
        if prior['finished_hash'] != digest(receipt):
            raise ConflictError('已有不同的阶段完成清单')
        return {'status': job.status}
    job.status = 'failed' if prior['state'] == 'failed' else 'action_required'
    job.error_code = 'ozon_import_failed' if job.status == 'failed' else ''
    job.error_message = 'Ozon 返回建品失败，请核对原导入任务' if job.status == 'failed' else ''
    job.finished_at = utc_now(); job.lease_owner = ''; job.lease_expires_at = None
    prior['finished_hash'] = digest(receipt)
    job.external_ref = dumps(prior)
    return {'status': job.status}
