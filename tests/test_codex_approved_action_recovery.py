from copy import deepcopy
from pathlib import Path
import pytest


@pytest.mark.parametrize('change', ['none', 'approval', 'sha', 'candidate', 'revision', 'action', 'new_action'])
def test_completed_image_action_requires_original_approval(monkeypatch, change):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
    from catalog_ozon_worker import approved_codex_action_matches
    record = {'image_action_id': 'repair', 'main_candidate': {'sha256': 'image'}}
    snapshot = {'revision': 'original', 'resume': {'assets': {'personal_image_action_id': 'repair'}}}
    review = {'revision': 'original', 'sha256': 'image', 'candidate_hash': 'candidate',
              'human_approval': {'sha256': 'image', 'candidate_hash': 'candidate'}}
    if change == 'approval': review['human_approval'] = None
    elif change == 'sha': record['main_candidate']['sha256'] = 'different-image'
    elif change == 'candidate': review['human_approval']['candidate_hash'] = 'different-candidate'
    elif change == 'revision': review['revision'] = 'changed'
    elif change == 'action': snapshot['resume']['assets']['personal_image_action_id'] = 'other'
    elif change == 'new_action': review['image_action'] = {'id': 'new-request'}
    assert approved_codex_action_matches(record, snapshot, review) is (change == 'none')
    assert not approved_codex_action_matches(None, snapshot, review)
