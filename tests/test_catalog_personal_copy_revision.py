import copy

import pytest

from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps, loads
from erp.services import catalog_personal_publications as bridge
from test_catalog_personal_publications import prepared


def _pending(prepared):
    session, job, publication, child, result = prepared
    publication.image_policy_json = dumps({'mode': 'regenerate_main'})
    publication.requirements_json = dumps({'price': 100, 'old_price': 125, 'generation_provider': 'codex'})
    result['content']['tags'] = ['#цветы']
    candidate = {'sha256': 'a' * 64, 'url': 'https://oss.test/candidate.jpg',
                 'content': result['content'], 'attributes': [], 'complex_attributes': [],
                 'assets': {'ai_preparation': {'producer': 'codex-desktop',
                    'image_review_status': 'PENDING', 'content': {'title_ru': result['content']['title'],
                    'description_ru': result['content']['description_ru'], 'tags_ru': result['content']['tags']}}}}
    revision = bridge._revision(publication, child)
    bridge.accept_review_candidate(session, job, child.store_id, revision, candidate)
    job.status = 'paused'
    review = loads(child.assets_json, {})['personal_review']
    before = loads(child.content_json, {})
    return revision, review, before, result


def test_copy_revision_cas_and_resume_overlay(prepared):
    session, job, publication, child, _ = prepared
    revision, review, before, result = _pending(prepared)
    corrected = {'title': 'Павлин среди пионов', 'description_ru': 'На рисунке павлин среди белых пионов.',
                 'tags': ['#павлин', '#белые_пионы'], 'tags_shortfall_reason': 'Других подтверждённых тем нет.'}
    args = (session, job, child.store_id, job.created_by, job.target_worker_id,
            revision, review['candidate_hash'], bridge.digest({k: before[k] for k in ('title','description_ru','tags')}), corrected)
    with pytest.raises(ConflictError):
        bridge.revise_pending_copy(*args[:6], 'b' * 64, *args[7:])
    with pytest.raises(ConflictError):
        bridge.revise_pending_copy(session, job, child.store_id, 'other', job.target_worker_id, *args[5:])
    with pytest.raises(ConflictError):
        bridge.revise_pending_copy(session, job, child.store_id, job.created_by, 'other-pc', *args[5:])
    bridge.revise_pending_copy(*args)
    assets = loads(child.assets_json, {})
    assert assets['personal_review']['candidate_hash'] == review['candidate_hash']
    assert assets['personal_review']['copy_revision']['before']['title'] == before['title']
    assert assets['ai_preparation']['content']['tags_shortfall_reason'] == corrected['tags_shortfall_reason']
    with pytest.raises(ConflictError):
        bridge.revise_pending_copy(*args)
    # Original computer resumes from its immutable old candidate after human approval.
    bridge.approve_review_candidate(session, job, child.store_id, job.created_by)
    stale = copy.deepcopy(result)
    stale['assets'].update({'primary_image': 'https://oss.test/main.jpg', 'images': [],
        'package_ready': True, 'approved_candidate_sha256': 'a' * 64,
        'ai_preparation': {'image_review_status': 'APPROVED',
                           'content': {'title_ru': before['title'], 'description_ru': before['description_ru'],
                                       'tags_ru': before['tags']}}})
    stale['assets']['import_item']['name'] = before['title']
    stale['attributes'] = [{'id': 1, 'values': [{'value': before['description_ru']}]}]
    stale['assets']['import_item']['attributes'] = copy.deepcopy(stale['attributes'])
    receipt = bridge.accept_prepared(session, job, child.store_id, revision, stale)
    assert receipt['status'] == 'ready_for_submit'
    assert loads(child.content_json, {})['title'] == corrected['title']
    saved = loads(child.assets_json, {})
    assert saved['import_item']['name'] == corrected['title']
    assert saved['import_item']['attributes'][0]['values'][0]['value'] == corrected['description_ru']
    assert saved['ai_preparation']['content']['description_ru'] == corrected['description_ru']
    assert saved['ai_preparation']['content']['tags_shortfall_reason'] == corrected['tags_shortfall_reason']
    assert saved['personal_review']['copy_revision']['after'] == corrected
    assert job.steps[5].status == 'pending'


def test_copy_revision_requires_pending_unapproved_candidate(prepared):
    session, job, publication, child, _ = prepared
    revision, review, before, result = _pending(prepared)
    corrected = {'title': 'Другой заголовок', 'description_ru': 'Другое описание',
                 'tags': ['#декор'], 'tags_shortfall_reason': 'Других тем нет.'}
    args = (session, job, child.store_id, job.created_by, job.target_worker_id,
            revision, review['candidate_hash'], bridge.digest({k: before[k] for k in ('title','description_ru','tags')}), corrected)
    job.status = 'running'
    with pytest.raises(ConflictError): bridge.revise_pending_copy(*args)
    job.status = 'paused'
    bridge.approve_review_candidate(session, job, child.store_id, job.created_by)
    with pytest.raises(ConflictError): bridge.revise_pending_copy(*args)


@pytest.mark.parametrize('mutation', ['provider', 'producer', 'image_status', 'receipt', 'shortfall', 'latin_tag', 'duplicate'])
def test_copy_revision_rejects_wrong_scope_or_bad_copy(prepared, mutation):
    session, job, publication, child, _ = prepared
    revision, review, before, result = _pending(prepared)
    corrected = {'title': 'Павлин среди пионов', 'description_ru': 'Павлин среди цветов.',
                 'tags': ['#павлин', '#цветы'], 'tags_shortfall_reason': 'Других тем нет.'}
    if mutation == 'provider': publication.requirements_json = dumps({'generation_provider': 'external'})
    if mutation in {'producer', 'image_status', 'receipt'}:
        assets = loads(child.assets_json, {})
        if mutation == 'producer': assets['ai_preparation']['producer'] = 'external'
        if mutation == 'image_status': assets['ai_preparation']['image_review_status'] = 'APPROVED'
        if mutation == 'receipt': assets['personal_review'].pop('sha256')
        child.assets_json = dumps(assets)
    if mutation == 'shortfall': corrected['tags_shortfall_reason'] = ''
    if mutation == 'latin_tag': corrected['tags'] = ['#flower']
    if mutation == 'duplicate': corrected['tags'] = ['#цветы', '#цветы']
    with pytest.raises(ConflictError):
        bridge.revise_pending_copy(session, job, child.store_id, job.created_by, job.target_worker_id,
            revision, review['candidate_hash'], bridge.digest({k: before[k] for k in ('title','description_ru','tags')}), corrected)
