"""Execute the original procurement workflow with task-scoped shared data."""
import asyncio
from contextvars import ContextVar

from purchase_journal import write_once


def execute(work, job, post, check, *, stage=lambda _value: None):
    stage('import_purchase_dependencies')
    from erp.domains.orders.legacy_engine import HicustomOrderQueue, OrderError
    from erp.domains.orders.service import supplier_client
    stage('purchase_snapshot')
    snapshot=post('/purchase-submit-snapshot',{})
    if 'completed_result' in snapshot:return snapshot['completed_result']
    settings=snapshot['config'];target=snapshot['target_id'];kind=snapshot['target_type']
    group_context=ContextVar('purchase_group',default=target)
    clients=[]

    class Orders:
        @property
        def library(self):return self
        def __getattr__(self,name):
            def call(*args,**kwargs):
                check()
                return post('/purchase-operation',{'method':name,'args':list(args),'kwargs':kwargs})
            return call
    orders=Orders()

    class Supplier:
        def __init__(self,provider):
            self.provider=provider;self.client=supplier_client(None,provider,settings);clients.append(self.client)
        def __getattr__(self,name):
            async def call(*args,**kwargs):
                check()
                async def invoke():
                    if name in {'create_order','create_product','upload_gallery'}:
                        post('/purchase-write-intent',{})
                    return await getattr(self.client,name)(*args,**kwargs)
                if name not in {'create_order','create_product','upload_gallery'}:
                    result=await invoke();check();return result
                if kind=='group':
                    # Recheck cancellations immediately before external writes.
                    orders.assert_purchase_sources_active([group_context.get()])
                suffix=str(kwargs.get('external_id') or '') if name=='upload_gallery' else ''
                operation=f'{self.provider}:{group_context.get()}:{name}:{suffix}'
                return await write_once(work/'purchase-writes',operation,{'args':args,'kwargs':kwargs},invoke,check)
            return call

    async def loader(provider='hicustom'):return Supplier(provider)
    class Queue(HicustomOrderQueue):
        async def _prepare_batch_product(self,group_id,client,config):
            token=group_context.set(group_id)
            try:return await super()._prepare_batch_product(group_id,client,config)
            finally:group_context.reset(token)
        async def _prepare_s2b_batch_product(self,group_id,client,config):
            token=group_context.set(group_id)
            try:return await super()._prepare_s2b_batch_product(group_id,client,config)
            finally:group_context.reset(token)
    def current():return orders.get_batch_group(target) if kind=='group' else orders.get_order(target)
    try:
        stage('purchase_preflight')
        before=current()
        if before.get('submission_status')!='submitted':
            stage('purchase_supplier_workflow')
            queue=Queue(orders,lambda:settings,loader)
            asyncio.run(queue.process_batch(target) if kind=='group' else queue.process(target))
        stage('purchase_result_sync')
        after=current()
        if after.get('submission_status')=='unknown':
            raise OrderError('采购结果未知，请先核对供应商原订单号',outcome_unknown=True)
        if after.get('submission_status')!='submitted':raise OrderError('采购尚未完成，请查看订单状态')
        return post('/purchase-submit-result',{})
    finally:
        for client in clients:client.close()
