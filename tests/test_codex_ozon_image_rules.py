import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'client/desktop'))
from ozon_image_rules import main_prompt,validate_image,export_rules

def content():
    return {'poster_text_lines_ru':['Скатерть','Размер 137 × 183 см'],'creative_direction_en':'A layered dining scene with bold Cyrillic headline and factual size badge.'}

def test_same_original_poster_prompt_and_store_direction():
    from erp.modules.ozon_follow.legacy.services.ai_listing import build_main_image_prompt,store_creative_profile
    value={**content(),'store_creative_profile':store_creative_profile(1)}
    assert main_prompt(content(),1)==build_main_image_prompt(value)
    prompt=main_prompt(content(),1)
    assert 'vertical 3:4 canvas' in prompt and 'VISIBLE RUSSIAN TEXT' in prompt
    assert '50-72%' in prompt and 'Скатерть' in prompt
    assert export_rules({'store_index':1})['store_creative_profile']==store_creative_profile(1)

@pytest.mark.parametrize('size',[(1000,1000),(1024,1536),(1200,900)])
def test_wrong_aspect_rejected(size):
    with pytest.raises(ValueError):validate_image({},size)

def test_aspect_and_actual_poster_check_required():
    lines=content()['poster_text_lines_ru']
    with pytest.raises(ValueError):validate_image({},(900,1200),lines)
    row={'poster_layout_verified':True,'russian_text_verified':True,'visible_text_lines_ru':lines}
    validate_image(row,(900,1200),lines)
    row['visible_text_lines_ru']=['Другой текст']
    with pytest.raises(ValueError):validate_image(row,(900,1200),lines)

def test_real_image_file_gate_and_reuse_exception(tmp_path):
    from PIL import Image
    from codex_bridge import image_record
    path=tmp_path/'square.png';Image.new('RGB',(90,90),'white').save(path)
    row={'path':str(path),'matches_sku':True,'check_notes':'隔离图像尺寸验证'}
    with pytest.raises(ValueError):image_record(row,tmp_path/'work','new.png')
    # Existing images are retained unchanged when the user chose reuse.
    image_record(row,tmp_path/'work','reused.png',generated=False)

def test_user_approved_square_batch_still_checks_shape_and_poster(tmp_path):
    from PIL import Image
    from codex_bridge import image_record
    path=tmp_path/'square.png';Image.new('RGB',(90,90),'white').save(path)
    lines=['СКАТЕРТЬ','140 × 180 см']
    row={'path':str(path),'matches_sku':True,'check_notes':'Compared with the selected SKU image',
         'poster_layout_verified':True,'russian_text_verified':True,'visible_text_lines_ru':lines}
    image_record(row,tmp_path/'work','square.png',poster_lines=lines,aspect_ratio='1:1')
    row['visible_text_lines_ru']=['Неверный текст']
    with pytest.raises(ValueError):image_record(row,tmp_path/'work','other.png',poster_lines=lines,aspect_ratio='1:1')
    with pytest.raises(ValueError):validate_image({},(900,1200),aspect_ratio='1:1')

def test_old_client_cannot_create_new_codex_image_job(monkeypatch):
    from erp.api.routes import publications
    from erp.core.database import SessionLocal
    from erp.core.task_identity import task_identity
    from erp.core.errors import ConflictError
    from erp.models import WorkerNode
    from erp.schemas import PublicationCreate
    from erp.core.jsonutil import dumps
    settings=publications.get_settings().model_copy(update={'multiuser_enabled':True})
    monkeypatch.setattr(publications,'get_settings',lambda:settings)
    token=task_identity.set({'id':'owner','worker_id':'pc'})
    try:
        with SessionLocal() as session:
            session.add(WorkerNode(id='pc',owner_user_id='owner',capabilities_json=dumps(['codex_handoff_v1'])));session.commit()
            with pytest.raises(ConflictError,match='0.6.68'):
                publications.create_publications(PublicationCreate(product_ids=['p'],store_ids=['s'],generation_provider='codex',image_policy='regenerate_main'),session)
    finally:task_identity.reset(token)

@pytest.mark.parametrize('mutation',['missing_lines','missing_direction','chinese','long'])
def test_missing_or_invalid_poster_copy_rejected(mutation):
    value=content()
    if mutation=='missing_lines':value['poster_text_lines_ru']=[]
    if mutation=='missing_direction':value['creative_direction_en']=''
    if mutation=='chinese':value['poster_text_lines_ru']=['俄文']
    if mutation=='long':value['poster_text_lines_ru']=['Я'*69]
    with pytest.raises(ValueError):main_prompt(value)
