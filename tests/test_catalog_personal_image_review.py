import hashlib
from pathlib import Path
import pytest
from cryptography.fernet import Fernet
from erp.core.errors import AppError, ConflictError
from erp.core.jsonutil import dumps, loads
from erp.services import catalog_personal_publications as bridge, publications as original
from erp.modules.ozon_follow.legacy.services import ai_listing
from test_catalog_personal_publications import prepared


@pytest.mark.parametrize('regenerate', [False, True])
def test_candidate_human_approval_and_original_pc_resume(prepared, tmp_path, monkeypatch, regenerate):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from catalog_ozon_worker import prepare, public_result
    session, job, publication, child, expected = prepared
    publication.image_policy_json = dumps({'mode': 'regenerate_main'})
    monkeypatch.setattr(original, 'OZON_WORK_DATA_DIR', tmp_path / 'ozon_follow')
    candidate = tmp_path / 'candidate.jpg'; candidate.write_bytes(b'candidate-image-exact-bytes')
    billed = []
    def original_prepare(context, publication, child, config):
        assets = loads(child.assets_json, {})
        if (assets.get('ai_preparation') or {}).get('image_review_status') != 'APPROVED':
            if not assets.get('personal_image_action_id'): billed.append('simulated paid image')
            child.content_json = dumps(expected['content'])
            child.assets_json = dumps({'primary_image': '', 'images': [], 'ai_preparation': {
                'image_review_status': 'PENDING', 'generated_image_path': str(candidate)},
                **({'personal_image_action_id': assets['personal_image_action_id']} if assets.get('personal_image_action_id') else {})})
            context.session.commit()
            raise AppError('Wait human review', code='publication_image_review_required')
        for key, value in expected.items(): setattr(child, key + '_json', dumps(value))
        state = loads(child.assets_json, {})
        state['ai_preparation'] = {'image_review_status': 'APPROVED'}
        child.assets_json = dumps(state)
    monkeypatch.setattr(original, 'prepare_store_package', original_prepare)
    monkeypatch.setattr(ai_listing, 'pending_main_image_path', lambda draft: candidate)
    monkeypatch.setattr(original, 'OssUploader', lambda config: type('Uploader', (), {
        'upload': lambda *args: 'https://oss.test/review-candidate.jpg'})())
    monkeypatch.setattr(original, 'approve_pending_main_image', lambda **kwargs:
        kwargs['draft'].ai_preparation.update(image_review_status='APPROVED'))
    cipher = Fernet(Fernet.generate_key())
    def protect(data, decrypt=False): return cipher.decrypt(data) if decrypt else cipher.encrypt(data)
    snapshot = bridge.snapshot(session, job, child.store_id)
    pending = prepare(snapshot, tmp_path, lambda: None, protect)
    assert pending == prepare(snapshot, tmp_path, lambda: None, protect)
    assert len(billed) == 1
    assert str(candidate) not in dumps(pending)
    assert pending['image_review']['sha256'] == hashlib.sha256(candidate.read_bytes()).hexdigest()
    bridge.accept_review_candidate(session, job, child.store_id, snapshot['revision'], pending['image_review'])
    assert child.status == 'image_review'
    with pytest.raises(ConflictError): bridge.approve_review_candidate(session, job, child.store_id, 'other')
    if regenerate:
        actions=[]
        def review_again(session, publication, child, action):
            assert child.status == 'image_review' and action == 'repair'
            actions.append(action)
            candidate.write_bytes(b'new-candidate-after-repair')
            session.commit()
        monkeypatch.setattr(original, 'review_publication_image', review_again)
        action=bridge.request_image_regeneration(session, job, child.store_id, job.created_by, 'repair')
        next_snapshot=bridge.snapshot(session, job, child.store_id)
        new_pending=prepare(next_snapshot, tmp_path, lambda: None, protect)
        assert new_pending == prepare(next_snapshot, tmp_path, lambda: None, protect)
        assert actions == ['repair']
        assert new_pending['image_review']['sha256'] != pending['image_review']['sha256']
        assert new_pending['image_review']['assets']['personal_image_action_id'] == action['id']
        bridge.accept_review_candidate(session, job, child.store_id, snapshot['revision'], new_pending['image_review'])
    bridge.approve_review_candidate(session, job, child.store_id, job.created_by)
    resumed = prepare(bridge.snapshot(session, job, child.store_id), tmp_path, lambda: None, protect)
    receipt = bridge.accept_prepared(session, job, child.store_id, snapshot['revision'], public_result(resumed))
    assert receipt['status'] == 'ready_for_submit'
    assert len(billed) == 1


def test_candidate_mutation_after_approval_is_rejected(prepared):
    session, job, publication, child, expected = prepared
    publication.image_policy_json = dumps({'mode': 'regenerate_main'})
    revision = bridge._revision(publication, child)
    candidate = {'sha256': 'a' * 64, 'url': 'https://oss.test/candidate.jpg', 'content': expected['content'],
                 'attributes': [], 'complex_attributes': [], 'assets': {'ai_preparation': {'image_review_status': 'PENDING'}}}
    bridge.accept_review_candidate(session, job, child.store_id, revision, candidate)
    publication.requirements_json = dumps({'price': 200})
    with pytest.raises(ConflictError): bridge.approve_review_candidate(session, job, child.store_id, job.created_by)


def test_regeneration_requires_owner_and_new_candidate_action(prepared):
    session, job, publication, child, expected = prepared
    publication.image_policy_json=dumps({'mode':'regenerate_main'})
    revision=bridge._revision(publication,child)
    candidate={'sha256':'a'*64,'url':'https://oss.test/old.jpg','content':expected['content'],
        'attributes':[],'complex_attributes':[],'assets':{'ai_preparation':{'image_review_status':'PENDING'}}}
    bridge.accept_review_candidate(session,job,child.store_id,revision,candidate)
    with pytest.raises(ConflictError):bridge.request_image_regeneration(session,job,child.store_id,'other','repair')
    action=bridge.request_image_regeneration(session,job,child.store_id,job.created_by,'repair')
    assert child.status=='queued'
    with pytest.raises(ConflictError):bridge.approve_review_candidate(session,job,child.store_id,job.created_by)
    with pytest.raises(ConflictError):bridge.accept_review_candidate(session,job,child.store_id,revision,candidate)
    candidate['sha256']='b'*64;candidate['url']='https://oss.test/new.jpg'
    candidate['assets']['personal_image_action_id']=action['id']
    bridge.accept_review_candidate(session,job,child.store_id,revision,candidate)
    assert child.status=='image_review'
    assert loads(child.assets_json,{})['personal_review']['human_approval'] is None
    assert bridge.accept_review_candidate(session,job,child.store_id,revision,candidate)['status']=='image_review'
