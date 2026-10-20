from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from erp.core.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class LegacyMixin:
    legacy_source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    legacy_id: Mapped[str | None] = mapped_column(String(300), nullable=True)


class IntegrationConnection(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "integration_connections"
    __table_args__ = (
        UniqueConstraint("provider", "name", name="uq_connections_provider_name"),
        UniqueConstraint("legacy_source", "legacy_id", name="uq_connections_legacy"),
        Index("idx_connections_provider_enabled", "provider", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_url: Mapped[str] = mapped_column(String(600), default="", nullable=False)
    config_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    secrets_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="untested", nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    stores: Mapped[list["Store"]] = relationship(back_populates="connection")


class Store(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "stores"
    __table_args__ = (
        UniqueConstraint("platform", "external_account_id", name="uq_stores_platform_external"),
        UniqueConstraint("legacy_source", "legacy_id", name="uq_stores_legacy"),
        Index("idx_stores_platform_enabled", "platform", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    external_account_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    offer_prefix: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="RUB", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    connection_id: Mapped[str | None] = mapped_column(
        ForeignKey("integration_connections.id", ondelete="SET NULL"), nullable=True
    )
    config_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    secrets_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)

    connection: Mapped[IntegrationConnection | None] = relationship(back_populates="stores")
    store_listings: Mapped[list["StoreListing"]] = relationship(back_populates="store")


class Asset(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("legacy_source", "legacy_id", name="uq_assets_legacy"),
        Index("idx_assets_sha256", "sha256"),
        Index("idx_assets_kind_created", "kind", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(50), default="file", nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[str] = mapped_column(String(80), default="local", nullable=False)
    local_path: Mapped[str] = mapped_column(Text, default="", nullable=False)
    oss_bucket: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    oss_key: Mapped[str] = mapped_column(Text, default="", nullable=False)
    oss_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    mime_type: Mapped[str] = mapped_column(String(150), default="application/octet-stream", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    read_only: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    mirror_status: Mapped[str] = mapped_column(String(30), default="not_requested", nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)


class ResearchProduct(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "research_products"
    __table_args__ = (
        UniqueConstraint("source", "source_sku", name="uq_research_source_sku"),
        UniqueConstraint("legacy_source", "legacy_id", name="uq_research_legacy"),
        Index("idx_research_status_updated", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(50), default="seerfar", nullable=False)
    source_sku: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(600), default="", nullable=False)
    category: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    ozon_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    image_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    image_urls_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    brand: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    selling_price_rub: Mapped[float | None] = mapped_column(Float, nullable=True)
    sales: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight_grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="pending_sourcing", nullable=False)
    description_category_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    type_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category_display: Mapped[str] = mapped_column(String(800), default="", nullable=False)
    source_file: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_row: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    sourcing_candidates: Mapped[list["SourcingCandidate"]] = relationship(
        back_populates="research_product", cascade="all, delete-orphan"
    )
    listings: Mapped[list["Listing"]] = relationship(back_populates="research_product")


class SourcingCandidate(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "sourcing_candidates"
    __table_args__ = (
        UniqueConstraint(
            "research_product_id", "provider", "external_product_id", "external_sku_id",
            name="uq_sourcing_candidate_external",
        ),
        Index("idx_sourcing_product_status", "research_product_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    research_product_id: Mapped[str] = mapped_column(
        ForeignKey("research_products.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    external_product_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    external_sku_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    title: Mapped[str] = mapped_column(String(600), default="", nullable=False)
    sku_name: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    image_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    unit_price_cny: Mapped[float | None] = mapped_column(Float, nullable=True)
    domestic_delivery_cny: Mapped[float | None] = mapped_column(Float, nullable=True)
    landed_cost_cny: Mapped[float | None] = mapped_column(Float, nullable=True)
    sales: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minimum_order_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="candidate", nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    research_product: Mapped[ResearchProduct] = relationship(back_populates="sourcing_candidates")


class CatalogProduct(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "catalog_products"
    __table_args__ = (
        UniqueConstraint("legacy_source", "legacy_id", name="uq_catalog_products_legacy"),
        Index("idx_catalog_status_updated", "status", "updated_at"),
        Index("idx_catalog_source_group", "source_type", "group_code"),
        Index("idx_catalog_product_sku", "sku"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    sku: Mapped[str] = mapped_column(String(200), nullable=False)
    group_code: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), default="self_selected", nullable=False)
    name: Mapped[str] = mapped_column(String(600), default="", nullable=False)
    category: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    source_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    supplier: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    supplier_product_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    purchase_price_cny: Mapped[float | None] = mapped_column(Float, nullable=True)
    domestic_delivery_cny: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(10), default="CNY", nullable=False)
    package_length_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    package_width_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    package_height_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    package_weight_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    description_category_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    type_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category_display: Mapped[str] = mapped_column(String(800), default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    production_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    source_snapshot_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="draft", nullable=False)
    tags_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    variants: Mapped[list["CatalogVariant"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    asset_links: Mapped[list["CatalogProductAsset"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    listings: Mapped[list["Listing"]] = relationship(back_populates="catalog_product")


class CatalogVariant(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "catalog_variants"
    __table_args__ = (
        UniqueConstraint("legacy_source", "legacy_id", name="uq_catalog_variants_legacy"),
        Index("idx_catalog_variants_product", "product_id"),
        Index("idx_catalog_variant_sku", "sku"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_products.id", ondelete="CASCADE"), nullable=False
    )
    sku: Mapped[str] = mapped_column(String(200), nullable=False)
    supplier_product_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    supplier_sku_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    specification: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    color: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    size: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    purchase_price_cny: Mapped[float | None] = mapped_column(Float, nullable=True)
    domestic_delivery_cny: Mapped[float | None] = mapped_column(Float, nullable=True)
    package_length_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    package_width_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    package_height_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight_grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    barcode: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    properties_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    product: Mapped[CatalogProduct] = relationship(back_populates="variants")
    asset_links: Mapped[list["CatalogProductAsset"]] = relationship(back_populates="variant")


class CatalogProductAsset(TimestampMixin, Base):
    __tablename__ = "catalog_product_assets"
    __table_args__ = (
        Index("idx_catalog_assets_product_current", "product_id", "is_current", "role", "position"),
        Index("idx_catalog_assets_variant", "variant_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_products.id", ondelete="CASCADE"), nullable=False
    )
    variant_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog_variants.id", ondelete="SET NULL"), nullable=True
    )
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(40), default="additional", nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(50), default="import", nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    product: Mapped[CatalogProduct] = relationship(back_populates="asset_links")
    variant: Mapped[CatalogVariant | None] = relationship(back_populates="asset_links")
    asset: Mapped[Asset] = relationship()


class CatalogDuplicateSuggestion(TimestampMixin, Base):
    __tablename__ = "catalog_duplicate_suggestions"
    __table_args__ = (
        UniqueConstraint("product_id", "candidate_product_id", name="uq_catalog_duplicate_pair"),
        Index("idx_catalog_duplicates_status", "status", "score"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("catalog_products.id", ondelete="CASCADE"), nullable=False)
    candidate_product_id: Mapped[str] = mapped_column(ForeignKey("catalog_products.id", ondelete="CASCADE"), nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    reasons_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)


class CatalogImportRun(TimestampMixin, Base):
    __tablename__ = "catalog_import_runs"
    __table_args__ = (Index("idx_catalog_imports_created", "kind", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    source_path: Mapped[str] = mapped_column(Text, default="", nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="queued", nullable=False)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    counts_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    errors_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    report_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)


class AmazonListingBatch(TimestampMixin, Base):
    __tablename__ = 'amazon_listing_batches'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    store_id: Mapped[str] = mapped_column(ForeignKey('stores.id', ondelete='RESTRICT'), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(32), default='', nullable=False)
    worker_id: Mapped[str] = mapped_column(String(100), default='', nullable=False)
    status: Mapped[str] = mapped_column(String(40), default='preparing', nullable=False, index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey('jobs.id', ondelete='SET NULL'), nullable=True)
    snapshot_json: Mapped[str] = mapped_column(Text, default='{}', nullable=False)
    draft_json: Mapped[str] = mapped_column(Text, default='{}', nullable=False)
    result_json: Mapped[str] = mapped_column(Text, default='{}', nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default='', nullable=False)


class AmazonListingItem(TimestampMixin, Base):
    __tablename__ = 'amazon_listing_items'
    __table_args__ = (UniqueConstraint('store_id', 'sku', name='uq_amazon_store_sku'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(ForeignKey('amazon_listing_batches.id', ondelete='RESTRICT'), nullable=False, index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey('stores.id', ondelete='RESTRICT'), nullable=False)
    product_id: Mapped[str | None] = mapped_column(ForeignKey('catalog_products.id', ondelete='RESTRICT'), nullable=True)
    variant_id: Mapped[str | None] = mapped_column(ForeignKey('catalog_variants.id', ondelete='RESTRICT'), nullable=True)
    sku: Mapped[str] = mapped_column(String(40), nullable=False)
    parent_sku: Mapped[str] = mapped_column(String(40), default='', nullable=False)
    status: Mapped[str] = mapped_column(String(40), default='draft', nullable=False, index=True)
    asin: Mapped[str] = mapped_column(String(20), default='', nullable=False)
    body_json: Mapped[str] = mapped_column(Text, default='{}', nullable=False)
    pricing_json: Mapped[str] = mapped_column(Text, default='{}', nullable=False)
    receipt_json: Mapped[str] = mapped_column(Text, default='{}', nullable=False)


class Publication(TimestampMixin, Base):
    __tablename__ = "publications"
    __table_args__ = (
        Index("idx_publications_batch", "batch_id", "created_at"),
        Index("idx_publications_product", "product_id", "created_at"),
        Index("idx_publications_status", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(String(32), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("catalog_products.id", ondelete="RESTRICT"), nullable=False)
    variant_id: Mapped[str | None] = mapped_column(ForeignKey("catalog_variants.id", ondelete="RESTRICT"), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="queued", nullable=False)
    requirements_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    image_policy_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    save_generated_to_catalog: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    submit_via_api: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    product_snapshot_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)

    product: Mapped[CatalogProduct] = relationship()
    variant: Mapped[CatalogVariant | None] = relationship()
    stores: Mapped[list["PublicationStore"]] = relationship(back_populates="publication", cascade="all, delete-orphan")


class PublicationStore(TimestampMixin, Base):
    __tablename__ = "publication_stores"
    __table_args__ = (
        UniqueConstraint("publication_id", "store_id", name="uq_publication_store"),
        UniqueConstraint("idempotency_key", name="uq_publication_store_idempotency"),
        Index("idx_publication_stores_status", "store_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id", ondelete="CASCADE"), nullable=False)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id", ondelete="RESTRICT"), nullable=False)
    offer_id: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(60), default="queued", nullable=False)
    content_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    attributes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    complex_attributes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    assets_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    pricing_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    package_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    settings_snapshot_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    external_task_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    external_product_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    stock_status: Mapped[str] = mapped_column(String(40), default="pending", nullable=False)
    promotion_status: Mapped[str] = mapped_column(String(40), default="pending", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)

    publication: Mapped[Publication] = relationship(back_populates="stores")
    store: Mapped[Store] = relationship()


class Listing(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("base_offer_id", name="uq_listings_base_offer"),
        UniqueConstraint("legacy_source", "legacy_id", name="uq_listings_legacy"),
        Index("idx_listings_status_updated", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    research_product_id: Mapped[str | None] = mapped_column(
        ForeignKey("research_products.id", ondelete="SET NULL"), nullable=True
    )
    catalog_product_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog_products.id", ondelete="SET NULL"), nullable=True
    )
    base_offer_id: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(600), default="", nullable=False)
    description_ru: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tags_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    attributes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    complex_attributes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    asset_ids_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    pricing_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    package_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    requirements_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    image_policy_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    save_generated_to_catalog: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    publication_mode: Mapped[str] = mapped_column(String(20), default="api", nullable=False)
    catalog_snapshot_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="draft", nullable=False)
    blockers_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)

    research_product: Mapped[ResearchProduct | None] = relationship(back_populates="listings")
    catalog_product: Mapped[CatalogProduct | None] = relationship(back_populates="listings")
    store_listings: Mapped[list["StoreListing"]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )


class StoreListing(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "store_listings"
    __table_args__ = (
        UniqueConstraint("listing_id", "store_id", name="uq_store_listing"),
        UniqueConstraint("store_id", "offer_id", name="uq_store_offer"),
        UniqueConstraint("idempotency_key", name="uq_store_listing_idempotency"),
        Index("idx_store_listings_status", "store_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), nullable=False)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    offer_id: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(60), default="draft", nullable=False)
    external_task_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    external_product_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    stock_status: Mapped[str] = mapped_column(String(40), default="pending", nullable=False)
    promotion_status: Mapped[str] = mapped_column(String(40), default="pending", nullable=False)
    content_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    attributes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    complex_attributes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    assets_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    pricing_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    settings_snapshot_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False, default=new_id)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)

    listing: Mapped[Listing] = relationship(back_populates="store_listings")
    store: Mapped[Store] = relationship(back_populates="store_listings")


class WbPublicationItem(TimestampMixin, Base):
    __tablename__ = "wb_publication_items"
    __table_args__ = (
        UniqueConstraint("store_id", "variant_id", name="uq_wb_publication_store_variant"),
        UniqueConstraint("store_id", "offer_id", name="uq_wb_publication_store_offer"),
        Index("idx_wb_publication_product_store", "product_id", "store_id", "status"),
        Index("idx_wb_publication_job", "job_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("catalog_products.id", ondelete="CASCADE"), nullable=False)
    variant_id: Mapped[str] = mapped_column(ForeignKey("catalog_variants.id", ondelete="CASCADE"), nullable=False)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    template_id: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    offer_id: Mapped[str] = mapped_column(String(200), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), default="excel", nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="queued", nullable=False)
    external_product_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_jobs_idempotency"),
        Index("idx_jobs_queue", "status", "priority", "created_at"),
        Index("idx_jobs_lease", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    type: Mapped[str] = mapped_column(String(80), nullable=False)
    created_by: Mapped[str] = mapped_column(String(32), default="", server_default="", nullable=False)
    target_worker_id: Mapped[str] = mapped_column(String(100), default="", server_default="", index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="queued", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    config_snapshot_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    progress: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    lease_owner: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    external_ref: Mapped[str] = mapped_column(Text, default="", nullable=False)
    error_code: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)

    steps: Mapped[list["JobStep"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    events: Mapped[list["JobEvent"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    artifacts: Mapped[list["JobArtifact"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobStep(TimestampMixin, Base):
    __tablename__ = "job_steps"
    __table_args__ = (
        UniqueConstraint("job_id", "key", name="uq_job_steps_key"),
        Index("idx_job_steps_job_order", "job_id", "position"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    progress: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    details_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    job: Mapped[Job] = relationship(back_populates="steps")


class JobEvent(Base):
    __tablename__ = "job_events"
    __table_args__ = (Index("idx_job_events_job_created", "job_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    level: Mapped[str] = mapped_column(String(20), default="info", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    job: Mapped[Job] = relationship(back_populates="events")


class JobArtifact(Base):
    __tablename__ = "job_artifacts"
    __table_args__ = (UniqueConstraint("job_id", "asset_id", name="uq_job_artifact"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(60), default="output", nullable=False)

    job: Mapped[Job] = relationship(back_populates="artifacts")


class WorkerNode(TimestampMixin, Base):
    __tablename__ = "worker_nodes"
    __table_args__ = (Index("idx_worker_nodes_last_seen", "last_seen_at"),)

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(32), default="", server_default="", index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="online", nullable=False)
    capabilities_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class SystemSetting(TimestampMixin, Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(150), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)


class AdSku(TimestampMixin, Base):
    __tablename__ = "ad_skus"
    __table_args__ = (
        UniqueConstraint("store_id", "sku_id", name="uq_ad_skus_store_sku"),
        Index("idx_ad_skus_store_category", "store_id", "category_path"),
        Index("idx_ad_skus_outcome", "store_id", "outcome_label"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    sku_id: Mapped[str] = mapped_column(String(200), nullable=False)
    product_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    name: Mapped[str] = mapped_column(String(600), default="", nullable=False)
    category_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    category_path: Mapped[str] = mapped_column(String(800), default="", nullable=False)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    live_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ad_start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    target_drr: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fulfillment_type: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    delivery_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    break_even_drr: Mapped[float | None] = mapped_column(Float, nullable=True)
    image_version: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    price_version: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    outcome_label: Mapped[str] = mapped_column(String(30), default="", nullable=False)
    weak_label: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    data_maturity: Mapped[str] = mapped_column(String(20), default="LEVEL_0", nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)


class AdDailyMetric(TimestampMixin, Base):
    __tablename__ = "ad_daily_metrics"
    __table_args__ = (
        UniqueConstraint("ad_sku_id", "metric_date", name="uq_ad_daily_sku_date"),
        Index("idx_ad_daily_date", "metric_date"),
        Index("idx_ad_daily_sku_date", "ad_sku_id", "metric_date"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ad_sku_id: Mapped[str] = mapped_column(ForeignKey("ad_skus.id", ondelete="CASCADE"), nullable=False)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    add_to_cart: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sold_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ad_spend: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    ad_sales: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    target_drr: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_cpc: Mapped[float | None] = mapped_column(Float, nullable=True)
    ctr: Mapped[float | None] = mapped_column(Float, nullable=True)
    atc_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    cvr: Mapped[float | None] = mapped_column(Float, nullable=True)
    cart_cvr: Mapped[float | None] = mapped_column(Float, nullable=True)
    cpa: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_drr: Mapped[float | None] = mapped_column(Float, nullable=True)
    break_even_cvr: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)


class AdCampaignDailyMetric(TimestampMixin, Base):
    __tablename__ = "ad_campaign_daily_metrics"
    __table_args__ = (
        UniqueConstraint("store_id", "campaign_id", "sku_id", "metric_date", name="uq_ad_campaign_sku_date"),
        Index("idx_ad_campaign_store_date", "store_id", "metric_date"),
        Index("idx_ad_campaign_store_campaign", "store_id", "campaign_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(200), nullable=False)
    campaign_name: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    sku_id: Mapped[str] = mapped_column(String(200), nullable=False)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    add_to_cart: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sold_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ad_spend: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    ad_sales: Mapped[float] = mapped_column(Float, default=0, nullable=False)


class AdCampaign(TimestampMixin, Base):
    __tablename__ = "ad_campaigns"
    __table_args__ = (UniqueConstraint("store_id", "campaign_id", name="uq_ad_campaign_store_external"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    state: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    payment_type: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    expense_strategy: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    budget_type: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    previous_budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    weekly_budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_change_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)


class AdCampaignSku(TimestampMixin, Base):
    __tablename__ = "ad_campaign_skus"
    __table_args__ = (UniqueConstraint("ad_campaign_id", "sku_id", name="uq_ad_campaign_sku"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ad_campaign_id: Mapped[str] = mapped_column(ForeignKey("ad_campaigns.id", ondelete="CASCADE"), nullable=False)
    sku_id: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_drr: Mapped[float | None] = mapped_column(Float, nullable=True)
    previous_target_drr: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_change_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)


class AdThresholdVersion(TimestampMixin, Base):
    __tablename__ = "ad_threshold_versions"
    __table_args__ = (
        Index("idx_ad_threshold_store_active", "store_id", "active"),
        Index("idx_ad_threshold_version", "version"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[str] = mapped_column(String(80), nullable=False)
    cohort_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    cohort_level: Mapped[str] = mapped_column(String(40), nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_sample_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    window_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    thresholds_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class AdSkuState(TimestampMixin, Base):
    __tablename__ = "ad_sku_states"
    __table_args__ = (
        UniqueConstraint("ad_sku_id", name="uq_ad_sku_states_sku"),
        Index("idx_ad_sku_states_state", "state", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ad_sku_id: Mapped[str] = mapped_column(ForeignKey("ad_skus.id", ondelete="CASCADE"), nullable=False)
    state: Mapped[str] = mapped_column(String(40), default="OBSERVATION", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    recommendation: Mapped[str] = mapped_column(String(50), default="OBSERVE", nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    cohort_key: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    cohort_level: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    cohort_sample_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    threshold_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("ad_threshold_versions.id", ondelete="SET NULL"), nullable=True
    )
    metrics_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    financial_check_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)


class AdSkuStateHistory(Base):
    __tablename__ = "ad_sku_state_history"
    __table_args__ = (Index("idx_ad_state_history_sku_created", "ad_sku_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ad_sku_id: Mapped[str] = mapped_column(ForeignKey("ad_skus.id", ondelete="CASCADE"), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    recommendation: Mapped[str] = mapped_column(String(50), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    cohort_key: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    threshold_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("ad_threshold_versions.id", ondelete="SET NULL"), nullable=True
    )
    metrics_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class AdAction(TimestampMixin, Base):
    __tablename__ = "ad_actions"
    __table_args__ = (Index("idx_ad_actions_sku_status", "ad_sku_id", "status"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ad_sku_id: Mapped[str] = mapped_column(ForeignKey("ad_skus.id", ondelete="CASCADE"), nullable=False)
    state_history_id: Mapped[str | None] = mapped_column(
        ForeignKey("ad_sku_state_history.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="proposed", nullable=False)
    previous_target_drr: Mapped[float | None] = mapped_column(Float, nullable=True)
    proposed_target_drr: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    before_metrics_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    after_metrics_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AdExecutionBatch(TimestampMixin, Base):
    __tablename__ = "ad_execution_batches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id", ondelete="RESTRICT"), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="preview", nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    preview_json: Mapped[str] = mapped_column(Text, nullable=False)
    results_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # A unique nullable store lease serializes writes across processes. Unknown
    # outcomes retain this lease until read-only reconciliation confirms them.
    active_store: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)


class AdExperiment(TimestampMixin, Base):
    __tablename__ = "ad_experiments"
    __table_args__ = (Index("idx_ad_experiments_sku_status", "ad_sku_id", "status"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ad_sku_id: Mapped[str] = mapped_column(ForeignKey("ad_skus.id", ondelete="CASCADE"), nullable=False)
    action_id: Mapped[str | None] = mapped_column(ForeignKey("ad_actions.id", ondelete="SET NULL"), nullable=True)
    experiment_code: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="planned", nullable=False)
    before_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    before_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    after_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    after_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    before_metrics_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    after_metrics_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    outcome: Mapped[str] = mapped_column(String(30), default="INCONCLUSIVE", nullable=False)


class AdBacktestRun(TimestampMixin, Base):
    __tablename__ = "ad_backtest_runs"
    __table_args__ = (Index("idx_ad_backtests_store_created", "store_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    threshold_version: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    false_kill_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    false_keep_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    potential_savings: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    summary_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)


# POD lives in its own domain package, but importing it here keeps Alembic and
# test-time Base.metadata discovery complete without returning to one huge model file.
from erp.domains.pod import models as _pod_models  # noqa: E402,F401
from erp import auth_models as _auth_models  # noqa: E402,F401
