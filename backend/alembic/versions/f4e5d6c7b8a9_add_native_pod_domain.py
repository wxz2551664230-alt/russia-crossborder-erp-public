"""add native POD domain and lossless legacy migration tables

Revision ID: f4e5d6c7b8a9
Revises: c1a2b3c4d5e6
Create Date: 2026-09-09 16:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4e5d6c7b8a9"
down_revision: Union[str, None] = "c1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _legacy() -> list[sa.Column]:
    return [
        sa.Column("legacy_source", sa.String(80), nullable=True),
        sa.Column("legacy_id", sa.String(300), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "pod_artwork_categories",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        *_timestamps(), *_legacy(),
        sa.UniqueConstraint("name", name="uq_pod_artwork_categories_name"),
        sa.UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_artwork_categories_legacy"),
    )
    op.create_table(
        "pod_artworks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("asset_id", sa.String(32), nullable=False),
        sa.Column("category_id", sa.String(32), nullable=True),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(150), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("source_relative_path", sa.Text(), nullable=False),
        sa.Column("oss_sync_status", sa.String(30), nullable=False),
        sa.Column("oss_sync_error", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        *_timestamps(), *_legacy(),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["category_id"], ["pod_artwork_categories.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_artworks_legacy"),
    )
    op.create_index("idx_pod_artworks_sha256", "pod_artworks", ["sha256"])
    op.create_index("idx_pod_artworks_category_updated", "pod_artworks", ["category_id", "updated_at"])

    op.create_table(
        "pod_product_categories",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        *_timestamps(), *_legacy(),
        sa.UniqueConstraint("name", name="uq_pod_product_categories_name"),
        sa.UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_product_categories_legacy"),
    )
    op.create_table(
        "pod_category_rules",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("category_id", sa.String(32), nullable=False),
        sa.Column("supplier", sa.String(40), nullable=False),
        sa.Column("product_type_id", sa.Integer(), nullable=False),
        *_timestamps(), *_legacy(),
        sa.ForeignKeyConstraint(["category_id"], ["pod_product_categories.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("supplier", "product_type_id", name="uq_pod_category_rules_supplier_type"),
        sa.UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_category_rules_legacy"),
    )
    op.create_table(
        "pod_product_versions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("catalog_product_id", sa.String(32), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("legacy_job_id", sa.String(300), nullable=False),
        sa.Column("job_status", sa.String(40), nullable=False),
        sa.Column("supplier", sa.String(40), nullable=False),
        sa.Column("product_type_id", sa.Integer(), nullable=True),
        sa.Column("default_color_id", sa.Integer(), nullable=True),
        sa.Column("default_view_id", sa.Integer(), nullable=True),
        sa.Column("product_code", sa.String(200), nullable=False),
        sa.Column("product_code_status", sa.String(40), nullable=False),
        sa.Column("legacy_output_folder", sa.Text(), nullable=False),
        sa.Column("recipe_json", sa.Text(), nullable=False),
        sa.Column("assets_json", sa.Text(), nullable=False),
        *_timestamps(), *_legacy(),
        sa.ForeignKeyConstraint(["catalog_product_id"], ["catalog_products.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_product_versions_legacy"),
        sa.UniqueConstraint("catalog_product_id", "version_no", name="uq_pod_product_versions_number"),
    )
    op.create_index("idx_pod_product_versions_product_current", "pod_product_versions", ["catalog_product_id", "is_current"])
    op.create_table(
        "pod_product_code_history",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("version_id", sa.String(32), nullable=False),
        sa.Column("product_code", sa.String(200), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("first_seen_at", sa.String(50), nullable=False),
        sa.Column("last_seen_at", sa.String(50), nullable=False),
        *_timestamps(), *_legacy(),
        sa.ForeignKeyConstraint(["version_id"], ["pod_product_versions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_product_code_history_legacy"),
    )
    op.create_index("idx_pod_product_code_history_version", "pod_product_code_history", ["version_id", "last_seen_at"])
    op.create_table(
        "pod_color_image_groups",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("version_id", sa.String(32), nullable=False),
        sa.Column("color_id", sa.Integer(), nullable=True),
        sa.Column("color_name", sa.String(200), nullable=False),
        sa.Column("rendering_urls_json", sa.Text(), nullable=False),
        sa.Column("saved_files_json", sa.Text(), nullable=False),
        *_timestamps(), *_legacy(),
        sa.ForeignKeyConstraint(["version_id"], ["pod_product_versions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_color_image_groups_legacy"),
    )
    op.create_index("idx_pod_color_image_groups_version", "pod_color_image_groups", ["version_id", "color_id"])
    op.create_table(
        "pod_deletion_tombstones",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("legacy_job_id", sa.String(300), nullable=False),
        sa.Column("sku", sa.String(200), nullable=False),
        sa.Column("product_name", sa.String(600), nullable=False),
        sa.Column("deleted_at", sa.String(50), nullable=False),
        *_legacy(),
        sa.UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_deletion_tombstones_legacy"),
    )
    op.create_index("idx_pod_deletion_tombstones_sku", "pod_deletion_tombstones", ["sku"])

    op.create_table(
        "pod_supplier_catalog_snapshots",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("product_count", sa.Integer(), nullable=False),
        sa.Column("category_count", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.String(50), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        *_timestamps(), *_legacy(),
        sa.UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_supplier_catalog_snapshots_legacy"),
    )
    op.create_index("idx_pod_supplier_catalog_provider_current", "pod_supplier_catalog_snapshots", ["provider", "is_current"])
    op.create_table(
        "pod_compositions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("job_id", sa.String(32), nullable=True),
        sa.Column("catalog_product_id", sa.String(32), nullable=True),
        sa.Column("supplier", sa.String(40), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("stage_text", sa.String(500), nullable=False),
        sa.Column("product_name", sa.String(600), nullable=False),
        sa.Column("library_sku", sa.String(200), nullable=False),
        sa.Column("product_type_id", sa.Integer(), nullable=True),
        sa.Column("default_color_id", sa.Integer(), nullable=True),
        sa.Column("default_view_id", sa.Integer(), nullable=True),
        sa.Column("remote_product_id", sa.String(200), nullable=False),
        sa.Column("product_code", sa.String(200), nullable=False),
        sa.Column("external_id", sa.String(300), nullable=False),
        sa.Column("outcome_unknown", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("selected_size_ids_json", sa.Text(), nullable=False),
        sa.Column("rendering_groups_json", sa.Text(), nullable=False),
        sa.Column("oss_assets_json", sa.Text(), nullable=False),
        sa.Column("config_snapshot_json", sa.Text(), nullable=False),
        sa.Column("raw_json", sa.Text(), nullable=False),
        *_timestamps(), *_legacy(),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["catalog_product_id"], ["catalog_products.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_compositions_legacy"),
    )
    op.create_index("idx_pod_compositions_status_created", "pod_compositions", ["status", "created_at"])
    op.create_index("idx_pod_compositions_product", "pod_compositions", ["catalog_product_id", "created_at"])
    op.create_table(
        "pod_composition_sides",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("composition_id", sa.String(32), nullable=False),
        sa.Column("artwork_id", sa.String(32), nullable=True),
        sa.Column("supplier", sa.String(40), nullable=False),
        sa.Column("view_id", sa.Integer(), nullable=False),
        sa.Column("view_name", sa.String(200), nullable=False),
        sa.Column("gallery_code", sa.String(300), nullable=False),
        sa.Column("source_filename", sa.String(500), nullable=False),
        sa.Column("source_legacy_path", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(150), nullable=False),
        sa.Column("rotation", sa.Integer(), nullable=False),
        sa.Column("width", sa.Float(), nullable=True),
        sa.Column("height", sa.Float(), nullable=True),
        sa.Column("top_x", sa.Float(), nullable=True),
        sa.Column("top_y", sa.Float(), nullable=True),
        sa.Column("upload_status", sa.String(30), nullable=False),
        sa.Column("raw_json", sa.Text(), nullable=False),
        *_timestamps(), *_legacy(),
        sa.ForeignKeyConstraint(["composition_id"], ["pod_compositions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["artwork_id"], ["pod_artworks.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("composition_id", "view_id", name="uq_pod_composition_sides_view"),
        sa.UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_composition_sides_legacy"),
    )
    op.create_index("idx_pod_composition_sides_gallery", "pod_composition_sides", ["supplier", "gallery_code"])
    op.create_table(
        "pod_remote_gallery_assets",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("artwork_id", sa.String(32), nullable=True),
        sa.Column("composition_side_id", sa.String(32), nullable=True),
        sa.Column("supplier", sa.String(40), nullable=False),
        sa.Column("gallery_code", sa.String(300), nullable=False),
        sa.Column("usage", sa.String(30), nullable=False),
        sa.Column("protected", sa.Boolean(), nullable=False),
        sa.Column("cleanup_candidate", sa.Boolean(), nullable=False),
        sa.Column("remote_deleted", sa.Boolean(), nullable=False),
        sa.Column("remote_deleted_at", sa.String(50), nullable=False),
        sa.Column("sku", sa.String(200), nullable=False),
        sa.Column("product_name", sa.String(600), nullable=False),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("order_ids_json", sa.Text(), nullable=False),
        sa.Column("order_statuses_json", sa.Text(), nullable=False),
        sa.Column("raw_json", sa.Text(), nullable=False),
        *_timestamps(), *_legacy(),
        sa.ForeignKeyConstraint(["artwork_id"], ["pod_artworks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["composition_side_id"], ["pod_composition_sides.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("supplier", "gallery_code", name="uq_pod_remote_gallery_supplier_code"),
        sa.UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_remote_gallery_legacy"),
    )
    op.create_index("idx_pod_remote_gallery_usage", "pod_remote_gallery_assets", ["usage", "protected", "remote_deleted"])
    op.create_table(
        "pod_legacy_import_items",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("import_run_id", sa.String(32), nullable=False),
        sa.Column("entity_type", sa.String(80), nullable=False),
        sa.Column("legacy_id", sa.String(300), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["import_run_id"], ["catalog_import_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("import_run_id", "entity_type", "legacy_id", name="uq_pod_legacy_import_item"),
    )
    op.create_index("idx_pod_legacy_import_items_status", "pod_legacy_import_items", ["import_run_id", "status"])


def downgrade() -> None:
    for table, indexes in [
        ("pod_legacy_import_items", ["idx_pod_legacy_import_items_status"]),
        ("pod_remote_gallery_assets", ["idx_pod_remote_gallery_usage"]),
        ("pod_composition_sides", ["idx_pod_composition_sides_gallery"]),
        ("pod_compositions", ["idx_pod_compositions_product", "idx_pod_compositions_status_created"]),
        ("pod_supplier_catalog_snapshots", ["idx_pod_supplier_catalog_provider_current"]),
        ("pod_deletion_tombstones", ["idx_pod_deletion_tombstones_sku"]),
        ("pod_color_image_groups", ["idx_pod_color_image_groups_version"]),
        ("pod_product_code_history", ["idx_pod_product_code_history_version"]),
        ("pod_product_versions", ["idx_pod_product_versions_product_current"]),
        ("pod_category_rules", []),
        ("pod_product_categories", []),
        ("pod_artworks", ["idx_pod_artworks_category_updated", "idx_pod_artworks_sha256"]),
        ("pod_artwork_categories", []),
    ]:
        for index in indexes:
            op.drop_index(index, table_name=table)
        op.drop_table(table)
