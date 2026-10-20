"""Scope the original purchase manager operations to one authorized task."""
from copy import deepcopy
import inspect

from erp.core.errors import ConflictError


GROUP_METHODS={'get_batch_group','update_batch_group','record_batch_costs','batch_payload','s2b_batch_payload'}
PURCHASE_METHODS={'list_purchase_groups','update_purchase_groups','record_merged_batch_costs','merged_batch_payload'}
ORDER_METHODS={'get_order','update_submission','submission_payload'}
REUSE_METHODS={'reusable_hicustom_product','reusable_s2b_product'}
WRITE_METHODS={'update_batch_group','record_batch_costs','update_purchase_groups','record_merged_batch_costs','update_submission'}


def capture(orders, target_type, target_id):
    if target_type=='group':
        group=orders.get_batch_group(target_id)
        groups=[group]
        if str(group.get('supplier') or 'hicustom')=='hicustom' and group.get('purchase_id'):
            groups=orders.list_purchase_groups(group['purchase_id'])
        return {'groups':[g['id'] for g in groups],
                'orders':sorted({s['order_key'] for g in groups for s in g['source_orders']}),
                'purchases':sorted({g['purchase_id'] for g in groups if g.get('purchase_id')}),
                'skus':sorted({g['sku'] for g in groups}),
                'suppliers':sorted({str(g.get('supplier') or 'hicustom') for g in groups})}
    if target_type=='order':
        order=orders.get_order(target_id)
        return {'groups':[],'orders':[order['order_key']],'purchases':[],
                'skus':sorted({i['matched_sku'] for i in order['items'] if i.get('matched_sku')}),
                'suppliers':['hicustom']}
    raise ConflictError('采购目标类型无效')


def invoke(orders, scope, settings, method, args, kwargs):
    """Data operations only. No supplier client or task executor lives here."""
    allowed=GROUP_METHODS|PURCHASE_METHODS|ORDER_METHODS|REUSE_METHODS|{'assert_purchase_sources_active','get_product'}
    if method not in allowed:raise ConflictError('不允许的采购数据操作')
    function=orders.library.get_product if method=='get_product' else getattr(orders,method)
    try:
        bound=inspect.signature(function).bind(*args,**kwargs);bound.apply_defaults()
    except TypeError:
        raise ConflictError('采购操作参数无效') from None
    values=bound.arguments
    for identity in scope['groups']:
        group=orders.get_batch_group(identity)
        if (group['sku'] not in scope['skus']
                or str(group.get('supplier') or 'hicustom') not in scope['suppliers']
                or not {row['order_key'] for row in group['source_orders']}<=set(scope['orders'])
                or (group.get('purchase_id') and group['purchase_id'] not in scope['purchases'])):
            raise ConflictError('采购组内容或来源订单已变化')
    if method in GROUP_METHODS and values['group_id'] not in scope['groups']:
        raise ConflictError('采购组不属于本任务')
    if method in ORDER_METHODS and values.get('posting_number',values.get('order_key')) not in scope['orders']:
        raise ConflictError('订单不属于本任务')
    if method in PURCHASE_METHODS:
        purchase=values['purchase_id']
        if purchase not in scope['purchases']:raise ConflictError('合并采购不属于本任务')
        if not {g['id'] for g in orders.list_purchase_groups(purchase)}<=set(scope['groups']):
            raise ConflictError('合并采购范围已变化')
    if method=='assert_purchase_sources_active' and not set(values['group_ids'])<=set(scope['groups']):
        raise ConflictError('采购组不属于本任务')
    if method in REUSE_METHODS|{'get_product'}:
        if values['sku'] not in scope['skus']:raise ConflictError('产品不属于本任务')
        if method in REUSE_METHODS and values['exclude_group_id'] not in scope['groups']:
            raise ConflictError('复用检查目标不属于本任务')
    if 'store_id' in values:
        provider='s2b' if method=='s2b_batch_payload' else 'hicustom'
        if provider not in scope['suppliers'] or int(values['store_id'])!=int(settings.get(provider+'_store_id') or 0):
            raise ConflictError('采购店铺与任务配置不同')
    result=function(*args,**kwargs)
    if method in REUSE_METHODS and result:
        fields={'id','sku','fulfillment_product_code','fulfillment_gallery_codes','product_revision','remote_product_id'}
        return {key:value for key,value in result.items() if key in fields}
    if method=='get_product':
        result=deepcopy(result);result['output_folder']=''
        for side in result.get('recipe',{}).get('sides',[]):side['source_path']=''
        for asset in result.get('oss_assets',[]):asset['local_path']=''
    return result
