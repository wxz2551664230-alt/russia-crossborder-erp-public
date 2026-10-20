import httpx
import pytest
from erp.integrations.amazon import AmazonUSClient,US_MARKETPLACE
from erp.core.errors import AppError

CONFIG={'seller_id':'SELLER','lwa_client_id':'app','lwa_client_secret':'private','refresh_token':'private-refresh'}

def test_s2b_us_catalog_does_not_mix_domestic_products():
    from erp.domains.pod.integrations.s2b import S2BClient
    c = object.__new__(S2BClient)
    calls = []
    def request(method, path, **kwargs):
        calls.append(kwargs['params'])
        return {'data': {'data': [
            {'id': 1, 'produce_country': 'US'},
            {'id': 2, 'produce_country': 'CN', 'produce_area': 1},
            {'id': 3, 'produce_area': 1},
        ]}}
    c._request = request
    result = c.product_types(1, country='US')
    assert len(result['data']) == 1
    assert result['_page_item_count'] == 3
    assert calls == [{'page': 1, 'per_page': 100, 'produce_country': 'US'}]
    result = c.product_types(1)
    assert len(result['data']) == 2
    assert calls[-1]['produce_area'] == 1

def test_amazon_token_cached_and_us_read_is_only_sellers():
    calls=[]
    def handler(request):
        calls.append(request)
        if request.url.host=='api.amazon.com':return httpx.Response(200,json={'access_token':'access','expires_in':3600})
        assert request.method=='GET' and request.headers['x-amz-access-token']=='access'
        return httpx.Response(200,json={'payload':[{'marketplace':{'id':US_MARKETPLACE},'participation':{'isParticipating':True}}]})
    c=AmazonUSClient(CONFIG,transport=httpx.MockTransport(handler))
    try:
        assert c.marketplace_participations()['participating']
        c.marketplace_participations()
        assert len(calls)==3
    finally:c.close()

def test_amazon_auth_errors_do_not_echo_secrets():
    c=AmazonUSClient(CONFIG,transport=httpx.MockTransport(lambda r:httpx.Response(400,json=CONFIG)))
    try:
        with pytest.raises(AppError) as e:c.access_token()
        assert 'private' not in str(e.value)
    finally:c.close()

def test_amazon_credentials_encrypted_and_test_not_fake(client):
    r=client.post('/api/v1/stores',json={'platform':'amazon','name':'美国FBM测试','external_account_id':'SELLER','currency':'USD','config':{'marketplace_id':US_MARKETPLACE},'secrets':{k:v for k,v in CONFIG.items() if k!='seller_id'}})
    assert r.status_code==201
    data=r.json();assert 'private' not in str(data)
    assert data['secret_status']['refresh_token']['configured']
    assert client.post('/api/v1/stores/'+data['id']+'/test').status_code==409
