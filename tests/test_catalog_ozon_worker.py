from pathlib import Path
import pytest
from cryptography.fernet import Fernet
from test_catalog_personal_publications import prepared
from erp.services import publications as original
from erp.services import catalog_personal_publications as bridge
from erp.core.jsonutil import dumps, loads


def test_original_image_resume_preserves_same_task_copy_and_attributes(prepared, monkeypatch):
    from types import SimpleNamespace as Obj
    from erp.core.errors import AppError
    session, job, publication, child, expected = prepared
    publication.image_policy_json = dumps({'mode': 'regenerate_main'})
    child.content_json = dumps(expected['content'])
    child.attributes_json = dumps([{'id': 10, 'values': [{'value': 'saved'}]}])
    child.assets_json = dumps({'ai_preparation': {'image_stage_status': 'PENDING',
        'content': {'title_ru': 'saved title', 'description_ru': 'saved description'}, 'visual_analysis': {}}})
    session.commit = lambda: None
    monkeypatch.setattr(original, 'OzonSellerClient', lambda *args, **kwargs: Obj(category_attributes=lambda *args: []))
    monkeypatch.setattr(original, 'ProductRepository', lambda: None)
    def resume(**kwargs):
        draft = kwargs['draft']
        assert draft.description_ru == expected['content']['description_ru']
        assert draft.attributes == [{'id': 10, 'values': [{'value': 'saved'}]}]
        draft.ai_preparation['image_review_status'] = 'PENDING'
    monkeypatch.setattr(original, 'prepare_main_image_from_checkpoint', resume)
    context = Obj(session=session, start_step=lambda *args: None,
                  complete_step=lambda *args, **kwargs: None, event=lambda *args: None)
    with pytest.raises(AppError) as error:
        original.prepare_store_package(context, publication, child, {})
    assert error.value.code == 'publication_image_review_required'
    assert loads(child.content_json, {})['description_ru'] == expected['content']['description_ru']


def test_empty_task_runtime_reaches_ai_sender_without_business_database(tmp_path):
    import os
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    environment = {**os.environ, 'ERP_DATA_DIR': str(tmp_path),
                   'ERP_DATABASE_URL': 'sqlite:///' + (tmp_path / 'unused.db').as_posix(),
                   'ERP_MULTIUSER_ENABLED': 'false', 'ERP_RUNTIME_MODE': 'worker',
                   'PYTHONPATH': os.pathsep.join([str(root / 'client/desktop'), str(root / 'backend')])}
    script = '''
import sqlite3
import requests
from catalog_ozon_worker import initialize_ai_runtime
from erp.core.ai_concurrency import post
initialize_ai_runtime()
calls = []
def sender(*args, **kwargs):
    calls.append(1)
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"usage": {}}'
    return response
assert post('text', sender, 'https://example.invalid').status_code == 200
assert calls == [1]
from erp.core.config import get_settings
tables = sqlite3.connect(get_settings().data_dir / 'unused.db').execute('select name from sqlite_master where type="table"').fetchall()
assert tables == [('system_settings',)]
'''
    result = subprocess.run([sys.executable, '-c', script], env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_original_prepare_on_pc_keeps_same_task_encrypted_resume(prepared, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from catalog_ozon_worker import prepare
    session, job, publication, child, expected = prepared
    value = bridge.snapshot(session, job, 'store')
    value['store_index'] = 2
    value['prior_copy_for_difference_check'] = [{'description_ru': 'Earlier different store copy'}]
    monkeypatch.setattr(original, 'OZON_WORK_DATA_DIR', tmp_path / 'ozon_follow')
    writes = []
    def original_prepare(context, publication, child, config):
        assert publication.personal_store_index == 2
        assert publication.personal_other_versions == value['prior_copy_for_difference_check']
        if not loads(child.assets_json, {}).get('package_ready'):
            writes.append('paid preparation simulation')
            for key, data in expected.items(): setattr(child, key + '_json', dumps(data))
            context.session.commit()
        return {'item': expected['assets']['import_item']}
    monkeypatch.setattr(original, 'prepare_store_package', original_prepare)
    cipher = Fernet(Fernet.generate_key())
    def protect(data, decrypt=False): return cipher.decrypt(data) if decrypt else cipher.encrypt(data)
    first = prepare(value, tmp_path, lambda: None, protect)
    second = prepare(value, tmp_path, lambda: None, protect)
    assert first == second == expected
    assert writes == ['paid preparation simulation']
    assert 'Новое описание'.encode() not in (tmp_path / 'preparation.dpapi').read_bytes()
    value['revision'] = 'different-task-revision'
    with pytest.raises(ValueError, match='禁止复用历史文案'):
        prepare(value, tmp_path, lambda: None, protect)
    assert len(writes) == 1


def test_wrong_runtime_directory_prevents_original_file_operations(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from catalog_ozon_worker import prepare
    monkeypatch.setattr(original, 'OZON_WORK_DATA_DIR', tmp_path / 'permanent-source')
    with pytest.raises(ValueError, match='运行目录未隔离'):
        prepare({'schema': 1}, tmp_path / 'job', lambda: None, lambda data: data)
