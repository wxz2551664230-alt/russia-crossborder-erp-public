"""Selected-publication transport for personal-computer Ozon preparation.

No AI or Seller API calls run here. Preparation receipts cannot import products,
approve images, change supplier facts, or change another store's publication.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy

from sqlalchemy import select

from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps, loads
from erp.models import Publication, PublicationStore
from erp.services.publications import (get_publication, publication_status,
    resolve_publication_snapshot, _price, validate_product_facts, _main_generation_requested)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def _target(session, job, store_id):
    if job.type != 'catalog_ozon_prepare' or not job.created_by or not job.target_worker_id:
        raise ConflictError('不是发起人电脑的产品库 Ozon 准备任务')
    payload = loads(job.payload_json, {})
    publication = get_publication(session, str(payload.get('publication_id') or ''))
    requested = payload.get('store_ids') or [row.store_id for row in publication.stores]
    child = next((row for row in publication.stores if row.store_id == store_id and store_id in requested), None)
    if child is None:
        raise ConflictError('店铺不属于本次发布任务')
    if (loads(child.assets_json,{}).get('personal_prepare_job_id') or publication.job_id) != job.id:
        raise ConflictError('该店铺已有新的准备任务')
    if child.external_task_id or child.status in {'submit_queued', 'import_uncertain', 'import_submitting', 'import_pending',
            'import_submitted', 'stock_pending', 'promotion_pending', 'completed'}:
        raise ConflictError('已进入平台提交阶段，不能回传 AI 准备结果')
    return publication, child


def _revision(publication, child):
    return digest({'product': loads(publication.product_snapshot_json, {}),
        'requirements': loads(publication.requirements_json, {}),
        'image_policy': loads(publication.image_policy_json, {}),
        'settings': child.settings_snapshot_json, 'offer_id': child.offer_id,
        'child_id': child.id, 'save_generated': publication.save_generated_to_catalog,
        'submit_via_api': publication.submit_via_api})


def snapshot(session, job, store_id):
    publication, child = _target(session, job, store_id)
    previous = session.scalars(select(PublicationStore.content_json).join(Publication,
        Publication.id == PublicationStore.publication_id).where(
        Publication.product_id == publication.product_id, Publication.id != publication.id,
        PublicationStore.content_json != '{}').order_by(PublicationStore.updated_at.desc()).limit(10)).all()
    return {'schema': 1, 'job_id': job.id, 'publication_id': publication.id,
        'product_id': publication.product_id, 'store_id': child.store_id,
        'store_name': child.store.name, 'offer_id': child.offer_id, 'revision': _revision(publication, child),
        'facts': loads(publication.product_snapshot_json, {}),
        'requirements': loads(publication.requirements_json, {}),
        'image_policy': loads(publication.image_policy_json, {}),
        'config': resolve_publication_snapshot(child.settings_snapshot_json),
        'prior_copy_for_difference_check': [loads(raw, {}) for raw in previous]
            + [loads(row.content_json, {}) for row in publication.stores if row.id != child.id and loads(row.content_json, {})],
        'store_index': next(i for i, row in enumerate(publication.stores) if row.id == child.id),
        'resume': {key: loads(getattr(child, key + '_json'), {} if key not in {'attributes', 'complex_attributes'} else [])
                   for key in ('content', 'attributes', 'complex_attributes', 'assets', 'pricing', 'package')}}


def accept_review_candidate(session, job, store_id, revision, candidate):
    publication, child = _target(session, job, store_id)
    from erp.services.publications import _additional_generation_requested
    policy=loads(publication.image_policy_json,{})
    codex_additional=loads(publication.requirements_json,{}).get('generation_provider')=='codex' and _additional_generation_requested(policy)
    if revision != _revision(publication, child) or not (_main_generation_requested(policy) or codex_additional):
        raise ConflictError('主图候选与本次任务不一致')
    if (not isinstance(candidate, dict) or set(candidate) != {'sha256', 'url', 'content', 'attributes', 'complex_attributes', 'assets'}
            or not re.fullmatch('[a-f0-9]{64}', str(candidate.get('sha256', '')))
            or not str(candidate.get('url', '')).startswith('https://')
            or len(dumps(candidate).encode()) > 1024 * 1024):
        raise ConflictError('主图候选回执格式无效')
    _public_result(candidate)
    content, assets = candidate['content'], candidate['assets']
    preparation = assets.get('ai_preparation') if isinstance(assets, dict) else None
    if (not isinstance(content, dict) or not content.get('title') or not content.get('description_ru')
            or content.get('generated_fresh') is not True or not isinstance(assets, dict)
            or not isinstance(preparation, dict) or preparation.get('image_review_status') != 'PENDING'
            or not isinstance(candidate['attributes'], list) or not isinstance(candidate['complex_attributes'], list)):
        raise ConflictError('候选文案或人工核验状态无效')
    old = loads(child.assets_json, {}).get('personal_review') or {}
    if old.get('human_approval'):
        raise ConflictError('该候选已人工批准，不能覆盖')
    if old.get('copy_revision'):
        raise ConflictError('该候选已有文案修订，不能重收旧候选覆盖')
    action = old.get('image_action') or {}
    if action and assets.get('personal_image_action_id') != action.get('id'):
        raise ConflictError('候选没有匹配本次重新生图操作')
    if old and not action and old.get('candidate_hash') != digest(candidate):
        raise ConflictError('该任务已有不同候选，请使用原重新生图操作')
    assets = dict(assets)
    prepare_binding=loads(child.assets_json,{}).get('personal_prepare_job_id')
    if prepare_binding:assets['personal_prepare_job_id']=prepare_binding
    assets['personal_review'] = {'revision': revision, 'sha256': candidate['sha256'], 'url': candidate['url'],
                                'candidate_hash': digest(candidate), 'human_approval': None}
    child.content_json = dumps(content); child.assets_json = dumps(assets)
    child.attributes_json = dumps(candidate['attributes']); child.complex_attributes_json = dumps(candidate['complex_attributes'])
    child.status = 'image_review'; child.last_error = ''
    publication.status = publication_status(publication.stores)
    return {'store_id': store_id, 'status': 'image_review', 'candidate_hash': digest(candidate)}


def request_image_regeneration(session, job, store_id, actor_id, action):
    import uuid
    publication, child = _target(session, job, store_id)
    assets = loads(child.assets_json, {})
    review = assets.get('personal_review') or {}
    if (actor_id != job.created_by or not actor_id or child.status != 'image_review'
            or review.get('revision') != _revision(publication, child) or review.get('human_approval')
            or action not in {'regenerate','redesign','full_redesign','repair'}):
        raise ConflictError('只能由原发起人重新处理待核验图片')
    review['image_action'] = {'id': uuid.uuid4().hex, 'action': action}
    assets['personal_review'] = review
    child.assets_json = dumps(assets)
    child.status = 'queued'
    return review['image_action']


def approve_review_candidate(session, job, store_id, actor_id):
    from erp.models import utc_now
    publication, child = _target(session, job, store_id)
    assets = loads(child.assets_json, {})
    review = assets.get('personal_review') or {}
    if (child.status != 'image_review' or actor_id != job.created_by or not actor_id
            or review.get('revision') != _revision(publication, child)
            or not re.fullmatch('[a-f0-9]{64}', str(review.get('sha256') or ''))):
        raise ConflictError('候选资料已变化或不是原发起人，不能批准')
    review['human_approval'] = {'actor_id': actor_id, 'sha256': review['sha256'], 'candidate_hash': review['candidate_hash'],
                                'approved_at': utc_now().isoformat()}
    assets['personal_review'] = review
    child.assets_json = dumps(assets)
    child.status = 'queued'
    return review['human_approval']


def _public_result(value):
    # Reject local files and secrets instead of silently accepting a result
    # that only works on the original computer.
    if isinstance(value, dict):
        forbidden = {'api_key', 'token', 'access_key_id', 'access_key_secret', 'secrets', 'secrets_encrypted'}
        if any(str(key).lower() in forbidden for key in value):
            raise ConflictError('准备结果包含凭据')
        for item in value.values():
            _public_result(item)
    elif isinstance(value, list):
        for item in value:
            _public_result(item)
    elif isinstance(value, str) and (re.match(r'^[A-Za-z]:[\\/]', value) or value.startswith(('\\\\', 'file://', '/tmp/', '/app/'))):
        raise ConflictError('准备结果仍依赖本机文件，请先上传共享资产')


def _copy_text_valid(content, *, require_shortfall=False):
    required = {'title', 'description_ru', 'tags', 'tags_shortfall_reason'} if require_shortfall else {'title', 'description_ru', 'tags'}
    if not isinstance(content, dict) or set(content) != required:
        return False
    if not all(isinstance(content[key], str) and content[key].strip() for key in ('title', 'description_ru')):
        return False
    if len(content['title']) > 500 or len(content['description_ru']) > 6000:
        return False
    if not all(re.search(r'[А-Яа-яЁё]', content[key]) for key in ('title', 'description_ru')):
        return False
    tags = content['tags']
    if not (isinstance(tags, list) and 1 <= len(tags) <= 30
            and all(isinstance(tag, str) and len(tag) <= 30
                    and re.fullmatch(r'#[А-Яа-яЁё0-9]+(?:_[А-Яа-яЁё0-9]+)*', tag) for tag in tags)
            and len(set(tags)) == len(tags)):
        return False
    if require_shortfall:
        reason = content['tags_shortfall_reason']
        if (not isinstance(reason, str) or len(reason) > 500
                or (len(tags) < 30 and (not reason.strip() or not re.search(r'[А-Яа-яЁё]', reason)))):
            return False
    return True


def revise_pending_copy(session, job, store_id, actor_id, worker_id, revision,
                        expected_candidate_hash, expected_content_hash, content):
    """Revise copy while retaining the immutable original candidate receipt."""
    from erp.models import utc_now
    publication, child = _target(session, job, store_id)
    assets = loads(child.assets_json, {})
    review = assets.get('personal_review') or {}
    preparation = assets.get('ai_preparation') or {}
    old = loads(child.content_json, {})
    old_copy = {key: old.get(key) for key in ('title', 'description_ru', 'tags')}
    if (not actor_id or actor_id != job.created_by or worker_id != job.target_worker_id
            or job.status != 'paused' or child.status != 'image_review'
            or loads(publication.requirements_json, {}).get('generation_provider') != 'codex'
            or not isinstance(preparation, dict) or preparation.get('producer') != 'codex-desktop'
            or preparation.get('image_review_status') != 'PENDING'
            or not isinstance(preparation.get('content'), dict)
            or review.get('human_approval') or review.get('image_action')
            or review.get('copy_revision')
            or review.get('revision') != revision or revision != _revision(publication, child)
            or not re.fullmatch('[a-f0-9]{64}', str(review.get('sha256') or ''))
            or not str(review.get('url') or '').startswith('https://')
            or not re.fullmatch('[a-f0-9]{64}', str(review.get('candidate_hash') or ''))
            or review.get('candidate_hash') != expected_candidate_hash
            or digest(old_copy) != expected_content_hash):
        raise ConflictError('文案修订与原发起人、电脑或待核验候选不一致')
    if not _copy_text_valid(content, require_shortfall=True) or not old.get('generated_fresh'):
        raise ConflictError('俄文文案修订格式无效')
    if len(dumps(content).encode()) > 32 * 1024:
        raise ConflictError('文案修订过大')
    old.update(title=content['title'].strip(), description_ru=content['description_ru'].strip(),
               tags=content['tags'], tags_shortfall_reason=content['tags_shortfall_reason'].strip())
    review['copy_revision'] = {'actor_id': actor_id, 'worker_id': worker_id,
        'candidate_hash': expected_candidate_hash, 'before': old_copy,
        'before_hash': expected_content_hash,
        'after': {key: old[key] for key in (*old_copy, 'tags_shortfall_reason')},
        'after_hash': digest({key: old[key] for key in (*old_copy, 'tags_shortfall_reason')}),
        'revised_at': utc_now().isoformat()}
    assets['personal_review'] = review
    if isinstance(preparation.get('content'), dict):
        preparation['content'].update(title_ru=old['title'], description_ru=old['description_ru'],
            tags_ru=old['tags'], tags_shortfall_reason=old['tags_shortfall_reason'])
        assets['ai_preparation'] = preparation
    child.content_json = dumps(old)
    child.assets_json = dumps(assets)
    return {'store_id': store_id, 'status': child.status, 'copy_hash': review['copy_revision']['after_hash']}


def _replace_copy_values(value, before, after):
    if isinstance(value, dict):
        return {key: _replace_copy_values(item, before, after) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_copy_values(item, before, after) for item in value]
    if isinstance(value, str):
        from erp.modules.ozon_follow.legacy.services.ai_listing import format_hashtags
        replacements = {before['title']: after['title'], before['description_ru']: after['description_ru'],
                        format_hashtags(before['tags']): format_hashtags(after['tags'])}
        return replacements.get(value, value)
    return value


def _apply_copy_revision(result, review):
    revision = review.get('copy_revision') or {}
    if not revision:
        return result
    before, after = revision.get('before'), revision.get('after')
    if (not _copy_text_valid(before) or not _copy_text_valid(after, require_shortfall=True)
            or digest(before) != revision.get('before_hash') or digest(after) != revision.get('after_hash')
            or revision.get('candidate_hash') != review.get('candidate_hash')):
        raise ConflictError('待核验文案修订记录已损坏')
    incoming = result.get('content') or {}
    if any(incoming.get(key) != before[key] for key in before):
        raise ConflictError('执行端恢复结果的文案与原候选不一致')
    result = deepcopy(result)
    result['content'].update(after)
    for key in ('attributes', 'complex_attributes'):
        result[key] = _replace_copy_values(result[key], before, after)
    assets = result['assets']
    prep = assets.get('ai_preparation') or {}
    if isinstance(prep.get('content'), dict):
        prep['content'].update({'title_ru': after['title'], 'description_ru': after['description_ru'],
                                'tags_ru': after['tags'], 'tags_shortfall_reason': after['tags_shortfall_reason']})
    if isinstance(assets.get('import_item'), dict):
        assets['import_item']['name'] = after['title']
        for key in ('attributes', 'complex_attributes'):
            assets['import_item'][key] = _replace_copy_values(assets['import_item'].get(key, []), before, after)
    return result


def accept_prepared(session, job, store_id, revision, result):
    publication, child = _target(session, job, store_id)
    keys = {'content', 'attributes', 'complex_attributes', 'assets', 'pricing', 'package'}
    if not isinstance(result, dict) or set(result) != keys or len(dumps(result).encode()) > 1024 * 1024:
        raise ConflictError('产品库准备回执格式或大小无效')
    step = next((row for row in job.steps if row.key == f'{store_id}:attributes'), None)
    if step is None:
        raise ConflictError('本次任务缺少属性准备步骤')
    receipt = loads(step.details_json, {})
    original_assets = loads(child.assets_json, {})
    review = original_assets.get('personal_review') or {}
    result = _apply_copy_revision(result, review)
    result_hash = digest(result)
    if step.status == 'completed':
        if receipt.get('revision') == revision and receipt.get('result_hash') == result_hash:
            return {'store_id': store_id, 'result_hash': result_hash, 'status': child.status}
        raise ConflictError('该店铺已经回传不同准备结果')
    if revision != _revision(publication, child):
        raise ConflictError('产品资料或店铺配置已变化，拒绝过期准备结果')
    _public_result(result)
    content, assets, pricing, package = (result[key] for key in ('content', 'assets', 'pricing', 'package'))
    if not all(isinstance(value, dict) for value in (content, assets, pricing, package)):
        raise ConflictError('文案、图片或定价格式无效')
    if not content.get('title') or not content.get('description_ru') or content.get('generated_fresh') is not True:
        raise ConflictError('每次新上架必须重新生成店铺文案')
    if not isinstance(result['attributes'], list) or not isinstance(result['complex_attributes'], list):
        raise ConflictError('商品属性格式无效')
    if assets.get('package_ready') is not True or not isinstance(assets.get('import_item'), dict):
        raise ConflictError('发布包尚未完成')
    facts = loads(publication.product_snapshot_json, {})
    requirements = loads(publication.requirements_json, {})
    if validate_product_facts(facts, requirements):
        raise ConflictError('产品资料仍不完整')
    expected_price = _price(resolve_publication_snapshot(child.settings_snapshot_json), facts, requirements)
    if pricing != expected_price:
        raise ConflictError('准备结果修改了原定价规则')
    expected_package = {'depth_mm': facts['package_length_mm'], 'width_mm': facts['package_width_mm'],
                        'height_mm': facts['package_height_mm'], 'weight_g': facts['package_weight_g']}
    if package != expected_package:
        raise ConflictError('准备结果修改了产品包装规格')
    item = assets['import_item']
    if item.get('offer_id') != child.offer_id or str(item.get('price')) != str(pricing['price']):
        raise ConflictError('发布包货号或价格与任务不一致')
    if item.get('currency_code') != 'CNY' or str(item.get('old_price')) != str(pricing['old_price']):
        raise ConflictError('发布包币种或原价与任务不一致')
    if (item.get('dimension_unit') != 'mm' or item.get('weight_unit') != 'g'
            or any(item.get(key) != expected_package[source] for key, source in
                   {'depth': 'depth_mm', 'width': 'width_mm', 'height': 'height_mm', 'weight': 'weight_g'}.items())):
        raise ConflictError('发布包包装规格与产品不一致')
    images = [assets.get('primary_image'), *(assets.get('images') or [])]
    if any(not isinstance(url, str) or not url.startswith('https://') for url in images):
        raise ConflictError('图片尚未全部入云')
    if (item.get('description_category_id') != int(requirements.get('description_category_id') or facts['description_category_id'])
            or item.get('type_id') != int(requirements.get('type_id') or facts['type_id'])
            or item.get('primary_image') != images[0] or item.get('images') != images[1:]):
        raise ConflictError('发布包类目或图片与原任务不一致')
    if not isinstance(assets.get('ai_preparation', {}), dict):
        raise ConflictError('图片准备状态格式无效')
    if _main_generation_requested(loads(publication.image_policy_json, {})):
        if (assets.get('ai_preparation') or {}).get('image_review_status') != 'APPROVED':
            raise ConflictError('新主图仍待人工核验')
    if (assets.get('ai_preparation') or {}).get('image_review_status') == 'APPROVED':
        original_assets = loads(child.assets_json, {})
        original = original_assets.get('ai_preparation') or {}
        review = original_assets.get('personal_review') or {}
        approved = review.get('human_approval') or {}
        if original.get('image_review_status') != 'APPROVED' and (
                approved.get('actor_id') != job.created_by or approved.get('sha256') != assets.get('approved_candidate_sha256')
                or review.get('revision') != revision):
            raise ConflictError('执行端不能代替人工批准主图')
    prepare_binding=original_assets.get('personal_prepare_job_id')
    if prepare_binding:result={**result,'assets':{**result['assets'],'personal_prepare_job_id':prepare_binding}}
    if review:result={**result,'assets':{**result['assets'],'personal_review':review}}
    for key in keys:
        setattr(child, key + '_json', dumps(result[key]))
    if publication.save_generated_to_catalog:
        from erp.services.publications import save_store_images_to_catalog
        save_store_images_to_catalog(session, publication, child)
    child.status = 'ready_for_submit' if publication.submit_via_api else 'ready_for_export'
    child.last_error = ''
    for row in job.steps:
        if row.key.startswith(store_id + ':') and row.key.split(':')[-1] in {'validate', 'ai_content', 'ai_images', 'attributes', 'oss_upload'}:
            row.status = 'completed'; row.progress = 100
            row.details_json = dumps({'revision': revision, 'result_hash': result_hash})
    publication.status = publication_status(publication.stores)
    return {'store_id': store_id, 'result_hash': result_hash, 'status': child.status}


def completion_summary(job):
    receipts = {}
    for step in job.steps:
        if not step.key.endswith(':attributes'):
            continue
        value = loads(step.details_json, {})
        if (step.status != 'completed' or not re.fullmatch('[a-f0-9]{64}', str(value.get('result_hash') or ''))
                or not re.fullmatch('[a-f0-9]{64}', str(value.get('revision') or ''))):
            raise ConflictError('产品库 Ozon 准备仍有店铺未回传')
        receipts[step.key.removesuffix(':attributes')] = value['result_hash']
    if not receipts:
        raise ConflictError('本次任务没有准备结果回执')
    return {'schema': 1, 'stores': receipts}


def targets(session, job):
    payload = loads(job.payload_json, {})
    publication = get_publication(session, str(payload.get('publication_id') or ''))
    requested = payload.get('store_ids') or [row.store_id for row in publication.stores]
    stores = []
    for identity in requested:
        child=next((row for row in publication.stores if row.store_id==identity),None)
        if child and (child.external_task_id or child.status in {'submit_queued','import_uncertain','import_submitting','import_pending','import_submitted','stock_pending','promotion_pending','completed'}):continue
        _target(session, job, identity)
        stores.append(identity)
    if not stores:
        raise ConflictError('准备任务没有目标店铺')
    return stores


def finish_preparation(session, job, receipts):
    from erp.models import utc_now
    expected = {}
    waiting_review = False
    export_only = True
    for store_id in targets(session, job):
        publication, child = _target(session, job, store_id)
        review = (loads(child.assets_json, {}).get('personal_review') or {})
        if child.status == 'image_review' and review.get('candidate_hash'):
            expected[store_id] = {'status': 'image_review', 'candidate_hash': review['candidate_hash']}
            waiting_review = True
        elif child.status in {'ready_for_submit', 'ready_for_export'}:
            step = next((row for row in job.steps if row.key == store_id + ':attributes'), None)
            if not step or step.status != 'completed':
                raise ConflictError('准备完成但缺少逐店回执')
            expected[store_id] = {'status': child.status, 'result_hash': loads(step.details_json, {}).get('result_hash')}
            export_only = export_only and child.status == 'ready_for_export'
        else:
            raise ConflictError('仍有目标店铺未完成准备或待核验回传')
    if receipts != {'schema': 1, 'stores': expected}:
        raise ConflictError('准备阶段完成清单与逐店回执不一致')
    prior = loads(job.external_ref, {})
    if prior.get('kind') == 'catalog_ozon_prepared' and prior.get('finished_hash') == digest(receipts):
        return {'status': job.status}
    job.status = 'paused' if waiting_review else ('completed' if export_only else 'action_required')
    job.pause_requested = waiting_review
    job.error_code = ''; job.error_message = ''
    job.lease_owner = ''; job.lease_expires_at = None
    job.finished_at = None if waiting_review else utc_now()
    job.external_ref = dumps({'kind': 'catalog_ozon_prepared', 'finished_hash': digest(receipts)})
    return {'status': job.status}
