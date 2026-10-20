"""add ad campaign metrics

Revision ID: 8b9d7c6a5e4f
Revises: 2136f62146aa
Create Date: 2026-09-08 01:30:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8b9d7c6a5e4f"
down_revision: Union[str, None] = "2136f62146aa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "ad_campaign_daily_metrics" not in tables:
        op.create_table(
        "ad_campaign_daily_metrics",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("store_id", sa.String(length=32), nullable=False),
        sa.Column("campaign_id", sa.String(length=200), nullable=False),
        sa.Column("campaign_name", sa.String(length=500), nullable=False),
        sa.Column("sku_id", sa.String(length=200), nullable=False),
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column("impressions", sa.Integer(), nullable=False),
        sa.Column("clicks", sa.Integer(), nullable=False),
        sa.Column("add_to_cart", sa.Integer(), nullable=False),
        sa.Column("sold_qty", sa.Integer(), nullable=False),
        sa.Column("ad_spend", sa.Float(), nullable=False),
        sa.Column("ad_sales", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_id", "campaign_id", "sku_id", "metric_date", name="uq_ad_campaign_sku_date"),
        )
        op.create_index("idx_ad_campaign_store_date", "ad_campaign_daily_metrics", ["store_id", "metric_date"], unique=False)
        op.create_index("idx_ad_campaign_store_campaign", "ad_campaign_daily_metrics", ["store_id", "campaign_id"], unique=False)
    columns = {item["name"] for item in inspector.get_columns("ad_skus")}
    if "weak_label" not in columns:
        op.add_column("ad_skus", sa.Column("weak_label", sa.String(length=50), server_default="", nullable=False))
    if "data_maturity" not in columns:
        op.add_column("ad_skus", sa.Column("data_maturity", sa.String(length=20), server_default="LEVEL_0", nullable=False))
    op.create_table(
        "ad_campaigns",
        sa.Column("id", sa.String(length=32), nullable=False), sa.Column("store_id", sa.String(length=32), nullable=False),
        sa.Column("campaign_id", sa.String(length=200), nullable=False), sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("state", sa.String(length=80), nullable=False), sa.Column("payment_type", sa.String(length=80), nullable=False),
        sa.Column("expense_strategy", sa.String(length=120), nullable=False), sa.Column("budget_type", sa.String(length=80), nullable=False),
        sa.Column("budget", sa.Float(), nullable=True), sa.Column("previous_budget", sa.Float(), nullable=True),
        sa.Column("daily_budget", sa.Float(), nullable=True), sa.Column("weekly_budget", sa.Float(), nullable=True),
        sa.Column("last_change_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remote_created_at", sa.DateTime(timezone=True), nullable=True), sa.Column("remote_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_id", "campaign_id", name="uq_ad_campaign_store_external"),
    )
    op.create_table(
        "ad_campaign_skus",
        sa.Column("id", sa.String(length=32), nullable=False), sa.Column("ad_campaign_id", sa.String(length=32), nullable=False),
        sa.Column("sku_id", sa.String(length=200), nullable=False), sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("bid", sa.Float(), nullable=True), sa.Column("target_drr", sa.Float(), nullable=True), sa.Column("previous_target_drr", sa.Float(), nullable=True),
        sa.Column("last_change_time", sa.DateTime(timezone=True), nullable=True), sa.Column("status", sa.String(length=80), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ad_campaign_id"], ["ad_campaigns.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ad_campaign_id", "sku_id", name="uq_ad_campaign_sku"),
    )


def downgrade() -> None:
    op.drop_table("ad_campaign_skus")
    op.drop_table("ad_campaigns")
    op.drop_column("ad_skus", "data_maturity")
    op.drop_column("ad_skus", "weak_label")
    op.drop_index("idx_ad_campaign_store_campaign", table_name="ad_campaign_daily_metrics")
    op.drop_index("idx_ad_campaign_store_date", table_name="ad_campaign_daily_metrics")
    op.drop_table("ad_campaign_daily_metrics")
