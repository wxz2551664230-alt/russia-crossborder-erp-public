from contextlib import closing
import pytest
from erp.core.errors import ConflictError
from erp.domains.orders import purchase_scope
from test_pod_orders import orders, posting
from test_purchase_cloud_status import seed


def test_only_authorized_group_and_order_data_can_be_changed(orders):
    seed(orders);orders.import_postings([posting()],store_id='A')
    key=orders.list_orders()['orders'][0]['order_key']
    with closing(orders._connect()) as db:
        db.execute('INSERT INTO batch_order_sources(group_id,posting_number,quantity) VALUES (?,?,?)',('group',key,2));db.commit()
    scope=purchase_scope.capture(orders,'group','group')
    result=purchase_scope.invoke(orders,scope,{},'update_batch_group',['group'],{'submission_status':'checking'})
    assert result['submission_status']=='checking'
    assert purchase_scope.invoke(orders,scope,{},'get_order',[key],{})['order_key']==key
    for method,args,kwargs in [('update_batch_group',['other'],{}),('get_order',['other'],{}),
                               ('initialize',[],{}),('update_batch_group',[],{'group_id':'other'}),
                               ('s2b_batch_payload',['group',99],{})]:
        with pytest.raises(ConflictError):purchase_scope.invoke(orders,scope,{'s2b_store_id':1},method,args,kwargs)


def test_group_source_change_does_not_expand_task_scope(orders):
    seed(orders);scope=purchase_scope.capture(orders,'group','group')
    orders.import_postings([posting()],store_id='A');key=orders.list_orders()['orders'][0]['order_key']
    with closing(orders._connect()) as db:
        db.execute('INSERT INTO batch_order_sources(group_id,posting_number,quantity) VALUES (?,?,?)',('group',key,2));db.commit()
    with pytest.raises(ConflictError,match='来源'):
        purchase_scope.invoke(orders,scope,{},'update_batch_group',['group'],{'submission_status':'submitted'})
    assert orders.get_batch_group('group')['submission_status']=='unknown'


def test_product_snapshot_does_not_reuse_other_computer_paths():
    class Library:
        def get_product(self,sku):
            return {'output_folder':'C:/original','recipe':{'sides':[{'source_path':'C:/image'}]},
                    'oss_assets':[{'local_path':'C:/image','oss_url':'https://oss.test/image'}]}
    class Orders:library=Library()
    scope={'groups':[],'orders':[],'purchases':[],'skus':['SKU'],'suppliers':['s2b']}
    result=purchase_scope.invoke(Orders(),scope,{},'get_product',['SKU'],{})
    assert result['output_folder']=='' and result['recipe']['sides'][0]['source_path']==''
    assert result['oss_assets'][0]['local_path']==''
    assert result['oss_assets'][0]['oss_url']=='https://oss.test/image'
    with pytest.raises(ConflictError):purchase_scope.invoke(Orders(),scope,{},'get_product',['OTHER'],{})
