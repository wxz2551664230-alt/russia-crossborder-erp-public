from sqlalchemy import event
from erp.core.database import SessionLocal,engine
from erp.models import Store,CatalogProduct,Publication,PublicationStore
from erp.api.routes.publications import list_publications

def test_stage_filter_precedes_pagination_and_queue_never_reads_gallery():
    with SessionLocal() as session:
        store=Store(platform='ozon',name='test');product=CatalogProduct(sku='SKU')
        session.add_all([store,product]);session.flush()
        for index,stage in enumerate(['completed','stock_pending','promotion_pending']):
            p=Publication(batch_id='test',product_id=product.id,status='mixed',product_snapshot_json='{"assets":["large history"]}')
            session.add(p);session.flush()
            session.add(PublicationStore(publication_id=p.id,store_id=store.id,offer_id=str(index),status=stage,idempotency_key=str(index)))
        session.commit();session.expunge_all()
        statements=[]
        def capture(conn,cursor,sql,params,context,many):statements.append(sql.lower())
        event.listen(engine,'before_cursor_execute',capture)
        try:
            data=list_publications(page=1,page_size=1,product_id='',status='',store_status='stock_pending',compact=True,session=session)
        finally:event.remove(engine,'before_cursor_execute',capture)
        assert data['total']==1 and len(data['items'])==1
        item=data['items'][0]
        assert item['sku']=='SKU' and item['stores'][0]['status']=='stock_pending'
        assert 'product_snapshot' not in item and 'assets' not in item['stores'][0]
        assert not any('catalog_product_assets' in sql or 'from assets' in sql for sql in statements)
