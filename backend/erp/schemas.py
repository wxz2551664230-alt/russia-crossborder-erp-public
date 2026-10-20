from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator


Provider = Literal[
    "ozon", "wildberries", "alibaba", "daji", "elim", "cloud_newton",
    "hicustom", "s2b", "sds", "tms", "oss", "ai_chat", "ai_image",
    "product_library", "procurement",
]
Platform = Literal["ozon", "wildberries", "amazon"]
CatalogSource = Literal["pod", "pod_amazon", "follow", "self_selected"]
CatalogAssetRole = Literal["main", "additional", "artwork", "mockup", "sku"]


class ConnectionCreate(BaseModel):
    provider: Provider
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(default="", max_length=600)
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class ConnectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: str | None = Field(default=None, max_length=600)
    config: dict[str, Any] | None = None
    secrets: dict[str, str] | None = None
    enabled: bool | None = None


class StoreCreate(BaseModel):
    platform: Platform
    name: str = Field(min_length=1, max_length=120)
    external_account_id: str | None = Field(default=None, max_length=200)
    offer_prefix: str = Field(default="", max_length=60)
    currency: str = Field(default="RUB", min_length=3, max_length=10)
    enabled: bool = True
    connection_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_credentials(self) -> "StoreCreate":
        if self.platform == "ozon" and not (self.external_account_id or self.secrets.get("client_id")):
            raise ValueError("OZON 店铺需要 Client ID")
        return self


class StoreUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    external_account_id: str | None = Field(default=None, max_length=200)
    offer_prefix: str | None = Field(default=None, max_length=60)
    currency: str | None = Field(default=None, min_length=3, max_length=10)
    enabled: bool | None = None
    connection_id: str | None = None
    config: dict[str, Any] | None = None
    secrets: dict[str, str] | None = None


class AssetRegister(BaseModel):
    path: str = Field(min_length=1, max_length=4000)
    kind: str = Field(default="file", max_length=50)
    source: str = Field(default="legacy", max_length=80)
    read_only: bool = True
    calculate_hash: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class CategoryConfirmation(BaseModel):
    description_category_id: int = Field(gt=0)
    type_id: int = Field(gt=0)
    display: str = Field(min_length=1, max_length=800)


class SourcingCandidateCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=50)
    external_product_id: str = Field(default="", max_length=200)
    external_sku_id: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=600)
    sku_name: str = Field(default="", max_length=500)
    image_url: str = Field(default="", max_length=4000)
    source_url: str = Field(default="", max_length=4000)
    unit_price_cny: float | None = Field(default=None, ge=0)
    domestic_delivery_cny: float | None = Field(default=None, ge=0)
    sales: int | None = Field(default=None, ge=0)
    minimum_order_quantity: float | None = Field(default=None, ge=0)
    rank: int | None = Field(default=None, ge=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    raw: dict[str, Any] = Field(default_factory=dict)


class SourcingRunRequest(BaseModel):
    provider_connection_id: str | None = None
    candidate_count: int = Field(default=10, ge=1, le=50)


class ListingCreate(BaseModel):
    research_product_id: str | None = None
    catalog_product_id: str | None = None
    base_offer_id: str = Field(min_length=1, max_length=200)
    store_ids: list[str] = Field(min_length=1, max_length=50)
    catalog_primary_store_id: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "ListingCreate":
        if bool(self.research_product_id) == bool(self.catalog_product_id):
            raise ValueError("必须且只能选择一个商品来源")
        if self.catalog_primary_store_id and self.catalog_primary_store_id not in self.store_ids:
            raise ValueError("产品库主店铺必须属于本次目标店铺")
        if not self.catalog_primary_store_id and self.store_ids:
            self.catalog_primary_store_id = self.store_ids[0]
        return self


class ListingUpdate(BaseModel):
    store_id: str | None = None
    title: str | None = Field(default=None, max_length=600)
    description_ru: str | None = Field(default=None, max_length=6000)
    tags: list[str] | None = Field(default=None, max_length=30)
    attributes: list[dict[str, Any]] | None = None
    complex_attributes: list[dict[str, Any]] | None = None
    asset_ids: list[str] | None = None
    pricing: dict[str, Any] | None = None
    package: dict[str, Any] | None = None


class ListingRunRequest(BaseModel):
    store_ids: list[str] | None = Field(default=None, max_length=50)
    start_at: Literal[
        "validate", "ai_prepare", "submit", "product_sync", "stock", "promotion",
    ] = "validate"
    dry_run: bool = True
    warehouse_id: int | None = Field(default=None, gt=0)
    stock: int | None = Field(default=None, gt=0)
    promotion_action_id: int | None = Field(default=None, gt=0)
    ozon_product_id: int | None = Field(default=None, gt=0)
    action_price: float | None = Field(default=None, gt=0)


class ImportRecoveryRequest(BaseModel):
    external_task_id: int = Field(gt=0)


class CatalogVariantInput(BaseModel):
    id: str | None = None
    sku: str = Field(min_length=1, max_length=200)
    supplier_product_id: str = Field(default="", max_length=200)
    supplier_sku_id: str = Field(default="", max_length=200)
    specification: str = Field(default="", max_length=500)
    color: str = Field(default="", max_length=200)
    size: str = Field(default="", max_length=200)
    purchase_price_cny: float | None = Field(default=None, ge=0)
    domestic_delivery_cny: float | None = Field(default=None, ge=0)
    package_length_mm: float | None = Field(default=None, gt=0)
    package_width_mm: float | None = Field(default=None, gt=0)
    package_height_mm: float | None = Field(default=None, gt=0)
    weight_grams: float | None = Field(default=None, gt=0)
    barcode: str = Field(default="", max_length=200)
    active: bool = True
    properties: dict[str, Any] = Field(default_factory=dict)


class CatalogProductCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=200)
    group_code: str = Field(default="", max_length=200)
    source_type: CatalogSource
    name: str = Field(min_length=1, max_length=600)
    category: str = Field(default="", max_length=300)
    source_url: str = Field(default="", max_length=4000)
    supplier: str = Field(default="", max_length=120)
    supplier_product_id: str = Field(default="", max_length=200)
    purchase_price_cny: float | None = Field(default=None, ge=0)
    domestic_delivery_cny: float | None = Field(default=None, ge=0)
    currency: str = Field(default="CNY", min_length=3, max_length=10)
    package_length_mm: float | None = Field(default=None, gt=0)
    package_width_mm: float | None = Field(default=None, gt=0)
    package_height_mm: float | None = Field(default=None, gt=0)
    package_weight_g: float | None = Field(default=None, gt=0)
    description_category_id: int | None = Field(default=None, gt=0)
    type_id: int | None = Field(default=None, gt=0)
    category_display: str = Field(default="", max_length=800)
    notes: str = Field(default="", max_length=6000)
    status: str = Field(default="ready", max_length=40)
    tags: list[str] = Field(default_factory=list, max_length=100)
    production: dict[str, Any] = Field(default_factory=dict)
    source_snapshot: dict[str, Any] = Field(default_factory=dict)
    variants: list[CatalogVariantInput] = Field(default_factory=list, max_length=1000)


class CatalogProductUpdate(BaseModel):
    group_code: str | None = Field(default=None, max_length=200)
    name: str | None = Field(default=None, min_length=1, max_length=600)
    category: str | None = Field(default=None, max_length=300)
    source_url: str | None = Field(default=None, max_length=4000)
    supplier: str | None = Field(default=None, max_length=120)
    supplier_product_id: str | None = Field(default=None, max_length=200)
    purchase_price_cny: float | None = Field(default=None, ge=0)
    domestic_delivery_cny: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=10)
    package_length_mm: float | None = Field(default=None, gt=0)
    package_width_mm: float | None = Field(default=None, gt=0)
    package_height_mm: float | None = Field(default=None, gt=0)
    package_weight_g: float | None = Field(default=None, gt=0)
    description_category_id: int | None = Field(default=None, gt=0)
    type_id: int | None = Field(default=None, gt=0)
    category_display: str | None = Field(default=None, max_length=800)
    notes: str | None = Field(default=None, max_length=6000)
    status: str | None = Field(default=None, max_length=40)
    tags: list[str] | None = Field(default=None, max_length=100)
    production: dict[str, Any] | None = None
    source_snapshot: dict[str, Any] | None = None


class CatalogAssetLinkCreate(BaseModel):
    asset_id: str
    variant_id: str | None = None
    role: CatalogAssetRole = "additional"
    position: int = Field(default=0, ge=0)
    source_kind: str = Field(default="manual", max_length=50)
    set_current: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class CatalogAssetUrlCreate(BaseModel):
    oss_url: str = Field(min_length=8, max_length=4000)
    name: str = Field(default="", max_length=500)
    role: CatalogAssetRole = "additional"
    position: int = Field(default=0, ge=0)
    variant_id: str | None = None
    set_current: bool = True

    @model_validator(mode="after")
    def validate_oss_url(self) -> "CatalogAssetUrlCreate":
        if not self.oss_url.startswith("https://"):
            raise ValueError("产品图片必须使用可公开访问的 HTTPS 链接")
        return self


class CatalogAssetPrepareCreate(BaseModel):
    source_url: HttpUrl
    name: str = Field(default="", max_length=500)
    role: CatalogAssetRole = "additional"
    position: int = Field(default=0, ge=0)
    variant_id: str | None = None
    set_current: bool = True
    max_dimension: int = Field(default=2000, ge=320, le=4096)
    quality: int = Field(default=85, ge=60, le=95)
    output_format: Literal["JPEG", "WEBP"] = "JPEG"


class CatalogDuplicateUpdate(BaseModel):
    status: Literal["pending", "ignored", "merged"]


class PodImportRequest(BaseModel):
    dry_run: bool = True
    source_database: str | None = Field(default=None, max_length=4000)


class SelfListingPipelineCreate(BaseModel):
    selected_steps: list[Literal["generate_images", "generate_copywriting", "watermark", "upload_original", "upload_watermarked"]] = Field(
        default_factory=lambda: ["generate_images", "generate_copywriting", "watermark", "upload_original", "upload_watermarked"],
        min_length=1,
        max_length=5,
    )
    product_root: str = Field(min_length=1, max_length=4000)
    output_dir: str = Field(default="", max_length=4000)
    watermark_root: str = Field(default="", max_length=4000)
    watermark_filename: str = Field(default="水印.png", min_length=1, max_length=300)
    source_subfolder: str = Field(default="_generated", min_length=1, max_length=100)
    watermark_position: Literal["top-left", "top-right", "bottom-left", "bottom-right", "center"] = "bottom-right"
    watermark_scale: float = Field(default=0.2, gt=0, le=1)
    watermark_opacity: float = Field(default=0.7, gt=0, le=1)
    original_oss_prefix: str = Field(default="OZON/original", max_length=500)
    watermarked_oss_prefix: str = Field(default="OZON/watermarked", max_length=500)
    product_hint: str = Field(default="", max_length=500)
    category_hint: str = Field(default="", max_length=500)
    page_keywords: str = Field(default="", max_length=6000)
    image_plan_count: int = Field(default=8, ge=1, le=12)
    repair_only_missing: bool = False
    ai_chat_connection_id: str | None = None
    ai_image_connection_id: str | None = None
    oss_connection_id: str | None = None
    oss_dry_run: bool = False


class LocalDirectoryPickerRequest(BaseModel):
    title: str = Field(default="选择文件夹", max_length=100)
    initial_dir: str = Field(default="", max_length=4000)


ImagePolicy = Literal["reuse", "regenerate_main", "regenerate_additional", "regenerate_all", "custom"]


class OzonMergeSettings(BaseModel):
    mode: Literal["single", "custom_group"] = "single"
    model_name: str = Field(default="", max_length=500)
    variant_attribute_ids: list[int] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_custom_group(self) -> "OzonMergeSettings":
        if self.mode == "custom_group":
            if not self.model_name.strip():
                raise ValueError("自定义合并组必须填写型号名称")
            if not self.variant_attribute_ids:
                raise ValueError("自定义合并组必须选择至少一个变体特征")
        return self


class PublicationCreate(BaseModel):
    variant_ids: list[str] | None = Field(default=None, min_length=1, max_length=5000)
    generation_provider: Literal['model', 'codex'] = 'model'
    product_ids: list[str] = Field(min_length=1, max_length=100)
    store_ids: list[str] = Field(min_length=1, max_length=50)
    requirements: dict[str, Any] = Field(default_factory=dict)
    image_policy: ImagePolicy = "reuse"
    custom_regenerate_roles: list[CatalogAssetRole] = Field(default_factory=list)
    save_generated_to_catalog: bool = False
    submit_via_api: bool = True
    dry_run: bool = False
    stock: int | None = Field(default=None, gt=0)
    warehouse_id: int | None = Field(default=None, gt=0)
    promotion_action_id: int | None = Field(default=None, gt=0)
    action_price: float | None = Field(default=None, gt=0)
    ozon_merge: OzonMergeSettings = Field(default_factory=OzonMergeSettings)

    @model_validator(mode="after")
    def validate_custom_image_policy(self) -> "PublicationCreate":
        if self.image_policy == "custom" and not self.custom_regenerate_roles:
            raise ValueError("自定义重做图片时至少选择一种图片")
        return self


class PublicationPreviewRequest(BaseModel):
    variant_ids: list[str] | None = Field(default=None, min_length=1, max_length=5000)
    product_ids: list[str] = Field(min_length=1, max_length=100)
    store_ids: list[str] = Field(min_length=1, max_length=50)
    requirements: dict[str, Any] = Field(default_factory=dict)


class PublicationStockRequest(BaseModel):
    warehouse_id: int = Field(gt=0)
    stock: int = Field(gt=0)


class PublicationPromotionRequest(BaseModel):
    action_id: int = Field(gt=0)
    action_price: float = Field(gt=0)
    stock: int | None = Field(default=None, gt=0, description="留空表示不设置促销数量上限")


class JobCreate(BaseModel):
    type: Literal["asset_index", "legacy_migration", "worker_probe", "image_prepare"]
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=200)
    priority: int = Field(default=100, ge=0, le=1000)


class WorkerRegistration(BaseModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    name: str = Field(default="", max_length=200)
    capabilities: list[Literal["worker_probe", "image_prepare"]] = Field(default_factory=lambda: ["worker_probe"])
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerHeartbeat(BaseModel):
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerClaim(BaseModel):
    capabilities: list[Literal["worker_probe", "image_prepare"]] = Field(default_factory=lambda: ["worker_probe"])


class WorkerJobResult(BaseModel):
    result: dict[str, Any] = Field(default_factory=dict)


class WorkerJobFailure(BaseModel):
    error_code: str = Field(default="worker_error", max_length=100)
    error_message: str = Field(min_length=1, max_length=4000)


class SettingUpdate(BaseModel):
    value: Any


class AdActionCreate(BaseModel):
    state_id: str
    proposed_target_drr: float | None = Field(default=None, ge=0, le=1)


class AdBatchExecutionRequest(BaseModel):
    action: Literal["STOP_ADS_CANDIDATE", "SCALE_CANDIDATES"]
    confirmed: bool = False
    approval_id: str = Field(default="", max_length=32)
    state_ids: list[str] = Field(default_factory=list, min_length=1, max_length=500)


class AdOutcomeUpdate(BaseModel):
    outcome_label: Literal["", "SUCCESS", "WEAK", "FAILED", "NO_TRAFFIC", "STOPPED_EXTERNALLY"]
