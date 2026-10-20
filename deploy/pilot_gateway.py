"""Restricted staging entry point. Never use as a full ERP deployment."""
import re

from starlette.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware
from erp.main import app as erp_app

compressed_reads = GZipMiddleware(erp_app, minimum_size=1000, compresslevel=5)
COMPRESSED_PATHS = {
    '/api/v1/pod/catalog', '/api/v1/pod/legacy-api/jobs',
    '/api/v1/pod/original-assets/app.js', '/api/v1/pod/original-assets/styles.css',
}


def allowed(method: str, path: str) -> bool:
    if method=='GET' and path=='/api/v1/pod/compositions/retry-failed/preview':return True
    if method=='POST' and path=='/api/v1/pod/compositions/retry-failed':return True
    if method=='POST' and re.fullmatch(r'/api/v1/pod/wb/stores/[a-f0-9]{32}/sync',path):return True
    if method=='GET' and re.fullmatch(r'/api/v1/pod/wb/stores/[a-f0-9]{32}/sync-status',path):return True
    if method=='POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/wb-shop-sync-throttle',path):return True
    if method=='GET' and path in {'/api/v1/amazon/pricing','/api/v1/amazon/products','/api/v1/amazon/batches'}:return True
    if method=='POST' and path in {'/api/v1/amazon/preview','/api/v1/amazon/batches','/api/v1/amazon/group-preview','/api/v1/amazon/batch-groups'}:return True
    if method=='GET' and re.fullmatch(r'/api/v1/amazon/batch-groups/[a-f0-9]{32}',path):return True
    if method=='DELETE' and re.fullmatch(r'/api/v1/amazon/(?:batch-groups|batches)/[a-f0-9]{32}',path):return True
    if method=='POST' and path=='/api/v1/amazon/batches/remove':return True
    if method in {'GET','POST'} and re.fullmatch(r'/api/v1/amazon/stores/[a-f0-9]{32}/catalog',path):return True
    if method=='GET' and re.fullmatch(r'/api/v1/amazon/batches/[a-f0-9]{32}',path):return True
    if method=='PATCH' and re.fullmatch(r'/api/v1/amazon/batches/[a-f0-9]{32}/facts',path):return True
    if method=='POST' and re.fullmatch(r'/api/v1/amazon/batches/[a-f0-9]{32}/(?:validate|submit|reconcile|refresh-basis)',path):return True
    if method=='GET' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/amazon-snapshot',path):return True
    if method=='POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/amazon-(?:intent|result|progress)',path):return True
    if method == 'GET' and path == '/api/v1/pod/us-catalog/freight-rule': return True
    if method == 'POST' and re.fullmatch(r'/api/v1/pod/us-catalog/products/s2b:[0-9]+/freight(?:-preview)?', path): return True
    if method == 'GET' and (path in {'/api/v1/pod/us-catalog','/api/v1/pod/us-composer/catalog'} or re.fullmatch(r'/api/v1/pod/(?:us-catalog|us-composer)/products/s2b:[0-9]+', path)):
        return True
    if method == 'POST' and (path == '/api/v1/pod/us-catalog/sync' or re.fullmatch(r'/api/v1/pod/us-catalog/products/s2b:[0-9]+/refresh', path)):
        return True
    if method == 'GET' and path == '/api/v1/codex-actions': return True
    if method == 'POST' and re.fullmatch(r'/api/v1/codex-actions/[a-z][a-z0-9_.-]{1,99}/(?:preview|execute)', path): return True
    if method == 'GET' and path in {'/api/v1/client/releases/latest', '/api/v1/client/releases/latest/download'}: return True
    if method=='GET' and re.fullmatch(r'/api/v1/codex-handoffs/(?:batches/[a-f0-9]{32}|[a-f0-9]{32}/local-context)',path):return True
    if method=='POST' and re.fullmatch(r'/api/v1/pod/wb/exports/[a-f0-9]{32}/approve-codex',path):return True
    if method=='POST' and re.fullmatch(r'/api/v1/codex-handoffs/[a-f0-9]{32}/resume',path):return True
    if method=='POST' and path=='/api/v1/publications/stock-scan':return True
    if method=='POST' and re.fullmatch(r'/api/v1/publications/batch/(?:submit|stock|promotion)',path):return True
    if method=='GET' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/catalog-ozon-batch/snapshot',path):return True
    if method=='POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/catalog-ozon-batch/(?:intent|task|result)',path):return True
    if method == 'PATCH' and re.fullmatch(r'/api/v1/execution-devices/[A-Za-z0-9_-]{1,100}/owner', path): return True
    if method == 'POST' and path == '/api/v1/publications': return True
    if method == 'POST' and re.fullmatch(r'/api/v1/publications/[a-f0-9]{32}/stores/[a-f0-9]{32}/(?:submit|retry|recover-import|stock|promotion|skip-stock|skip-promotion|image/(?:approve|regenerate|redesign|full_redesign|repair))',path): return True
    if method == 'GET' and re.fullmatch(r'/api/v1/publications/[a-f0-9]{32}/(?:export/ozon\.xlsx|stores/[a-f0-9]{32}/image-preview)',path): return True
    if method == 'POST' and re.fullmatch(r'/api/v1/stores/[a-f0-9]{32}/ozon-options/(?:warehouses|promotions|category_attributes|category_tree)',path): return True
    if method == 'GET' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/catalog-ozon-(?:targets|snapshot/[a-f0-9]{32}|import-snapshot|followup-snapshot|options-snapshot)',path): return True
    if method == 'POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/catalog-ozon-(?:prepared/[a-f0-9]{32}|candidate/[a-f0-9]{32}|copy-revision/[a-f0-9]{32}|prepare-finish|import-(?:intent|task|result|finish)|followup-(?:intent|result))',path): return True
    if method == 'GET' and path in {'/api/v1/ad-optimization', '/api/v1/ad-optimization/dashboard', '/api/v1/ad-optimization/actions/batches'}:
        return True
    if method == 'GET' and re.fullmatch(r'/api/v1/ad-optimization/actions/(?:previews|batches)/[a-f0-9]{32}', path):
        return True
    if method == 'POST' and path in {'/api/v1/ad-optimization/actions/preview', '/api/v1/ad-optimization/actions/execute-batch'}:
        return True
    if method == 'POST' and re.fullmatch(r'/api/v1/ad-optimization/actions/batches/[a-f0-9]{32}/reconcile', path):
        return True
    if method == 'GET' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/ad-analysis-snapshot', path):
        return True
    if method == 'GET' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/ad-execution-(?:snapshot|credentials)', path):
        return True
    if method == 'GET' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/ad-reconcile-snapshot', path):
        return True
    if method == 'GET' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/ad-sync-(?:snapshot|credentials)', path):
        return True
    if method == 'POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/ad-sync-(?:chunk|result)', path):
        return True
    if method == 'POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/ad-(?:analysis|backtest)-result', path):
        return True
    if method == 'POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/ad-execution-result', path):
        return True
    if method == 'POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/ad-reconcile-result', path):
        return True
    if method == 'POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/ad-report-result', path):
        return True
    if method == 'GET' and re.fullmatch(r'/api/v1/ad-optimization/reports/[a-f0-9]{32}/download', path):
        return True
    if method == 'POST' and path in {'/api/v1/ad-optimization/jobs/analyze', '/api/v1/ad-optimization/jobs/backtest', '/api/v1/ad-optimization/jobs/sync', '/api/v1/ad-optimization/jobs/reports'}:
        return True
    if method == 'PUT' and re.fullmatch(r'/api/v1/ad-optimization/skus/[a-f0-9]{32}/outcome', path):
        return True
    order_root='/api/v1/orders/pod'
    order_key=r'[A-Za-z0-9][A-Za-z0-9_.:%-]*'
    if method=='GET' and (path in {order_root,order_root+'/addresses',order_root+'/product-options',order_root+'/batch/groups',order_root+'/purchase-export'}
            or re.fullmatch(order_root+'/'+order_key+r'(?:/purchase-groups)?',path)):return True
    if method=='POST' and (path in {order_root+'/manual',order_root+'/import',order_root+'/rematch',order_root+'/addresses',order_root+'/batch/preview',order_root+'/batch/queue',order_root+'/manual-complete',order_root+'/tracking-export'}
            or re.fullmatch(order_root+'/'+order_key+r'/(?:manual-tracking|rematch|items/[0-9]+/match)',path)):return True
    if method=='PUT' and re.fullmatch(order_root+'/'+order_key+r'/recipient',path):return True
    if method=='DELETE' and re.fullmatch(order_root+r'/addresses/[a-f0-9]{32}',path):return True
    if method=='POST' and path=='/api/v1/orders/pod/purchase/submit':return True
    if method=='POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/purchase-(?:submit-snapshot|operation|submit-result|write-intent)',path):return True
    if method=='POST' and path=='/api/v1/orders/pod/purchase/sync':return True
    if method=='POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/purchase-(?:snapshot|statuses)',path):return True
    if method=='POST' and path in {'/api/v1/orders/pod/sync','/api/v1/orders/pod/images/sync'}:return True
    if method=='POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/order-(?:snapshot|batch|images)',path):return True
    if method=='GET' and (path in {'/api/v1/pod/wb/templates','/api/v1/pod/wb/exports','/api/v1/pod/wb/publications'} or re.fullmatch(r'/api/v1/pod/wb/exports/[a-f0-9]{32}/download',path)):return True
    if method=='POST' and path=='/api/v1/pod/wb/templates':return True
    if method=='GET' and (path=='/api/v1/jobs/overview' or re.fullmatch(r'/api/v1/jobs/overview/[a-f0-9]{32}',path)):return True
    if method=='POST' and path in {'/api/v1/pod/wb/availability','/api/v1/pod/wb/exports/preview','/api/v1/pod/wb/exports'}:return True
    if method=='POST' and re.fullmatch(r'/api/v1/pod/wb/exports/[a-f0-9]{32}/confirm-upload',path):return True
    if method=='POST' and re.fullmatch(r'/api/v1/pod/wb/exports/[a-f0-9]{32}/reconcile-upload',path):return True
    if method=='POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/wb-reconcile-snapshot',path):return True
    if method=='POST' and path in {'/api/v1/pod/wb/history/reconcile','/api/v1/pod/wb/history/sync-uploaded'}:return True
    if method=='GET' and re.fullmatch(r'/api/v1/pod/wb/history/[a-f0-9]{32}/report',path):return True
    if method=='POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/wb-history-(?:snapshot|part)',path):return True
    if method=='POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/wb-stock-snapshot',path):return True
    if method=='POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/wb-price-(?:snapshot|progress)',path):return True
    if method=='GET' and path=='/api/v1/pod/wb/warehouses':return True
    if method=='POST' and path=='/api/v1/pod/wb/publications/stock':return True
    if method=='POST' and path=='/api/v1/pod/wb/publications/price':return True
    if method=='POST' and path=='/api/v1/pod/wb/publications/price/reconcile':return True
    if method=='POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/(?:wb-snapshot|wb-progress|wb-file/(?:wb_excel|wb_package|wb_copy|wb_copy_failures))',path):return True
    if method=='GET' and (path in {'/api/v1/pod/ai-design/status','/api/v1/pod/ai-design/batches'} or re.fullmatch(r'/api/v1/pod/ai-design/batches/[a-f0-9]{32}',path)):return True
    if method=='POST' and (path in {'/api/v1/pod/ai-design/uploads','/api/v1/pod/ai-design/batches','/api/v1/pod/ai-design/upscale'} or re.fullmatch(r'/api/v1/pod/ai-design/batches/[a-f0-9]{32}/generate',path)):return True
    if method=='PATCH' and re.fullmatch(r'/api/v1/pod/ai-design/batches/[a-f0-9]{32}/plan',path):return True
    if method=='POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/ai-(?:snapshot|progress)',path):return True
    if method=='GET' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/ai-source/[a-f0-9]{32}',path):return True
    if method=='POST' and path in {'/api/v1/pod/artworks','/api/v1/pod/artworks/batch','/api/v1/pod/artworks/categories'}:return True
    if method in {'PATCH','DELETE'} and re.fullmatch(r'/api/v1/pod/artworks/categories/[a-f0-9]{32}',path):return True
    if method=='PATCH' and path=='/api/v1/pod/artworks/batch-category':return True
    if method=='GET' and re.fullmatch(r'/api/v1/pod/artworks/upload-result/[a-f0-9]{32}',path):return True
    if method in {'GET','POST'} and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/artwork-(?:source|snapshot)',path):return True
    if method=='POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/composition-(?:snapshot|progress)',path):return True
    if method=='GET' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/source/[a-f0-9]{32}',path):return True
    if method=='GET' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/supplier-gallery-snapshot',path):return True
    if method=='POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/supplier-gallery-result',path):return True
    if method=='GET' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/supplier-catalog-snapshot',path):return True
    if method=='POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/supplier-catalog-result',path):return True
    if method=='GET' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/catalog-import-source',path):return True
    if method=='GET' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/catalog-import-oss',path):return True
    if method=='POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/catalog-import-result',path):return True
    if method=='POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/pod-import-result',path):return True
    if method=='GET' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/self-listing-config',path):return True
    if method=='POST' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/self-listing-artifact',path):return True
    if method=='GET' and path in {'/api/v1/catalog/imports','/api/v1/catalog/imports/self-selected/template'}:return True
    if method=='POST' and path=='/api/v1/catalog/imports/self-selected':return True
    if method=='POST' and path=='/api/v1/catalog/imports/self-selected/bundle':return True
    if method=='POST' and path=='/api/v1/catalog/imports/pod':return True
    if method=='POST' and path=='/api/v1/catalog/self-listing/pipeline':return True
    if method=='GET' and re.fullmatch(r'/api/v1/assets/[a-f0-9]{32}/content',path):return True
    if method=='GET' and (path in {'/api/v1/pod/original-composer','/api/v1/pod/original-assets/app.js','/api/v1/pod/original-assets/styles.css','/api/v1/pod/legacy-health','/api/v1/pod/legacy-api/status','/api/v1/pod/legacy-api/jobs','/api/v1/pod/catalog','/api/v1/pod/library/categories/compat','/api/v1/pod/compositions'} or re.fullmatch(r'/api/v1/pod/product-types/(?:hicustom|s2b):[0-9]+',path)):return True
    if method=='DELETE' and path=='/api/v1/pod/legacy-api/jobs/finished':return True
    if method=='POST' and (path in {'/api/v1/pod/compositions','/api/v1/pod/compositions/multi','/api/v1/pod/compositions/batch','/api/v1/pod/product-types/previews','/api/v1/pod/legacy-api/jobs','/api/v1/pod/legacy-api/jobs/multi'} or re.fullmatch(r'/api/v1/pod/legacy-api/jobs/[a-f0-9]{32}/(?:cancel|retry|retry-oss)',path)):return True
    if method=='POST' and path=='/api/v1/pod/supplier-gallery/sync':return True
    if method=='POST' and path=='/api/v1/pod/catalog/sync':return True
    if method == 'POST' and re.fullmatch(r'/api/v1/jobs/[a-f0-9]{32}/(?:pause|resume|cancel|retry)', path):
        return True
    if method == 'GET' and (path in {'/api/v1/execution-devices','/api/v1/jobs','/api/v1/ai-usage','/api/v1/auth/users'} or re.fullmatch(r'/api/v1/jobs/[a-f0-9]{32}',path)):
        return True
    if method == 'POST' and (path in {'/api/v1/execution-devices/select','/api/v1/execution-devices/probe','/api/v1/execution-devices/local/register'}
        or re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/(?:claim|jobs/[a-f0-9]{32}/(?:heartbeat|complete|fail))',path)
        or re.fullmatch(r'/api/v1/pod/library/products/[a-f0-9]{32}/restore',path)):
        return True
    if method == 'GET' and (path in {
        '/api/v1/pod/library/summary', '/api/v1/pod/library/products',
        '/api/v1/pod/library/categories', '/api/v1/pod/artworks',
        '/api/v1/pod/artworks/product-options', '/api/v1/pod/supplier-gallery',
    } or re.fullmatch(r'/api/v1/pod/library/products/[A-Za-z0-9_-]{1,100}', path)
      or re.fullmatch(r'/api/v1/pod/artworks/[A-Za-z0-9_-]{1,100}/(?:thumbnail|file|usages)', path)):
        return True
    if method in {"GET", "HEAD"}:
        if path == "/" or re.fullmatch(r"/(?:catalog|pod|self-listing|ozon-follow|orders|ad-optimization)(?:/[a-z0-9-]+)*", path) or path in {"/configuration", "/jobs", "/stores", "/connections", "/settings", "/users", "/execution-devices"}:
            return True
        if re.fullmatch(r"/assets/[A-Za-z0-9_-][A-Za-z0-9_.-]*\.(?:js|css|woff2?|svg|png|ico)", path):
            return True
    if method == "GET" and path in {"/health", "/readyz", "/api/v1/auth/config", "/api/v1/auth/session", "/api/v1/auth/client-capabilities"}:
        return True
    if method == "POST" and path in {"/api/v1/auth/login", "/api/v1/auth/logout", "/api/v1/auth/password"}:
        return True
    if method == "GET" and path in {"/api/v1/catalog/products", "/api/v1/catalog/summary", "/api/v1/catalog/duplicates"}:
        return True
    if method == "GET" and (path == "/api/v1/research-products" or re.fullmatch(r"/api/v1/research-products/[a-f0-9]{32}", path)):
        return True
    if method == "PUT" and re.fullmatch(r"/api/v1/research-products/[a-f0-9]{32}/category", path):
        return True
    if method == "GET" and (path == "/api/v1/listings" or re.fullmatch(r"/api/v1/listings/[a-f0-9]{32}", path)):
        return True
    if method == "GET" and (path in {"/api/v1/publications","/api/v1/publications/pricing-options","/api/v1/publications/stock-quantities"} or re.fullmatch(r"/api/v1/publications/[a-f0-9]{32}", path)):
        return True
    if method == "POST" and path == "/api/v1/publications/preview":
        return True
    if method == "POST" and path == "/api/v1/listings":
        return True
    if method == "PATCH" and re.fullmatch(r"/api/v1/listings/[a-f0-9]{32}", path):
        return True
    if method == "POST" and re.fullmatch(r"/api/v1/listings/[a-f0-9]{32}/price", path):
        return True
    if method == "GET" and (path == "/api/v1/stores" or re.fullmatch(r"/api/v1/stores/[A-Za-z0-9_-]{1,100}/category-options", path)):
        return True
    if method == 'GET' and path in {'/api/v1/connections','/api/v1/settings','/api/v1/settings/ozon-follow-pricing-options'}:
        return True
    if method == 'POST' and path in {'/api/v1/connections','/api/v1/stores'}:
        return True
    if method in {'PATCH','DELETE'} and re.fullmatch(r'/api/v1/(?:connections|stores)/[a-f0-9]{32}',path):
        return True
    if method == 'POST' and re.fullmatch(r'/api/v1/connections/[a-f0-9]{32}/test',path):
        return True
    if method == 'POST' and re.fullmatch(r'/api/v1/stores/[a-f0-9]{32}/test',path):
        return True
    if method == 'GET' and re.fullmatch(r'/api/v1/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}/store-test-snapshot',path):
        return True
    if method == 'PUT' and re.fullmatch(r'/api/v1/settings/[a-z][a-z0-9_]{0,79}',path):
        return True
    if method == "PATCH" and re.fullmatch(r"/api/v1/catalog/products/[A-Za-z0-9_-]{1,100}", path):
        return True
    if method == "POST" and re.fullmatch(r"/api/v1/catalog/products/[A-Za-z0-9_-]{1,100}/assets/url", path):
        return True
    if method == "GET" and re.fullmatch(r"/api/v1/catalog/products/[A-Za-z0-9_-]{1,100}", path):
        return True
    return method == "PUT" and bool(re.fullmatch(r"/api/v1/catalog/products/[A-Za-z0-9_-]{1,100}/notes", path))


async def app(scope, receive, send):
    if scope["type"] == "http" and not allowed(scope["method"], scope["path"]):
        await JSONResponse({"code": "pilot_scope_only", "message": "当前阶段已恢复原产品库查看；此操作尚未接通多人版本，不会在服务器执行。原本机 ERP 仍可使用。"}, status_code=403)(scope, receive, send)
        return
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    compress_path=scope['type']=='http' and (scope['path'] in COMPRESSED_PATHS or scope['path'].startswith('/api/v1/amazon/'))
    target = compressed_reads if compress_path and scope['method']=='GET' else erp_app
    await target(scope, receive, send)
