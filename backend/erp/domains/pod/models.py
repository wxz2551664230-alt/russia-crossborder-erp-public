from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from erp.core.database import Base


def _id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PodTimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class PodLegacyMixin:
    legacy_source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    legacy_id: Mapped[str | None] = mapped_column(String(300), nullable=True)


class PodArtworkCategory(PodTimestampMixin, PodLegacyMixin, Base):
    __tablename__ = "pod_artwork_categories"
    __table_args__ = (
        UniqueConstraint("name", name="uq_pod_artwork_categories_name"),
        UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_artwork_categories_legacy"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class PodArtwork(PodTimestampMixin, PodLegacyMixin, Base):
    __tablename__ = "pod_artworks"
    __table_args__ = (
        UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_artworks_legacy"),
        Index("idx_pod_artworks_sha256", "sha256"),
        Index("idx_pod_artworks_category_updated", "category_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    category_id: Mapped[str | None] = mapped_column(
        ForeignKey("pod_artwork_categories.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(150), default="application/octet-stream", nullable=False)
    source: Mapped[str] = mapped_column(String(80), default="legacy", nullable=False)
    source_relative_path: Mapped[str] = mapped_column(Text, default="", nullable=False)
    oss_sync_status: Mapped[str] = mapped_column(String(30), default="legacy", nullable=False)
    oss_sync_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    asset = relationship("Asset")
    category = relationship("PodArtworkCategory")


class PodProductCategory(PodTimestampMixin, PodLegacyMixin, Base):
    __tablename__ = "pod_product_categories"
    __table_args__ = (
        UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_product_categories_legacy"),
        UniqueConstraint("name", name="uq_pod_product_categories_name"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class PodCategoryRule(PodTimestampMixin, PodLegacyMixin, Base):
    __tablename__ = "pod_category_rules"
    __table_args__ = (
        UniqueConstraint("supplier", "product_type_id", name="uq_pod_category_rules_supplier_type"),
        UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_category_rules_legacy"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    category_id: Mapped[str] = mapped_column(ForeignKey("pod_product_categories.id", ondelete="CASCADE"), nullable=False)
    supplier: Mapped[str] = mapped_column(String(40), default="hicustom", nullable=False)
    product_type_id: Mapped[int] = mapped_column(Integer, nullable=False)


class PodProductVersion(PodTimestampMixin, PodLegacyMixin, Base):
    __tablename__ = "pod_product_versions"
    __table_args__ = (
        UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_product_versions_legacy"),
        UniqueConstraint("catalog_product_id", "version_no", name="uq_pod_product_versions_number"),
        Index("idx_pod_product_versions_product_current", "catalog_product_id", "is_current"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    catalog_product_id: Mapped[str] = mapped_column(ForeignKey("catalog_products.id", ondelete="CASCADE"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    legacy_job_id: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    job_status: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    supplier: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    product_type_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    default_color_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    default_view_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_code: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    product_code_status: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    legacy_output_folder: Mapped[str] = mapped_column(Text, default="", nullable=False)
    recipe_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    assets_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)


class PodProductCodeHistory(PodTimestampMixin, PodLegacyMixin, Base):
    __tablename__ = "pod_product_code_history"
    __table_args__ = (
        UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_product_code_history_legacy"),
        Index("idx_pod_product_code_history_version", "version_id", "last_seen_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    version_id: Mapped[str] = mapped_column(ForeignKey("pod_product_versions.id", ondelete="CASCADE"), nullable=False)
    product_code: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    first_seen_at: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    last_seen_at: Mapped[str] = mapped_column(String(50), default="", nullable=False)


class PodColorImageGroup(PodTimestampMixin, PodLegacyMixin, Base):
    __tablename__ = "pod_color_image_groups"
    __table_args__ = (
        UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_color_image_groups_legacy"),
        Index("idx_pod_color_image_groups_version", "version_id", "color_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    version_id: Mapped[str] = mapped_column(ForeignKey("pod_product_versions.id", ondelete="CASCADE"), nullable=False)
    color_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    color_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    rendering_urls_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    saved_files_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)


class PodDeletionTombstone(PodLegacyMixin, Base):
    __tablename__ = "pod_deletion_tombstones"
    __table_args__ = (
        UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_deletion_tombstones_legacy"),
        Index("idx_pod_deletion_tombstones_sku", "sku"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    legacy_job_id: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    sku: Mapped[str] = mapped_column(String(200), nullable=False)
    product_name: Mapped[str] = mapped_column(String(600), default="", nullable=False)
    deleted_at: Mapped[str] = mapped_column(String(50), default="", nullable=False)


class PodSupplierCatalogSnapshot(PodTimestampMixin, PodLegacyMixin, Base):
    __tablename__ = "pod_supplier_catalog_snapshots"
    __table_args__ = (
        UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_supplier_catalog_snapshots_legacy"),
        Index("idx_pod_supplier_catalog_provider_current", "provider", "is_current"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    product_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    category_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fetched_at: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)


class PodComposition(PodTimestampMixin, PodLegacyMixin, Base):
    __tablename__ = "pod_compositions"
    __table_args__ = (
        UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_compositions_legacy"),
        Index("idx_pod_compositions_status_created", "status", "created_at"),
        Index("idx_pod_compositions_product", "catalog_product_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    catalog_product_id: Mapped[str | None] = mapped_column(ForeignKey("catalog_products.id", ondelete="SET NULL"), nullable=True)
    supplier: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    stage_text: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    product_name: Mapped[str] = mapped_column(String(600), default="", nullable=False)
    library_sku: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    product_type_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    default_color_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    default_view_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remote_product_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    product_code: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    external_id: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    outcome_unknown: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    selected_size_ids_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    rendering_groups_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    oss_assets_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    config_snapshot_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)


class PodCompositionSide(PodTimestampMixin, PodLegacyMixin, Base):
    __tablename__ = "pod_composition_sides"
    __table_args__ = (
        UniqueConstraint("composition_id", "view_id", name="uq_pod_composition_sides_view"),
        UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_composition_sides_legacy"),
        Index("idx_pod_composition_sides_gallery", "supplier", "gallery_code"),
        Index("idx_pod_composition_sides_asset", "asset_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    composition_id: Mapped[str] = mapped_column(ForeignKey("pod_compositions.id", ondelete="CASCADE"), nullable=False)
    artwork_id: Mapped[str | None] = mapped_column(ForeignKey("pod_artworks.id", ondelete="SET NULL"), nullable=True)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=True)
    supplier: Mapped[str] = mapped_column(String(40), nullable=False)
    view_id: Mapped[int] = mapped_column(Integer, nullable=False)
    view_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    gallery_code: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    source_filename: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    source_legacy_path: Mapped[str] = mapped_column(Text, default="", nullable=False)
    mime_type: Mapped[str] = mapped_column(String(150), default="application/octet-stream", nullable=False)
    rotation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    width: Mapped[float | None] = mapped_column(Float, nullable=True)
    height: Mapped[float | None] = mapped_column(Float, nullable=True)
    top_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    top_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    upload_status: Mapped[str] = mapped_column(String(30), default="completed", nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    asset = relationship("Asset", foreign_keys=[asset_id])


class PodRemoteGalleryAsset(PodTimestampMixin, PodLegacyMixin, Base):
    __tablename__ = "pod_remote_gallery_assets"
    __table_args__ = (
        UniqueConstraint("supplier", "gallery_code", name="uq_pod_remote_gallery_supplier_code"),
        UniqueConstraint("legacy_source", "legacy_id", name="uq_pod_remote_gallery_legacy"),
        Index("idx_pod_remote_gallery_usage", "usage", "protected", "remote_deleted"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    artwork_id: Mapped[str | None] = mapped_column(ForeignKey("pod_artworks.id", ondelete="SET NULL"), nullable=True)
    composition_side_id: Mapped[str | None] = mapped_column(
        ForeignKey("pod_composition_sides.id", ondelete="SET NULL"), nullable=True
    )
    supplier: Mapped[str] = mapped_column(String(40), nullable=False)
    gallery_code: Mapped[str] = mapped_column(String(300), nullable=False)
    usage: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    protected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cleanup_candidate: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    remote_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    remote_deleted_at: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    sku: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    product_name: Mapped[str] = mapped_column(String(600), default="", nullable=False)
    filename: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    order_ids_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    order_statuses_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)


class PodLegacyImportItem(PodTimestampMixin, Base):
    __tablename__ = "pod_legacy_import_items"
    __table_args__ = (
        UniqueConstraint("import_run_id", "entity_type", "legacy_id", name="uq_pod_legacy_import_item"),
        Index("idx_pod_legacy_import_items_status", "import_run_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    import_run_id: Mapped[str] = mapped_column(ForeignKey("catalog_import_runs.id", ondelete="CASCADE"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    legacy_id: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="imported", nullable=False)
    details_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
