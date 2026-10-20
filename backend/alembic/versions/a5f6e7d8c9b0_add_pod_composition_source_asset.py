"""link native POD composition sides to durable source assets

Revision ID: a5f6e7d8c9b0
Revises: f4e5d6c7b8a9
Create Date: 2026-09-09 13:20:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a5f6e7d8c9b0"
down_revision: Union[str, None] = "f4e5d6c7b8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("pod_composition_sides") as batch:
        batch.add_column(sa.Column("asset_id", sa.String(32), nullable=True))
        batch.create_foreign_key(
            "fk_pod_composition_sides_asset_id_assets",
            "assets",
            ["asset_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index("idx_pod_composition_sides_asset", ["asset_id"])


def downgrade() -> None:
    with op.batch_alter_table("pod_composition_sides") as batch:
        batch.drop_index("idx_pod_composition_sides_asset")
        batch.drop_constraint("fk_pod_composition_sides_asset_id_assets", type_="foreignkey")
        batch.drop_column("asset_id")
