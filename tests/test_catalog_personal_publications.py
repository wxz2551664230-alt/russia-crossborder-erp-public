import copy
from types import SimpleNamespace as Obj
import pytest
from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps
from erp.services import catalog_personal_publications as bridge


@pytest.fixture
def prepared(monkeypatch):
    facts = {'name': '产品', 'sku': 'SKU', 'purchase_price_cny': 10, 'package_length_mm': 100,
             'package_width_mm': 200, 'package_height_mm': 30, 'package_weight_g': 250,
             'description_category_id': 1, 'type_id': 2, 'assets': [{'oss_url': 'https://oss.test/main.jpg'}]}
    child = Obj(id='child', store_id='store', store=Obj(name='店铺'), offer_id='PREFIX-SKU',
                external_task_id='', status='queued', settings_snapshot_json=dumps({'public': {}}),
                assets_json='{}', content_json='{}', attributes_json='[]', complex_attributes_json='[]',
                pricing_json='{}', package_json='{}')
    publication = Obj(id='publication', product_id='product', job_id='job', stores=[child],
        product_snapshot_json=dumps(facts), requirements_json=dumps({'price': 100, 'old_price': 125}),
        image_policy_json=dumps({'mode': 'reuse'}), save_generated_to_catalog=False, submit_via_api=True)
    steps = [Obj(key='store:' + key, status='pending', progress=0, details_json='{}') for key in
             ('validate', 'ai_content', 'ai_images', 'attributes', 'oss_upload', 'submit', 'stock', 'promotion')]
    job = Obj(id='job', type='catalog_ozon_prepare', created_by='owner', target_worker_id='pc',
              payload_json=dumps({'publication_id': 'publication'}), steps=steps)
    monkeypatch.setattr(bridge, 'get_publication', lambda session, identity: publication)
    session = Obj(scalars=lambda query: Obj(all=lambda: []))
    price = bridge._price({}, facts, {'price': 100, 'old_price': 125})
    package = {'depth_mm': 100, 'width_mm': 200, 'height_mm': 30, 'weight_g': 250}
    result = {'content': {'title': 'Новый заголовок', 'description_ru': 'Новое описание', 'generated_fresh': True},
        'attributes': [], 'complex_attributes': [], 'pricing': price, 'package': package,
        'assets': {'primary_image': 'https://oss.test/main.jpg', 'images': [], 'package_ready': True,
            'import_item': {'offer_id': 'PREFIX-SKU', 'price': '100', 'old_price': '125', 'currency_code': 'CNY',
                'depth': 100, 'width': 200, 'height': 30, 'weight': 250, 'dimension_unit': 'mm', 'weight_unit': 'g',
                'description_category_id': 1, 'type_id': 2, 'primary_image': 'https://oss.test/main.jpg', 'images': []}}}
    return session, job, publication, child, result


def test_selected_snapshot_and_idempotent_receipt(prepared):
    session, job, publication, child, result = prepared
    value = bridge.snapshot(session, job, 'store')
    assert value['facts']['sku'] == 'SKU'
    assert value['prior_copy_for_difference_check'] == []
    receipt = bridge.accept_prepared(session, job, 'store', value['revision'], result)
    assert receipt['status'] == 'ready_for_submit'
    assert child.external_task_id == ''
    assert job.steps[5].status == 'pending'
    assert bridge.accept_prepared(session, job, 'store', value['revision'], result) == receipt
    changed = copy.deepcopy(result); changed['content']['title'] = 'Different'
    with pytest.raises(ConflictError):
        bridge.accept_prepared(session, job, 'store', value['revision'], changed)


@pytest.mark.parametrize('mutation', ['sku', 'price', 'dimensions', 'local', 'secret', 'approval'])
def test_receipt_cannot_change_business_facts_or_approve_image(prepared, mutation):
    session, job, publication, child, result = prepared
    revision = bridge.snapshot(session, job, 'store')['revision']
    if mutation == 'sku': result['assets']['import_item']['offer_id'] = 'OTHER'
    if mutation == 'price': result['pricing']['price'] = '1'
    if mutation == 'dimensions': result['assets']['import_item']['weight'] = 1
    if mutation == 'local': result['assets']['ai_preparation'] = {'generated_image_path': 'D:/private/file.jpg'}
    if mutation == 'secret': result['assets']['ai_preparation'] = {'api_key': 'private'}
    if mutation == 'approval': result['assets']['ai_preparation'] = {'image_review_status': 'APPROVED'}
    with pytest.raises(ConflictError): bridge.accept_prepared(session, job, 'store', revision, result)
    assert child.status == 'queued' and child.content_json == '{}'


def test_old_revision_and_wrong_store_are_rejected(prepared):
    session, job, publication, child, result = prepared
    revision = bridge.snapshot(session, job, 'store')['revision']
    publication.requirements_json = dumps({'price': 200})
    with pytest.raises(ConflictError): bridge.accept_prepared(session, job, 'store', revision, result)
    with pytest.raises(ConflictError): bridge.snapshot(session, job, 'other')
    job.created_by = ''
    with pytest.raises(ConflictError): bridge.snapshot(session, job, 'store')


def test_new_image_requires_original_human_approval(prepared):
    session, job, publication, child, result = prepared
    publication.image_policy_json = dumps({'mode': 'regenerate_main'})
    with pytest.raises(ConflictError):
        bridge.accept_prepared(session, job, 'store', bridge._revision(publication, child), result)


def test_finish_preparation_preserves_manual_submit_boundary(prepared):
    session, job, publication, child, result = prepared
    receipt = bridge.accept_prepared(session, job, 'store', bridge._revision(publication, child), result)
    summary = {'schema': 1, 'stores': {'store': {'status': receipt['status'], 'result_hash': receipt['result_hash']}}}
    job.external_ref = ''
    assert bridge.finish_preparation(session, job, summary) == {'status': 'action_required'}
    assert job.steps[5].status == 'pending'
    assert child.external_task_id == ''
    assert bridge.finish_preparation(session, job, summary) == {'status': 'action_required'}
    summary['stores']['store']['result_hash'] = 'wrong'
    with pytest.raises(ConflictError): bridge.finish_preparation(session, job, summary)


def test_explicit_gallery_writeback_only_after_valid_ready_receipt(prepared, monkeypatch):
    from erp.services import publications
    session, job, publication, child, result = prepared
    publication.save_generated_to_catalog = True
    saved = []
    monkeypatch.setattr(publications, 'save_store_images_to_catalog', lambda s, p, c: saved.append(c.store_id))
    revision = bridge._revision(publication, child)
    bridge.accept_prepared(session, job, 'store', revision, result)
    bridge.accept_prepared(session, job, 'store', revision, result)
    assert saved == ['store']


def test_preparation_stays_bound_while_other_store_imports(prepared):
    import copy
    session,job,publication,child,result=prepared
    child.assets_json=dumps({'personal_prepare_job_id':job.id})
    other=copy.deepcopy(child);other.id='other';other.store_id='other-store';other.status='stock_pending';other.external_task_id='7'
    publication.stores.append(other);publication.job_id='other-store-import-job'
    assert bridge.targets(session,job)==[child.store_id]
    snapshot=bridge.snapshot(session,job,child.store_id)
    bridge.accept_prepared(session,job,child.store_id,snapshot['revision'],result)
    assert __import__('json').loads(child.assets_json)['personal_prepare_job_id']==job.id
    assert bridge.accept_prepared(session,job,child.store_id,snapshot['revision'],result)['status']=='ready_for_submit'
