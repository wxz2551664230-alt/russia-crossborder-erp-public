"""Read-only business categories for task discovery."""
from sqlalchemy import or_, not_
from erp.models import Job

MODULES = {
    'amazon': ('Amazon 美国站上架', ('amazon_',), ()),
    'wb': ('WB 上架 / 库存 / 价格', ('pod_wb_',), ()),
    'ozon': ('Ozon 产品上架', ('catalog_ozon_',), ()),
    'orders': ('订单 / 采购', ('pod_order_', 'pod_purchase_'), ()),
    'supplier': ('供应商同步', ('pod_supplier_',), ()),
    'ai': ('AI 生图 / 超分', ('pod_ai_design_',), ()),
    'pod': ('POD 合成 / 图库', (), ('pod_native_composition','pod_us_composition', 'pod_artwork_upload', 'pod_library_restore', 'image_prepare')),
    'self': ('自主上架 / 历史导入', ('catalog_self_', 'catalog_pod_', 'self_listing_'), ()),
    'ads': ('广告优化', ('ad_optimization_',), ()),
    'follow': ('Ozon 跟卖', ('ozon_',), ('listing_pipeline',)),
    'system': ('系统 / 电脑连接', (), ('worker_probe', 'store_connection_test', 'legacy_migration', 'asset_index')),
}


def module_condition(key):
    if key == 'other':
        return not_(or_(*(module_condition(k) for k in MODULES)))
    _, prefixes, types = MODULES[key]
    return or_(*(Job.type.startswith(prefix, autoescape=True) for prefix in prefixes),
               Job.type.in_(types))


def job_module(job_type):
    for key, (_, prefixes, types) in MODULES.items():
        if job_type in types or any(job_type.startswith(p) for p in prefixes):
            return key
    return 'other'
