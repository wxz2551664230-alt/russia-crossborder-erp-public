import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
from catalog_ozon_worker import codex_binding, codex_content_preparer, normalize_codex_attributes


@pytest.mark.parametrize('suggestions', [None, []])
def test_empty_codex_attributes(suggestions):
    assert normalize_codex_attributes(suggestions) == []


@pytest.mark.parametrize('row', [
    {'attribute_id': 8229, 'value': 'Скатерть'},
    {'id': 8229, 'value': 'Скатерть'},
    {'attribute_id': 8229, 'id': 8229, 'value': 'Скатерть'},
])
def test_codex_attribute_alias_is_in_memory_only(row):
    import copy
    original = copy.deepcopy(row)
    assert normalize_codex_attributes([row]) == [{'attribute_id': 8229, 'value': 'Скатерть'}]
    assert row == original


@pytest.mark.parametrize('suggestions', [
    {}, '', False, [None], [[]], [{}],
    [{'attribute_id': 1, 'id': 2, 'value': 'x'}],
    *[[{'id': value, 'value': 'x'}] for value in (0, -1, True, 1.0, '1', None)],
    *[[{'attribute_id': 1, 'value': value}] for value in (None, '', '  ', 1, [], {})],
    [{'attribute_id': 1, 'id': True, 'value': 'x'}],
])
def test_invalid_codex_attributes_rejected(suggestions):
    with pytest.raises(ValueError):
        normalize_codex_attributes(suggestions)


def test_attribute_alias_reaches_original_consumer_without_mutating_record(monkeypatch):
    import copy
    from erp.modules.ozon_follow.legacy.services import ai_listing
    snapshot, record = fixture()
    record['content']['attributes'] = [{'id': 8229, 'value': 'Скатерть'}]
    original = copy.deepcopy(record)
    seen = []
    monkeypatch.setattr(ai_listing, 'apply_ai_attributes',
                        lambda draft, definitions, suggestions, client: seen.extend(suggestions) or {'applied': []})
    for name in ('apply_content_attributes', 'enforce_system_attributes', 'enforce_known_fact_attributes'):
        monkeypatch.setattr(ai_listing, name, lambda *args: [])
    draft = SimpleNamespace(ai_preparation={}, primary_image='https://original', images=[])
    codex_content_preparer(record, snapshot, lambda: None)(
        config=None, product={}, draft=draft, definitions=[], ozon_client=None,
        sourcing={}, generate_image=False)
    assert seen == [{'attribute_id': 8229, 'value': 'Скатерть'}]
    assert record == original
    assert draft.ai_preparation['content']['attributes'] == original['content']['attributes']


def fixture():
    snapshot = dict(job_id='job', publication_id='pub', store_id='store', offer_id='offer',
                    facts={'sku': 'sku'}, requirements={}, image_policy={'mode': 'reuse'},
                    prior_copy_for_difference_check=[])
    record = dict(binding=codex_binding(snapshot), producer='codex-desktop',
                  content={'title_ru': 'Название', 'description_ru': 'Новое описание', 'tags_ru': []})
    return snapshot, record


def test_changed_facts_and_historical_copy_rejected():
    snapshot, record = fixture()
    snapshot['facts']['sku'] = 'other'
    with pytest.raises(ValueError): codex_content_preparer(record, snapshot, lambda: None)
    snapshot, record = fixture()
    snapshot['prior_copy_for_difference_check'] = [{'description_ru': 'Новое описание'}]
    with pytest.raises(ValueError): codex_content_preparer(record, snapshot, lambda: None)


def test_local_copy_runs_original_attribute_enforcers_and_checkpoint(monkeypatch):
    from erp.modules.ozon_follow.legacy.services import ai_listing
    snapshot, record = fixture(); calls=[]
    for name in ('apply_content_attributes', 'enforce_system_attributes', 'enforce_known_fact_attributes'):
        monkeypatch.setattr(ai_listing, name, lambda *a, _name=name: calls.append(_name) or [])
    monkeypatch.setattr(ai_listing, 'apply_ai_attributes', lambda *a: {'applied': []})
    draft=SimpleNamespace(ai_preparation={}, primary_image='https://image', images=[])
    prepare=codex_content_preparer(record,snapshot,lambda: calls.append('lease'))
    kwargs=dict(config=None, product={}, draft=draft, definitions=[], ozon_client=None,
                sourcing={}, on_content_checkpoint=lambda: calls.append('checkpoint'))
    with pytest.raises(ValueError): prepare(**kwargs, generate_image=True)
    prepare(**kwargs, generate_image=False)
    assert draft.description_ru == 'Новое описание'
    assert draft.ai_preparation['producer'] == 'codex-desktop'
    assert 'enforce_system_attributes' in calls and calls[-1] == 'checkpoint'


def test_codex_main_and_additional_images_remain_pending_review(monkeypatch,tmp_path):
    import hashlib
    from PIL import Image
    from erp.modules.ozon_follow.legacy.services import ai_listing
    from erp.services import publications
    snapshot,record=fixture()
    snapshot['image_policy']={'mode':'regenerate_all'}
    record['binding']=codex_binding(snapshot)
    path=tmp_path/'generated.png';Image.new('RGB',(60,80),'green').save(path)
    image={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'matches_sku':True,'check_notes':'隔离测试图片'}
    record.update(main_candidate=image,additional_candidates=[image,image,image])
    uploaded=[]
    monkeypatch.setattr(publications,'OssUploader',lambda config:SimpleNamespace(upload=lambda *args:uploaded.append(args) or 'https://oss.test/'+args[-1]))
    for name in ('apply_content_attributes','enforce_system_attributes','enforce_known_fact_attributes'):
        monkeypatch.setattr(ai_listing,name,lambda *args:[])
    monkeypatch.setattr(ai_listing,'apply_ai_attributes',lambda *args:{'applied':[]})
    draft=SimpleNamespace(ai_preparation={},primary_image='https://original',images=[])
    codex_content_preparer(record,snapshot,lambda:None)(config=None,product={},draft=draft,definitions=[],ozon_client=None,sourcing={})
    assert len(uploaded)==3 and len(draft.images)==3
    assert draft.ai_preparation['image_review_status']=='PENDING'
    assert draft.ai_preparation['generated_image_path']==str(path)
    path.write_bytes(b'changed')
    with pytest.raises(ValueError):
        codex_content_preparer(record,snapshot,lambda:None)(config=None,product={},draft=draft,definitions=[],ozon_client=None,sourcing={})
