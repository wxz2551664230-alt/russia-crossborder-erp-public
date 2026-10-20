"""Dedicated Amazon US listing batches and permanent store SKU ledger."""
from alembic import op
import sqlalchemy as sa
revision='b2c3d4e5f6a7'
down_revision='a1b2c3d4e5f6'
branch_labels=None
depends_on=None
def times():
    return [sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False)]
def upgrade():
    op.create_table('amazon_listing_batches',sa.Column('id',sa.String(32),primary_key=True),
        sa.Column('store_id',sa.String(32),sa.ForeignKey('stores.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('created_by',sa.String(32),nullable=False),sa.Column('worker_id',sa.String(100),nullable=False),
        sa.Column('status',sa.String(40),nullable=False),sa.Column('job_id',sa.String(32),sa.ForeignKey('jobs.id',ondelete='SET NULL')),
        *(sa.Column(k,sa.Text,nullable=False) for k in ('snapshot_json','draft_json','result_json','last_error')),*times())
    for k in ('store_id','status'):op.create_index('ix_amazon_listing_batches_'+k,'amazon_listing_batches',[k])
    op.create_table('amazon_listing_items',sa.Column('id',sa.String(32),primary_key=True),
        sa.Column('batch_id',sa.String(32),sa.ForeignKey('amazon_listing_batches.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('store_id',sa.String(32),sa.ForeignKey('stores.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('product_id',sa.String(32),sa.ForeignKey('catalog_products.id',ondelete='RESTRICT')),
        sa.Column('variant_id',sa.String(32),sa.ForeignKey('catalog_variants.id',ondelete='RESTRICT')),
        sa.Column('sku',sa.String(40),nullable=False),sa.Column('parent_sku',sa.String(40),nullable=False),
        sa.Column('status',sa.String(40),nullable=False),sa.Column('asin',sa.String(20),nullable=False),
        *(sa.Column(k,sa.Text,nullable=False) for k in ('body_json','pricing_json','receipt_json')),
        *times(),sa.UniqueConstraint('store_id','sku',name='uq_amazon_store_sku'))
    for k in ('batch_id','status'):op.create_index('ix_amazon_listing_items_'+k,'amazon_listing_items',[k])
def downgrade():
    op.drop_table('amazon_listing_items');op.drop_table('amazon_listing_batches')
