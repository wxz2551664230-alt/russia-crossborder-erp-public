"""Lease-scoped input for running the original Ozon follow AI flow on a PC."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from sqlalchemy.orm import Session

from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps, loads
from erp.models import Job
from erp.services.jobs import JobContext
from erp.services.ozon_follow import _config, _draft, _load
from erp.modules.ozon_follow.legacy.services.ozon_listing import OzonListingDraft


_CHANGED_BY_AI = frozenset({
    'title', 'description_ru', 'tags_ru', 'attributes', 'complex_attributes',
    'primary_image', 'images', 'images_oss_ready', 'ai_preparation',
})


def _source(listing) -> dict:
    product = listing.research_product
    return {
        'sku': product.source_sku,
        'title': product.title,
        'brand': product.brand,
        'image_url': product.image_url,
        'image_urls': loads(product.image_urls_json, []),
        'ozon_url': product.ozon_url,
        'weight_grams': product.weight_grams,
    }


def _revision(product: dict, draft: dict) -> str:
    # Exclude all credentials. Any product or store draft edit invalidates a
    # result based on an older snapshot.
    return hashlib.sha256(json.dumps({'product': product, 'draft': draft}, sort_keys=True,
                                     ensure_ascii=False, separators=(',', ':'), default=str).encode('utf-8')).hexdigest()


def snapshot(session: Session, job: Job, store_id: str) -> dict:
    if job.type != 'ozon_ai_prepare':
        raise ConflictError('不是 Ozon 跟卖 AI 准备任务')
    context = JobContext(session, job)
    listing, children = _load(context)
    child = next((item for item in children if item.store_id == store_id), None)
    if child is None:
        raise ConflictError('店铺不属于本次任务')
    product = _source(listing)
    draft = _draft(listing, child).to_dict()
    config = asdict(_config(context, child))
    return {
        'schema': 1,
        'job_id': job.id,
        'listing_id': listing.id,
        'store_id': child.store_id,
        'store_name': child.store.name,
        'source': product,
        'draft': draft,
        'config': config,
        'revision': _revision(product, draft),
    }


def accept_result(session: Session, job: Job, store_id: str, *,
                  revision: str, draft_data: dict) -> dict:
    """Accept only fields the original AI preparation changes, once per store."""
    if job.type != 'ozon_ai_prepare':
        raise ConflictError('不是 Ozon 跟卖 AI 准备任务')
    if not isinstance(draft_data, dict) or len(dumps(draft_data)) > 1024 * 1024:
        raise ConflictError('Ozon AI 结果格式或大小无效')
    context = JobContext(session, job)
    listing, children = _load(context)
    child = next((item for item in children if item.store_id == store_id), None)
    if child is None:
        raise ConflictError('店铺不属于本次任务')
    step = next((item for item in job.steps if item.key == f'{store_id}:ai_prepare'), None)
    if step is None:
        raise ConflictError('目标店铺缺少 AI 准备步骤')
    result_hash = hashlib.sha256(json.dumps(draft_data, sort_keys=True, ensure_ascii=False,
                                           separators=(',', ':'), default=str).encode('utf-8')).hexdigest()
    receipt = loads(step.details_json, {})
    if step.status == 'completed':
        if receipt.get('revision') == revision and receipt.get('result_hash') == result_hash:
            return {'store_id': store_id, 'status': child.status, 'result_hash': result_hash}
        raise ConflictError('该店铺已有不同的 AI 准备结果')
    current = _draft(listing, child).to_dict()
    if _revision(_source(listing), current) != revision:
        raise ConflictError('商品或店铺草稿已变化，请重新领取快照')
    if set(draft_data) != set(current) or any(
        draft_data[key] != current[key] for key in current if key not in _CHANGED_BY_AI
    ):
        raise ConflictError('AI 结果修改了商品、货源或店铺基础字段')
    draft = OzonListingDraft.from_dict(draft_data)
    if not isinstance(draft.ai_preparation, dict) or draft.ai_preparation.get('image_review_status') not in {'PENDING', 'APPROVED'}:
        raise ConflictError('AI 图片准备状态无效')
    if not draft.title or not draft.description_ru or not isinstance(draft.attributes, list):
        raise ConflictError('AI 文案或属性缺失')
    child.content_json = dumps({'title': draft.title, 'description_ru': draft.description_ru,
                                'tags': draft.tags_ru})
    child.assets_json = dumps({'primary_image': draft.primary_image, 'images': draft.images,
                               'ai_preparation': draft.ai_preparation})
    child.attributes_json = dumps(draft.attributes)
    child.complex_attributes_json = dumps(draft.complex_attributes)
    child.status = 'image_review' if draft.ai_preparation['image_review_status'] == 'PENDING' else 'ready'
    child.last_error = ''
    if draft.title:
        listing.title = draft.title
    if draft.description_ru:
        listing.description_ru = draft.description_ru
    step.status = 'completed'
    step.progress = 100
    step.details_json = dumps({'revision': revision, 'result_hash': result_hash})
    from erp.models import utc_now
    step.finished_at = utc_now()
    return {'store_id': store_id, 'status': child.status, 'result_hash': result_hash}


def completion_summary(job: Job) -> dict:
    stores = {}
    for step in job.steps:
        if not step.key.endswith(':ai_prepare') or step.status != 'completed':
            raise ConflictError('Ozon 跟卖 AI 准备仍有店铺未完成')
        receipt = loads(step.details_json, {})
        if not isinstance(receipt.get('result_hash'), str) or len(receipt['result_hash']) != 64:
            raise ConflictError('Ozon 跟卖 AI 准备回执缺失')
        stores[step.key.removesuffix(':ai_prepare')] = receipt['result_hash']
    if not stores:
        raise ConflictError('Ozon 跟卖 AI 准备没有目标店铺')
    return {'schema': 1, 'stores': stores}
