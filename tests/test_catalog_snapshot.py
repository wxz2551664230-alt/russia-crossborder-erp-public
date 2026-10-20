import gzip
import hashlib
import json
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from erp.models import Base, CatalogProduct, CatalogVariant, Asset, CatalogProductAsset
from scripts.sync_shared_catalog import export_snapshot, apply_snapshot, public_facts


def test_catalog_copy_preserves_cloud_edits_and_rolls_back_bad_batch(tmp_path):
    source=create_engine('sqlite:///'+str(tmp_path/'source.db'))
    target=create_engine('sqlite:///'+str(tmp_path/'target.db'))
    Base.metadata.create_all(source);Base.metadata.create_all(target)
    with Session(source) as db:
        db.add(CatalogProduct(id='p1',sku='REAL-SKU',name='Product',notes='source note',purchase_price_cny=12.5));db.flush()
        db.add(CatalogVariant(id='v1',product_id='p1',sku='SIZE-M',size='M'))
        db.add(Asset(id='a1',kind='image',name='image',source='test',local_path='C:/private/file.jpg',oss_url='https://example-assets.oss-cn-chengdu.aliyuncs.com/test.jpg'));db.flush()
        db.add(CatalogProductAsset(id='l1',product_id='p1',variant_id='v1',asset_id='a1'));db.commit()
    with Session(target) as db:
        db.add(CatalogProduct(id='p1',sku='REAL-SKU',notes='cloud edit'));db.commit()
    directory=tmp_path/'snapshot'; export_snapshot(tmp_path/'source.db',directory)
    apply_snapshot(target,directory);apply_snapshot(target,directory)
    with Session(target) as db:
        product=db.get(CatalogProduct,'p1')
        assert product.notes=='cloud edit' and product.purchase_price_cny==12.5
        assert len(product.variants)==1 and len(product.asset_links)==1
        assert db.get(Asset,'a1').local_path==''
    # Make the final relation cross products. Earlier changes must all roll back.
    with Session(source) as db:
        db.get(CatalogProduct,'p1').purchase_price_cny=99
        db.add(CatalogProduct(id='p2',sku='SECOND'));db.flush()
        db.get(CatalogVariant,'v1').product_id='p2';db.commit()
    invalid=tmp_path/'invalid';export_snapshot(tmp_path/'source.db',invalid)
    with pytest.raises(ValueError,match='relations'):apply_snapshot(target,invalid)
    with Session(target) as db: assert db.get(CatalogProduct,'p1').purchase_price_cny==12.5
    (directory/'assets.jsonl.gz').write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='checksum'):apply_snapshot(target,directory)
    assert public_facts({'name':'ok','nested':{'password':'hidden','size':'M'}})=={'name':'ok','nested':{'size':'M'}}
