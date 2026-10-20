from deploy.pilot_gateway import allowed


def test_catalog_ozon_personal_routes_open_without_server_execution():
    publication_id = 'a' * 32
    assert allowed('GET', '/api/v1/publications')
    assert allowed('GET', '/api/v1/publications/stock-quantities')
    assert allowed('GET', f'/api/v1/publications/{publication_id}')
    assert allowed('POST', '/api/v1/publications/preview')
    assert allowed('POST', '/api/v1/publications')
    assert allowed('POST', f'/api/v1/publications/{publication_id}/stores/{publication_id}/submit')


def test_ozon_follow_original_product_page_has_read_only_cloud_data():
    product_id = 'a' * 32
    assert allowed('GET', '/ozon-follow/products')
    assert allowed('GET', '/api/v1/research-products')
    assert allowed('GET', f'/api/v1/research-products/{product_id}')
    assert not allowed('POST', '/api/v1/research-products/imports/seerfar')
    assert allowed('PUT', f'/api/v1/research-products/{product_id}/category')
    assert not allowed('DELETE', f'/api/v1/research-products/{product_id}')
    assert not allowed('GET', '/api/v1/research-products/../../settings')


def test_ozon_follow_listing_draft_writes_are_open_but_unadapted_jobs_are_blocked():
    listing_id = 'b' * 32
    assert allowed('GET', '/ozon-follow/details')
    assert allowed('GET', '/api/v1/listings')
    assert allowed('GET', f'/api/v1/listings/{listing_id}')
    assert allowed('POST', '/api/v1/listings')
    assert allowed('PATCH', f'/api/v1/listings/{listing_id}')
    assert allowed('POST', f'/api/v1/listings/{listing_id}/price')
    assert not allowed('GET', f'/api/v1/listings/{listing_id}/source-image')
    assert not allowed('POST', f'/api/v1/listings/{listing_id}/ai-prepare')
    assert not allowed('POST', f'/api/v1/listings/{listing_id}/run')


def test_ozon_follow_category_confirmation_round_trips_shared_product():
    from fastapi.testclient import TestClient
    from deploy.pilot_gateway import app
    from erp.core.database import SessionLocal
    from erp.models import ResearchProduct

    with SessionLocal() as db:
        product = ResearchProduct(source='test', source_sku='FOLLOW-CATEGORY', title='类目测试')
        db.add(product)
        db.commit()
        product_id = product.id
    with TestClient(app, headers={'Authorization': 'Bearer test-operator-token'}) as client:
        result = client.put(f'/api/v1/research-products/{product_id}/category', json={
            'description_category_id': 123, 'type_id': 456, 'display': '测试末级类目',
        })
        assert result.status_code == 200
        detail = client.get(f'/api/v1/research-products/{product_id}')
        assert detail.status_code == 200
        assert detail.json()['category_display'] == '测试末级类目'
        assert detail.json()['description_category_id'] == 123
        assert detail.json()['type_id'] == 456
        assert detail.json()['status'] == 'category_confirmed'
        assert client.post(f'/api/v1/research-products/{product_id}/sourcing-candidates', json={}).status_code == 403


def test_ozon_follow_draft_create_edit_and_original_pricing_without_jobs():
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select
    from deploy.pilot_gateway import app
    from erp.core.database import SessionLocal
    from erp.core.jsonutil import dumps
    from erp.models import Job, ResearchProduct, SourcingCandidate, Store

    with SessionLocal() as db:
        product = ResearchProduct(source='test', source_sku='FOLLOW-DRAFT', title='隔离草稿',
                                  status='source_confirmed', weight_grams=500)
        pricing_config = dumps({'ozon_follow_pricing': {'target_cost_profit_margin': 30,
                                'pricing_category_module': '家居与汽车用品',
                                'pricing_product_category': '装饰、清洁与储物'}})
        first = Store(platform='ozon', name='隔离店 A', external_account_id='isolation-a',
                      currency='CNY', config_json=pricing_config)
        second = Store(platform='ozon', name='隔离店 B', external_account_id='isolation-b',
                       currency='CNY', config_json=pricing_config)
        db.add_all([product, first, second])
        db.flush()
        db.add(SourcingCandidate(research_product_id=product.id, provider='manual',
                                 external_product_id='123456789', external_sku_id='SKU-1',
                                 unit_price_cny=12, selected=True))
        db.commit()
        product_id, first_id, second_id = product.id, first.id, second.id
    with TestClient(app, headers={'Authorization': 'Bearer test-operator-token'}) as client:
        created = client.post('/api/v1/listings', json={'research_product_id': product_id,
                              'base_offer_id': 'ISOLATION-FOLLOW-DRAFT', 'store_ids': [first_id, second_id]})
        assert created.status_code == 201
        listing_id = created.json()['id']
        saved = client.patch(f'/api/v1/listings/{listing_id}', json={
            'store_id': first_id, 'title': '独立标题 A', 'description_ru': '独立文案 A',
            'package': {'depth_mm': 300, 'width_mm': 200, 'height_mm': 100, 'weight_g': 500},
            'pricing': {'price': 180, 'old_price': 240, 'currency': 'CNY'},
        })
        assert saved.status_code == 200
        stores = {row['store_id']: row for row in saved.json()['stores']}
        assert stores[first_id]['content']['title'] == '独立标题 A'
        assert stores[second_id]['content'].get('title') != '独立标题 A'
        priced = client.post(f'/api/v1/listings/{listing_id}/price')
        assert priced.status_code == 200, priced.text
        assert all(row['pricing'].get('price') for row in priced.json()['stores'])
        assert client.post(f'/api/v1/listings/{listing_id}/ai-prepare').status_code == 403
        assert client.post(f'/api/v1/listings/{listing_id}/run', json={'dry_run': True}).status_code == 403
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_ad_history_and_personal_analysis_only_are_exposed():
    for path in ('/api/v1/ad-optimization', '/api/v1/ad-optimization/dashboard', '/api/v1/ad-optimization/actions/batches'):
        assert allowed('GET', path)
    assert allowed('POST', '/api/v1/ad-optimization/jobs/analyze')
    assert allowed('POST', '/api/v1/ad-optimization/jobs/backtest')
    assert allowed('POST', '/api/v1/ad-optimization/jobs/sync')
    assert allowed('POST', '/api/v1/ad-optimization/actions/preview')
    assert allowed('GET', '/api/v1/ad-optimization/actions/previews/' + 'b'*32)
    assert allowed('GET', '/api/v1/ad-optimization/actions/batches/' + 'b'*32)
    assert allowed('POST', '/api/v1/ad-optimization/jobs/reports')
    assert allowed('GET', '/api/v1/ad-optimization/reports/' + 'b'*32 + '/download')
    assert allowed('PUT', '/api/v1/ad-optimization/skus/' + 'a'*32 + '/outcome')
    assert allowed('GET', '/api/v1/execution-devices/local/pc-' + 'a'*32 + '/jobs/' + 'b'*32 + '/ad-analysis-snapshot')
    assert allowed('POST', '/api/v1/execution-devices/local/pc-' + 'a'*32 + '/jobs/' + 'b'*32 + '/ad-analysis-result')
    assert allowed('POST', '/api/v1/execution-devices/local/pc-' + 'a'*32 + '/jobs/' + 'b'*32 + '/ad-backtest-result')
    for suffix in ('ad-sync-chunk', 'ad-sync-result'):
        assert allowed('POST', '/api/v1/execution-devices/local/pc-' + 'a'*32 + '/jobs/' + 'b'*32 + '/' + suffix)
    assert allowed('POST', '/api/v1/execution-devices/local/pc-' + 'a'*32 + '/jobs/' + 'b'*32 + '/ad-report-result')
    assert allowed('GET', '/api/v1/execution-devices/local/pc-' + 'a'*32 + '/jobs/' + 'b'*32 + '/ad-reconcile-snapshot')
    assert allowed('POST', '/api/v1/execution-devices/local/pc-' + 'a'*32 + '/jobs/' + 'b'*32 + '/ad-reconcile-result')
    assert allowed('POST', '/api/v1/ad-optimization/actions/batches/' + 'b'*32 + '/reconcile')
    assert not allowed('POST', '/api/v1/ad-optimization/reports')
    assert allowed('POST', '/api/v1/ad-optimization/actions/execute-batch')
    assert not allowed('POST', '/api/v1/ad-optimization/actions/preview/unsafe')


def test_wb_excel_upload_confirmation_and_local_reconcile_only():
    job = 'a' * 32
    assert allowed('POST', f'/api/v1/pod/wb/exports/{job}/confirm-upload')
    assert allowed('POST', f'/api/v1/pod/wb/exports/{job}/reconcile-upload')
    assert allowed('POST', f'/api/v1/execution-devices/local/pc-{job}/jobs/{job}/wb-reconcile-snapshot')
    assert allowed('POST', f'/api/v1/execution-devices/local/pc-{job}/jobs/{job}/wb-stock-snapshot')
    assert allowed('GET', '/api/v1/pod/wb/warehouses')
    assert allowed('POST', '/api/v1/pod/wb/publications/stock')
    assert allowed('POST', '/api/v1/pod/wb/publications/price')
    assert allowed('POST', '/api/v1/pod/wb/publications/price/reconcile')
    assert allowed('POST', f'/api/v1/execution-devices/local/pc-{job}/jobs/{job}/wb-price-snapshot')
    assert not allowed('POST', f'/api/v1/pod/wb/exports/{job}/cancel-upload')


def test_original_catalog_oss_image_write_is_scoped_to_one_product():
    product = 'a' * 32
    assert allowed('POST', f'/api/v1/catalog/products/{product}/assets/url')
    assert not allowed('POST', f'/api/v1/catalog/products/{product}/assets/prepare')
    assert not allowed('DELETE', f'/api/v1/catalog/products/{product}/assets/{product}')


def test_pilot_rejects_business_execution_even_for_admin_paths():
    for method, path in [
        ("POST", "/api/v1/jobs"), ("POST", "/api/v1/pod/compositions/abc/publish"),
        ("POST", "/api/v1/workers/register"), ("POST", "/api/v1/publications/unsafe-execute"),
        ("PUT", "/api/v1/catalog/products/abc"),
        ("DELETE", "/api/v1/catalog/products/abc"), ("GET", "/.env"),
        ("GET", "/assets/../.env"), ("GET", "/assets/secrets.json"),
        ("PUT", "/api/v1/catalog/products/../notes"),
        ("PATCH", "/api/v1/stores/abc"),
        ("POST", "/api/v1/stores/abc/categories/sync"),
        ("POST", "/api/v1/catalog/products/abc/assets/prepare"),
        ("DELETE", "/api/v1/catalog/products/abc/assets/link"),
        ("PATCH", "/api/v1/catalog/duplicates/abc"),
        ("POST", "/api/v1/catalog/summary"),
    ]:
        assert not allowed(method, path)
    from fastapi.testclient import TestClient
    from deploy.pilot_gateway import app
    with TestClient(app, headers={"Authorization": "Bearer test-operator-token"}) as client:
        assert client.get("/api/v1/catalog/products").status_code == 200
        result = client.post("/api/v1/jobs", json={"job_type": "worker_probe"})
        assert result.status_code == 403
        assert result.json()["code"] == "pilot_scope_only"


def test_pilot_exposes_only_client_vertical_slice():
    for method, path in [
        ("GET", "/health"), ("GET", "/readyz"),
        ("POST", "/api/v1/auth/login"), ("POST", "/api/v1/auth/logout"),
        ("POST", "/api/v1/auth/password"), ("GET", "/api/v1/auth/client-capabilities"),
        ("GET", "/api/v1/catalog/products"), ("GET", "/api/v1/catalog/products/abc123"),
        ("GET", "/api/v1/catalog/summary"), ("GET", "/api/v1/catalog/duplicates"),
        ("PUT", "/api/v1/catalog/products/abc123/notes"),
        ("GET", "/"), ("GET", "/catalog/products"), ("GET", "/pod/studio"),
        ("GET", "/assets/index-123.js"),
        ("POST", "/api/v1/pod/compositions"),
        ("POST", "/api/v1/pod/ai-design/uploads"),
        ("POST", "/api/v1/pod/ai-design/upscale"),
    ]:
        assert allowed(method, path)


def test_original_catalog_reads_keep_source_filters_variants_and_duplicates():
    from fastapi.testclient import TestClient
    from deploy.pilot_gateway import app
    from erp.core.database import SessionLocal
    from erp.models import CatalogProduct, CatalogVariant, CatalogDuplicateSuggestion
    with SessionLocal() as db:
        a=CatalogProduct(id='audit-follow',sku='FOL-AUDIT',name='跟卖样本',source_type='follow',source_url='https://detail.1688.com/offer/773453066664.html',category_display='测试类目')
        b=CatalogProduct(id='audit-self',sku='SELF-AUDIT',name='自主样本',source_type='self_selected')
        db.add_all([a,b]);db.flush()
        db.add(CatalogVariant(id='audit-variant',product_id=a.id,sku=a.sku,specification='15绿色恐龙',supplier_sku_id='MANUAL-773453066664',weight_grams=210))
        db.add(CatalogDuplicateSuggestion(id='audit-duplicate',product_id=a.id,candidate_product_id=b.id,score=.8))
        db.commit()
    with TestClient(app,headers={'Authorization':'Bearer test-operator-token'}) as client:
        result=client.get('/api/v1/catalog/products?source_type=follow&search=773453066664').json()
        assert result['total']==1 and result['items'][0]['sku']=='FOL-AUDIT'
        assert result['category_options']==[{'label':'测试类目','count':1}]
        stats=client.get('/api/v1/catalog/summary?source_type=follow').json()
        assert stats['total_products']==1 and stats['variants']==1 and stats['duplicate_suggestions']==1
        detail=client.get('/api/v1/catalog/products/audit-follow').json()
        assert detail['source_url'].endswith('/773453066664.html')
        assert detail['variants'][0]['specification']=='15绿色恐龙'
        assert detail['variants'][0]['supplier_sku_id']=='MANUAL-773453066664'
        assert client.get('/api/v1/catalog/duplicates').json()['items'][0]['id']=='audit-duplicate'


def test_catalog_ozon_bridge_only_opens_named_personal_protocol():
    worker='pc-'+'a'*32;job='b'*32;store='c'*32
    root=f'/api/v1/execution-devices/local/{worker}/jobs/{job}/'
    assert allowed('GET',root+'catalog-ozon-options-snapshot')
    assert allowed('POST',root+'catalog-ozon-import-intent')
    assert allowed('POST',root+'catalog-ozon-candidate/'+store)
    assert allowed('POST',root+'catalog-ozon-copy-revision/'+store)
    assert not allowed('GET',root+'catalog-ozon-copy-revision/'+store)
    assert not allowed('POST',root+'catalog-ozon-copy-revision/not-an-id')
    assert not allowed('POST',root+'catalog-ozon-run-anything')
    assert not allowed('GET',root+'catalog-ozon-prepared/'+store)
    assert not allowed('GET','/api/v1/stores/'+store+'/warehouses')
    assert not allowed('POST','/api/v1/stores/'+store+'/categories/sync')


def test_operator_options_permission_does_not_grant_store_admin():
    from erp.core.access import permitted
    path='/stores/'+'a'*32+'/ozon-options/warehouses'
    assert permitted({'role':'operator'},'POST',path)
    assert not permitted({'role':'viewer'},'POST',path)
    assert not permitted({'role':'operator'},'PATCH','/stores/'+'a'*32)
    assert not permitted({'role':'operator'},'POST','/stores/'+'a'*32+'/ozon-options/delete')

def test_device_owner_gateway_only_allows_existing_patch():
    for device in ['pc-' + 'a'*32, 'office-win-op2bhbrh071', 'local-stage-probe']:
        path = '/api/v1/execution-devices/' + device + '/owner'
        assert allowed('PATCH', path)
        assert not allowed('POST', path)
        assert not allowed('DELETE', path)
    assert not allowed('PATCH', '/api/v1/execution-devices/invalid.id/owner')
def test_wb_history_bridge_scope_is_narrow():
    from deploy.pilot_gateway import allowed
    worker='pc-'+'a'*32
    job='b'*32
    for path in ('/api/v1/pod/wb/history/reconcile','/api/v1/pod/wb/history/sync-uploaded'):
        assert allowed('POST',path)
        assert not allowed('GET',path)
    assert allowed('GET',f'/api/v1/pod/wb/history/{job}/report')
    for suffix in ('wb-history-snapshot','wb-history-part'):
        path=f'/api/v1/execution-devices/local/{worker}/jobs/{job}/{suffix}'
        assert allowed('POST',path)
        assert not allowed('GET',path)
    assert not allowed('POST',f'/api/v1/execution-devices/local/{worker}/jobs/{job}/wb-history-delete')
