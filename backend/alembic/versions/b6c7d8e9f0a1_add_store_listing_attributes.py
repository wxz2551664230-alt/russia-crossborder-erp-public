"""store OZON attributes per store listing

Revision ID: b6c7d8e9f0a1
Revises: a5f6e7d8c9b0
Create Date: 2026-09-11 12:20:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b6c7d8e9f0a1"
down_revision: Union[str, None] = "a5f6e7d8c9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("store_listings") as batch:
        batch.add_column(sa.Column("attributes_json", sa.Text(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("complex_attributes_json", sa.Text(), nullable=False, server_default="[]"))
    op.execute("UPDATE store_listings SET attributes_json = COALESCE((SELECT attributes_json FROM listings WHERE listings.id = store_listings.listing_id), '[]')")
    op.execute("UPDATE store_listings SET complex_attributes_json = COALESCE((SELECT complex_attributes_json FROM listings WHERE listings.id = store_listings.listing_id), '[]')")


def downgrade() -> None:
    with op.batch_alter_table("store_listings") as batch:
        batch.drop_column("complex_attributes_json")
        batch.drop_column("attributes_json")
