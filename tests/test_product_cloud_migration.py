import gzip,json
from sqlalchemy import create_engine,select
from erp.core.database import SessionLocal,engine
from erp.models import Base,CatalogProduct
from scripts.migrate_product_cloud_records import export,apply,sanitize

def test_insert_only_migration_preserves_cloud_edits(tmp_path):
    with SessionLocal() as db:
        db.add(CatalogProduct(id='shared',sku='SHARED',name='Local name',notes='Local note',source_type='follow'))
        db.add(CatalogProduct(id='new',sku='NEW',name='New product',source_type='follow'))
        db.commit()
    directory=tmp_path/'export'
    export(__import__('pathlib').Path(engine.url.database),directory)
    cloud=create_engine('sqlite:///'+str(tmp_path/'cloud.db'))
    Base.metadata.create_all(cloud)
    with cloud.begin() as db:
        db.execute(CatalogProduct.__table__.insert().values(id='shared',sku='SHARED',name='Cloud edited',notes='Cloud note',source_type='follow'))
    apply(directory,cloud)
    apply(directory,cloud)
    with cloud.connect() as db:
        row=db.execute(select(CatalogProduct.__table__).where(CatalogProduct.id=='shared')).mappings().one()
        assert row['name']=='Cloud edited' and row['notes']=='Cloud note'
        assert len(db.execute(select(CatalogProduct.id)).all())==2
    assert sanitize({'nested':{'secrets_encrypted':'no','api-key':'no','price':3}})=={'nested':{'price':3}}
