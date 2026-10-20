"""Amazon US SP-API transport. Call only from the initiating desktop worker."""
import time
import base64
import hashlib
import json
from urllib.parse import quote, urlsplit
import httpx
from erp.core.errors import AppError

US_MARKETPLACE = "ATVPDKIKX0DER"

class AmazonWriteUnknown(AppError):
    def __init__(self):
        super().__init__('Amazon 提交结果未知；请核对原 SKU，程序不会重复提交', code='amazon_write_outcome_unknown',status_code=409)

class AmazonRequestRejected(AppError):
    """An explicit non-mutating HTTP rejection, retained as a per-SKU receipt."""
    def __init__(self, status, errors=None):
        self.response={'status':'INVALID','http_status':status,'issues':[
            {'severity':'ERROR','code':str(e.get('code') or status)[:100],
             'message':str(e.get('message') or 'Amazon 拒绝本次请求')[:1000]}
            for e in (errors or [{}])[:20] if isinstance(e,dict)]}
        super().__init__(f'Amazon 已拒绝本次请求（HTTP {status}）',code='amazon_request_rejected',status_code=409)

class AmazonUSClient:
    def __init__(self, config, *, transport=None):
        self.seller_id=str(config.get('seller_id') or config.get('external_account_id') or '').strip()
        self.secrets=config.get('secrets') or config
        if not self.seller_id or not all(self.secrets.get(k) for k in ('lwa_client_id','lwa_client_secret','refresh_token')):
            raise AppError('Amazon 缺少 Seller ID 或 LWA 授权配置',code='amazon_incomplete')
        self.http=httpx.Client(transport=transport,timeout=30,follow_redirects=False)
        self.token='';self.expires=0
        self.next_request=0

    def close(self):self.http.close()

    def request(self, method, path, *, params=None, body=None, preview=False, missing_ok=False):
        token=self.access_token()
        time.sleep(max(0,self.next_request-time.monotonic()))
        self.next_request=time.monotonic()+.6
        try:
            response=self.http.request(method,'https://sellingpartnerapi-na.amazon.com'+path,
                params=params,json=body,headers={'x-amz-access-token':token,'User-Agent':'RussiaERP/AmazonUS'})
        except httpx.HTTPError:
            if method!='GET' and not preview: raise AmazonWriteUnknown() from None
            raise AppError('Amazon 连接未完成，请从原任务继续',code='amazon_connection_failed') from None
        if response.status_code==404 and missing_ok:return None
        if (response.status_code>=500 or response.status_code==408) and method!='GET' and not preview:raise AmazonWriteUnknown()
        if method!='GET' and 400<=response.status_code<500:
            try:errors=response.json().get('errors',[])
            except (ValueError,AttributeError):errors=[]
            raise AmazonRequestRejected(response.status_code,errors)
        if response.status_code==429:
            raise AppError('Amazon 限流，本次已停止；请稍后从原任务人工继续',code='amazon_rate_limited',status_code=429)
        if not response.is_success:
            raise AppError(f'Amazon 请求失败（HTTP {response.status_code}），请核对授权与资料',code='amazon_request_failed',status_code=409)
        try:return response.json()
        except ValueError:
            if method!='GET' and not preview:raise AmazonWriteUnknown() from None
            raise AppError('Amazon 返回格式无效',code='amazon_response_invalid') from None

    def product_types(self, keywords):
        return self.request('GET','/definitions/2020-09-01/productTypes',params={
            'marketplaceIds':US_MARKETPLACE,'keywords':keywords,'locale':'en_US'})

    def definition(self, product_type, parentage='NONE'):
        data=self.request('GET','/definitions/2020-09-01/productTypes/'+quote(product_type,safe=''),params={
            'marketplaceIds':US_MARKETPLACE,'sellerId':self.seller_id,'requirements':'LISTING',
            'requirementsEnforced':'ENFORCED','locale':'en_US','parentageLevel':parentage})
        link=data.get('schema') or {}; parsed=urlsplit((link.get('link') or {}).get('resource',''))
        if (parsed.scheme!='https' or not parsed.hostname or not parsed.hostname.endswith('.amazonaws.com')
                or parsed.username or parsed.password or parsed.port not in (None,443)):
            raise AppError('Amazon 类目定义下载地址无效',code='amazon_schema_invalid')
        # Presigned schema request deliberately carries no LWA authorization.
        try:
            with self.http.stream('GET',parsed.geturl()) as response:
                if not response.is_success:raise ValueError()
                raw=bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw)>4*1024**2:raise ValueError()
            expected=str(link.get('checksum') or '')
            digest=hashlib.md5(raw,usedforsecurity=False).digest()
            if expected not in {base64.b64encode(digest).decode(),digest.hex()}:raise ValueError()
            schema=json.loads(raw)
        except (httpx.HTTPError,ValueError,TypeError):
            raise AppError('Amazon 类目定义下载或校验未完成',code='amazon_schema_invalid') from None
        # Signed URLs are transient credentials. Only materialized schemas go to cloud.
        return {'product_type':product_type,'parentage':parentage,'schema':schema,
                'checksum':expected,'version':data.get('productTypeVersion') or {},
                'display_name':data.get('displayName') or product_type}

    def listing(self, sku):
        return self.request('GET',f'/listings/2021-08-01/items/{quote(self.seller_id,safe="")}/{quote(sku,safe="")}',
            params={'marketplaceIds':US_MARKETPLACE,'includedData':'summaries,issues,attributes,relationships,fulfillmentAvailability'},missing_ok=True)

    def put_listing(self, sku, body, *, preview=False):
        params={'marketplaceIds':US_MARKETPLACE,'issueLocale':'en_US'}
        if preview:params['mode']='VALIDATION_PREVIEW'
        return self.request('PUT',f'/listings/2021-08-01/items/{quote(self.seller_id,safe="")}/{quote(sku,safe="")}',
                            params=params,body=body,preview=preview)

    def access_token(self):
        if self.token and self.expires>time.monotonic():return self.token
        try:
            r=self.http.post('https://api.amazon.com/auth/o2/token',data={'grant_type':'refresh_token',
                'client_id':self.secrets['lwa_client_id'],'client_secret':self.secrets['lwa_client_secret'],
                'refresh_token':self.secrets['refresh_token']})
            if not r.is_success:raise ValueError()
            body=r.json();token=body.get('access_token');ttl=int(body.get('expires_in',3600))
            if not token or ttl<=0:raise ValueError()
        except (httpx.HTTPError,ValueError,TypeError):
            raise AppError('Amazon 授权失败，请检查 LWA 凭据和卖家授权',code='amazon_auth_failed') from None
        self.token=token;self.expires=time.monotonic()+max(0,ttl-60);return token

    def marketplace_participations(self):
        token=self.access_token()
        try:
            r=self.http.get('https://sellingpartnerapi-na.amazon.com/sellers/v1/marketplaceParticipations',
                headers={'x-amz-access-token':token,'User-Agent':'RussiaERP/AmazonUS'})
        except httpx.HTTPError:
            raise AppError('Amazon 连接失败，请在原电脑继续',code='amazon_connection_failed') from None
        if r.status_code==429:
            raise AppError('Amazon 请求限流，请稍后人工测试',code='amazon_rate_limited',status_code=429)
        if not r.is_success:
            raise AppError(f'Amazon 读取站点失败（HTTP {r.status_code}），请核对应用权限',code='amazon_read_failed')
        try:
            rows=r.json()['payload']
            if not isinstance(rows,list):raise ValueError()
            us=next((x for x in rows if x.get('marketplace',{}).get('id')==US_MARKETPLACE),None)
            if not us or not us.get('participation',{}).get('isParticipating'):raise ValueError()
        except (ValueError,KeyError,TypeError):
            raise AppError('未确认美国站参与状态；不能视为上架权限已通过',code='amazon_us_not_confirmed') from None
        return {'marketplace_id':US_MARKETPLACE,'country':'US','currency':'USD','participating':True,
                'listing_permission_verified':False,'order_permission_verified':False}
