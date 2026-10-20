"""US artwork entry uses cached facts and keeps domestic usage separate."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps, loads
from erp.domains.pod.models import PodArtwork, PodComposition, PodCompositionSide, PodSupplierCatalogSnapshot
from erp.models import Job
from test_personal_job_controls import team
from test_us_composition import seed


def record_usage(artwork_id, market=None, status='completed'):
    with SessionLocal() as db:
        art = db.get(PodArtwork, artwork_id)
        composition = PodComposition(supplier='s2b', status=status, product_type_id=7,
            product_name=f'{market or "CN"} pad', raw_json=dumps({'market': market} if market else {}))
        db.add(composition); db.flush()
        db.add(PodCompositionSide(composition_id=composition.id, artwork_id=art.id,
            asset_id=art.asset_id, supplier='s2b', view_id=1))
        db.commit()


def test_us_picker_reads_cache_searches_and_excludes_unverified_sources(team, monkeypatch):
    clients, worker = team; owner = clients['owner']; seed(owner, worker)
    from erp.domains.pod.integrations.s2b import S2BClient
    monkeypatch.setattr(S2BClient, '_request', lambda *a, **kw: pytest.fail('Picker must not call supplier'))
    with SessionLocal() as db:
        snapshot = db.scalar(select(PodSupplierCatalogSnapshot))
        data = loads(snapshot.payload_json, {})
        data['products'] += [
            {'supplier': 's2b', 'source_id': '8', 'catalog_key': 's2b:8', 'produce_country': 'US', 'name': 'US towel', 'code': 'TOWEL'},
            {'supplier': 's2b', 'source_id': '9', 'catalog_key': 's2b:9', 'name': 'Unverified'},
            {'supplier': 'hicustom', 'source_id': '10', 'catalog_key': 'hicustom:10', 'produce_country': 'US', 'name': 'Unverified supplier'},
        ]
        snapshot.payload_json = dumps(data)
        db.add(PodSupplierCatalogSnapshot(provider='all', is_current=True, payload_json=dumps({
            'products': [{'supplier': 's2b', 'id': 7, 'name': 'Domestic pad'}]})))
        db.commit()
    base = '/api/v1/pod/artworks/product-options'
    us = owner.get(base, params={'market': 'US'}).json()
    assert [x['key'] for x in us['items']] == ['s2b:7', 's2b:8']
    assert [x['detail_cached'] for x in us['items']] == [True, False]
    assert all(x['market'] == 'US' and '美国' in x['label'] for x in us['items'])
    assert owner.get(base, params={'market': 'US', 'search': 'TOWEL'}).json()['items'][0]['key'] == 's2b:8'
    domestic = owner.get(base).json()['items']
    assert len(domestic) == 1 and domestic[0]['name'] == 'Domestic pad' and domestic[0]['market'] == 'CN'
    assert owner.get(base, params={'market': 'CA'}).status_code == 422
    with SessionLocal() as db:
        assert db.query(Job).count() == 0


def test_used_artwork_filters_isolate_market_and_preserve_legacy_usage(team):
    clients, worker = team; owner = clients['owner']; request, _ = seed(owner, worker)
    artwork_id = request['placements'][0]['artwork_id']
    record_usage(artwork_id)  # Existing rows without a market remain domestic.
    base = '/api/v1/pod/artworks'
    query = {'product_key': 's2b:7', 'usage': 'available'}
    assert owner.get(base, params=query).json()['total'] == 0
    available = owner.get(base, params={**query, 'market': 'US'}).json()
    assert available['total'] == 1 and not available['items'][0]['used_by_selected']
    record_usage(artwork_id, 'US', status='failed')
    assert owner.get(base, params={**query, 'market': 'US'}).json()['total'] == 1
    record_usage(artwork_id, 'US')
    assert owner.get(base, params={**query, 'market': 'US'}).json()['total'] == 0
    used = owner.get(base, params={**query, 'market': 'US', 'usage': 'used_selected'}).json()
    assert used['total'] == 1 and used['items'][0]['used_by_selected']
    assert used['items'][0]['used_product_count'] == 2
    assert owner.get(base, params={'market': 'US', 'usage': 'unused_any'}).json()['total'] == 0
    history = owner.get(base + '/' + artwork_id + '/usages').json()['items']
    assert {x['market'] for x in history if x['counts_as_used']} == {'CN', 'US'}


def test_us_batch_deduplicates_only_same_market_and_queues_on_origin_pc(team, monkeypatch):
    clients, worker = team; owner = clients['owner']; request, _ = seed(owner, worker)
    artwork_id = request['placements'][0]['artwork_id']
    record_usage(artwork_id)
    from erp.domains.pod.integrations.s2b import S2BClient
    monkeypatch.setattr(S2BClient, '_request', lambda *a, **kw: pytest.fail('Enqueue must not call supplier'))
    batch = {**request, 'artwork_ids': [artwork_id], 'prevent_duplicate': True}
    response = owner.post('/api/v1/pod/compositions/batch', json=batch)
    assert response.status_code == 202, response.text
    assert response.json()['accepted'] == 1
    with SessionLocal() as db:
        job = db.get(Job, response.json()['job_ids'][0])
        assert job.type == 'pod_us_composition' and job.target_worker_id == worker
        assert job.status == 'queued'
    assert owner.post('/api/v1/pod/compositions/batch', json=batch).status_code == 409


@pytest.mark.parametrize('scenario', ['market_mismatch', 'missing_product', 'missing_detail', 'image_failure', 'success'])
def test_composer_handoff_preserves_failed_selection_and_reuses_selected_us_product(scenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed for the browser handoff regression')
    source = (Path(__file__).resolve().parents[1] / 'backend/erp/domains/pod/composer_batch_bridge.js').read_text(encoding='utf-8')
    importer = source[source.index('async function importErpComposerBatch()'):source.index('// Share in-flight reads')]
    script = r'''
const assert = require('node:assert/strict');
const scenario = SCENARIO;
const product = {key:'s2b:7'};
let stored = JSON.stringify({market: scenario==='market_mismatch'?'CN':'US',product,
  items:[{id:'art1',thumbnail_url:'/thumbnail'}]}), reads=0, loads=0;
const ERP_COMPOSER_BATCH_KEY = 'batch', erpUsComposer = true;
const window = {sessionStorage:{getItem:()=>stored,removeItem:()=>{stored=null;}}};
const state = {products:scenario==='missing_product'?[]:[product], selectedProduct:product,
  detail:scenario==='missing_detail'?null:{}, artworkReferenceId:'',artworkStatusMessage:''};
const productId = p=>p.key;
const loadCatalog = async()=>{reads++;};
const selectProduct = async()=>{throw new Error('US product was already selected; no repeated detail read');};
const showNotice = ()=>{}, updateReadiness=()=>{};
const useArtworkLibraryItem = async item=>{
  loads++;
  if(scenario==='image_failure')state.artworkStatusMessage='加入失败';
  else state.artworkReferenceId=item.id;
};
IMPORTER
(async()=>{
  await importErpComposerBatch();
  if(scenario==='success'){
    assert.equal(stored,null);assert.equal(loads,1);assert.equal(reads,0);
    assert.equal(state.batchArtworkItems[0].id,'art1');
  }else{
    assert.ok(stored,'Failed handoff must retain the artwork selection');
    assert.equal(loads,scenario==='image_failure'?1:0);
    assert.equal(state.batchArtworkItems?.length||0,0);
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
'''.replace('SCENARIO', json.dumps(scenario)).replace('IMPORTER', importer)
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
