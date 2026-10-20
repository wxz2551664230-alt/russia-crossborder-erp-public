"""add product library and publication package fields

Revision ID: c1a2b3c4d5e6
Revises: 8b9d7c6a5e4f
Create Date: 2026-09-08 21:15:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1a2b3c4d5e6"
down_revision: Union[str, None] = "8b9d7c6a5e4f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    recreate = "always" if op.get_bind().dialect.name == "sqlite" else "auto"
    with op.batch_alter_table("catalog_products", recreate=recreate) as batch:
        batch.drop_constraint("uq_catalog_products_sku", type_="unique")
        batch.add_column(sa.Column("group_code", sa.String(200), server_default="", nullable=False))
        batch.add_column(sa.Column("source_type", sa.String(30), server_default="self_selected", nullable=False))
        batch.add_column(sa.Column("source_url", sa.Text(), server_default="", nullable=False))
        batch.add_column(sa.Column("supplier", sa.String(120), server_default="", nullable=False))
        batch.add_column(sa.Column("supplier_product_id", sa.String(200), server_default="", nullable=False))
        batch.add_column(sa.Column("purchase_price_cny", sa.Float(), nullable=True))
        batch.add_column(sa.Column("domestic_delivery_cny", sa.Float(), nullable=True))
        batch.add_column(sa.Column("currency", sa.String(10), server_default="CNY", nullable=False))
        batch.add_column(sa.Column("package_length_mm", sa.Float(), nullable=True))
        batch.add_column(sa.Column("package_width_mm", sa.Float(), nullable=True))
        batch.add_column(sa.Column("package_height_mm", sa.Float(), nullable=True))
        batch.add_column(sa.Column("package_weight_g", sa.Float(), nullable=True))
        batch.add_column(sa.Column("description_category_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("type_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("category_display", sa.String(800), server_default="", nullable=False))
        batch.add_column(sa.Column("notes", sa.Text(), server_default="", nullable=False))
        batch.add_column(sa.Column("production_json", sa.Text(), server_default="{}", nullable=False))
        batch.add_column(sa.Column("source_snapshot_json", sa.Text(), server_default="{}", nullable=False))
        batch.create_index("idx_catalog_source_group", ["source_type", "group_code"])
        batch.create_index("idx_catalog_product_sku", ["sku"])

    with op.batch_alter_table("catalog_variants", recreate=recreate) as batch:
        batch.drop_constraint("uq_catalog_variants_sku", type_="unique")
        batch.add_column(sa.Column("supplier_product_id", sa.String(200), server_default="", nullable=False))
        batch.add_column(sa.Column("supplier_sku_id", sa.String(200), server_default="", nullable=False))
        batch.add_column(sa.Column("specification", sa.String(500), server_default="", nullable=False))
        batch.add_column(sa.Column("purchase_price_cny", sa.Float(), nullable=True))
        batch.add_column(sa.Column("domestic_delivery_cny", sa.Float(), nullable=True))
        batch.add_column(sa.Column("package_length_mm", sa.Float(), nullable=True))
        batch.add_column(sa.Column("package_width_mm", sa.Float(), nullable=True))
        batch.add_column(sa.Column("package_height_mm", sa.Float(), nullable=True))
        batch.add_column(sa.Column("barcode", sa.String(200), server_default="", nullable=False))
        batch.add_column(sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False))
        batch.create_index("idx_catalog_variant_sku", ["sku"])

    op.create_table(
        "catalog_product_assets",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("product_id", sa.String(32), nullable=False),
        sa.Column("variant_id", sa.String(32), nullable=True),
        sa.Column("asset_id", sa.String(32), nullable=False),
        sa.Column("role", sa.String(40), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("source_kind", sa.String(50), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["catalog_products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["variant_id"], ["catalog_variants.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_catalog_assets_product_current", "catalog_product_assets", ["product_id", "is_current", "role", "position"])
    op.create_index("idx_catalog_assets_variant", "catalog_product_assets", ["variant_id"])

    op.create_table(
        "catalog_duplicate_suggestions",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("product_id", sa.String(32), nullable=False),
        sa.Column("candidate_product_id", sa.String(32), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("reasons_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["candidate_product_id"], ["catalog_products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["catalog_products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", "candidate_product_id", name="uq_catalog_duplicate_pair"),
    )
    op.create_index("idx_catalog_duplicates_status", "catalog_duplicate_suggestions", ["status", "score"])

    op.create_table(
        "catalog_import_runs",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("job_id", sa.String(32), nullable=True),
        sa.Column("counts_json", sa.Text(), nullable=False),
        sa.Column("errors_json", sa.Text(), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_catalog_imports_created", "catalog_import_runs", ["kind", "created_at"])

    op.create_table(
        "publications",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("batch_id", sa.String(32), nullable=False),
        sa.Column("product_id", sa.String(32), nullable=False),
        sa.Column("variant_id", sa.String(32), nullable=True),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("requirements_json", sa.Text(), nullable=False),
        sa.Column("image_policy_json", sa.Text(), nullable=False),
        sa.Column("save_generated_to_catalog", sa.Boolean(), nullable=False),
        sa.Column("submit_via_api", sa.Boolean(), nullable=False),
        sa.Column("product_snapshot_json", sa.Text(), nullable=False),
        sa.Column("job_id", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["product_id"], ["catalog_products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["variant_id"], ["catalog_variants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_publications_batch", "publications", ["batch_id", "created_at"])
    op.create_index("idx_publications_product", "publications", ["product_id", "created_at"])
    op.create_index("idx_publications_status", "publications", ["status", "updated_at"])

    op.create_table(
        "publication_stores",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("publication_id", sa.String(32), nullable=False),
        sa.Column("store_id", sa.String(32), nullable=False),
        sa.Column("offer_id", sa.String(200), nullable=False),
        sa.Column("status", sa.String(60), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("attributes_json", sa.Text(), nullable=False),
        sa.Column("complex_attributes_json", sa.Text(), nullable=False),
        sa.Column("assets_json", sa.Text(), nullable=False),
        sa.Column("pricing_json", sa.Text(), nullable=False),
        sa.Column("package_json", sa.Text(), nullable=False),
        sa.Column("settings_snapshot_json", sa.Text(), nullable=False),
        sa.Column("external_task_id", sa.String(200), nullable=False),
        sa.Column("external_product_id", sa.String(200), nullable=False),
        sa.Column("stock_status", sa.String(40), nullable=False),
        sa.Column("promotion_status", sa.String(40), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["publication_id"], ["publications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_publication_store_idempotency"),
        sa.UniqueConstraint("publication_id", "store_id", name="uq_publication_store"),
    )
    op.create_index("idx_publication_stores_status", "publication_stores", ["store_id", "status"])

    with op.batch_alter_table("listings") as batch:
        batch.add_column(sa.Column("requirements_json", sa.Text(), server_default="{}", nullable=False))
        batch.add_column(sa.Column("image_policy_json", sa.Text(), server_default="{}", nullable=False))
        batch.add_column(sa.Column("save_generated_to_catalog", sa.Boolean(), server_default=sa.false(), nullable=False))
        batch.add_column(sa.Column("publication_mode", sa.String(20), server_default="api", nullable=False))
        batch.add_column(sa.Column("catalog_snapshot_json", sa.Text(), server_default="{}", nullable=False))


def downgrade() -> None:
    recreate = "always" if op.get_bind().dialect.name == "sqlite" else "auto"
    with op.batch_alter_table("listings") as batch:
        batch.drop_column("catalog_snapshot_json")
        batch.drop_column("publication_mode")
        batch.drop_column("save_generated_to_catalog")
        batch.drop_column("image_policy_json")
        batch.drop_column("requirements_json")
    op.drop_index("idx_publication_stores_status", table_name="publication_stores")
    op.drop_table("publication_stores")
    op.drop_index("idx_publications_status", table_name="publications")
    op.drop_index("idx_publications_product", table_name="publications")
    op.drop_index("idx_publications_batch", table_name="publications")
    op.drop_table("publications")
    op.drop_index("idx_catalog_imports_created", table_name="catalog_import_runs")
    op.drop_table("catalog_import_runs")
    op.drop_index("idx_catalog_duplicates_status", table_name="catalog_duplicate_suggestions")
    op.drop_table("catalog_duplicate_suggestions")
    op.drop_index("idx_catalog_assets_variant", table_name="catalog_product_assets")
    op.drop_index("idx_catalog_assets_product_current", table_name="catalog_product_assets")
    op.drop_table("catalog_product_assets")
    with op.batch_alter_table("catalog_variants", recreate=recreate) as batch:
        batch.drop_index("idx_catalog_variant_sku")
        for column in ("active", "barcode", "package_height_mm", "package_width_mm", "package_length_mm", "domestic_delivery_cny", "purchase_price_cny", "specification", "supplier_sku_id", "supplier_product_id"):
            batch.drop_column(column)
        batch.create_unique_constraint("uq_catalog_variants_sku", ["sku"])
    with op.batch_alter_table("catalog_products", recreate=recreate) as batch:
        batch.drop_index("idx_catalog_product_sku")
        batch.drop_index("idx_catalog_source_group")
        for column in ("source_snapshot_json", "production_json", "notes", "category_display", "type_id", "description_category_id", "package_weight_g", "package_height_mm", "package_width_mm", "package_length_mm", "currency", "domestic_delivery_cny", "purchase_price_cny", "supplier_product_id", "supplier", "source_url", "source_type", "group_code"):
            batch.drop_column(column)
        batch.create_unique_constraint("uq_catalog_products_sku", ["sku"])
