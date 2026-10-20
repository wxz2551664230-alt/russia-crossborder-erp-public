export interface ApiErrorShape {
  code: string;
  message: string;
  details?: unknown;
  trace_id?: string;
}

export interface Connection {
  id: string;
  provider: string;
  name: string;
  base_url: string;
  config: Record<string, unknown>;
  secret_status: Record<string, { configured: boolean; masked: string }>;
  enabled: boolean;
  status: string;
  last_error: string;
  last_tested_at?: string;
}

export interface Store {
  id: string;
  platform: "ozon" | "wildberries" | "amazon";
  name: string;
  external_account_id: string;
  offer_prefix: string;
  currency: string;
  enabled: boolean;
  connection_id?: string;
  config: Record<string, unknown>;
  secret_status: Record<string, { configured: boolean; masked: string }>;
}

export interface ResearchProduct {
  id: string;
  source_sku: string;
  title: string;
  category: string;
  ozon_url: string;
  image_url: string;
  image_urls: string[];
  brand: string;
  selling_price_rub?: number;
  sales?: number;
  rating?: number;
  weight_grams?: number;
  status: string;
  description_category_id?: number;
  type_id?: number;
  category_display: string;
  updated_at: string;
  sourcing_candidates?: SourcingCandidate[];
}

export interface SourcingCandidate {
  id: string;
  research_product_id: string;
  provider: string;
  external_product_id: string;
  external_sku_id: string;
  title: string;
  sku_name: string;
  image_url: string;
  source_url: string;
  unit_price_cny?: number;
  domestic_delivery_cny?: number;
  landed_cost_cny?: number;
  sales?: number;
  minimum_order_quantity?: number;
  rank?: number;
  confidence?: number;
  color?: string;
  size?: string;
  remark?: string;
  status: string;
  selected: boolean;
}

export interface StoreListing {
  id: string;
  store_id: string;
  store_name: string;
  store_enabled: boolean;
  platform: string;
  offer_id: string;
  status: string;
  external_task_id: string;
  external_product_id: string;
  stock_status: string;
  promotion_status: string;
  last_error: string;
  pricing: Record<string, unknown>;
  content: Record<string, unknown>;
  assets: Record<string, unknown> & { primary_image?: string; images?: string[]; ai_preparation?: Record<string, unknown> };
  attributes: Record<string, unknown>[];
  complex_attributes: Record<string, unknown>[];
}

export interface Listing {
  id: string;
  research_product_id?: string;
  catalog_product_id?: string;
  base_offer_id: string;
  title: string;
  description_ru: string;
  tags: string[];
  attributes: Record<string, unknown>[];
  complex_attributes: Record<string, unknown>[];
  asset_ids: string[];
  pricing: Record<string, unknown>;
  package: Record<string, unknown>;
  status: string;
  blockers: string[];
  stores: StoreListing[];
  updated_at: string;
  research_product?: ResearchProduct;
}

export interface JobStep {
  id: string;
  key: string;
  name: string;
  position: number;
  status: string;
  progress: number;
  attempts: number;
  error_message: string;
  details: Record<string, unknown>;
}

export interface Job {
  wb_price_recovery?: {state?: string; via?: string} | null;
  business_module?: string;
  lease_expires_at?: string | null;
    id: string;
    created_by?: string;
    target_worker_id?: string;
  type: string;
  status: string;
  priority: number;
  progress: number;
  attempts: number;
  max_attempts: number;
  error_code: string;
  error_message: string;
  created_at: string;
  updated_at: string;
  payload?: Record<string, unknown>;
  steps: JobStep[];
  events?: Array<{
    id: number;
    level: string;
    message: string;
    created_at: string;
  }>;
}

export interface CatalogVariant {
  id: string;
  sku: string;
  supplier_product_id: string;
  supplier_sku_id: string;
  specification: string;
  color: string;
  size: string;
  purchase_price_cny?: number;
  domestic_delivery_cny?: number;
  package_length_mm?: number;
  package_width_mm?: number;
  package_height_mm?: number;
  weight_grams?: number;
  barcode: string;
  active: boolean;
  properties: Record<string, unknown>;
}

export interface CatalogAssetLink {
  id: string;
  variant_id?: string;
  role: string;
  position: number;
  is_current: boolean;
  source_kind: string;
  asset: {
    id: string;
    name: string;
    local_path: string;
    local_exists: boolean;
    oss_bucket: string;
    oss_key: string;
    oss_url: string;
    sha256: string;
    kind: string;
    mirror_status?: string;
  };
}

export interface CatalogProduct {
  id: string;
  sku: string;
  group_code: string;
  source_type: "pod" | "pod_amazon" | "follow" | "self_selected";
  name: string;
  category: string;
  source_url: string;
  supplier: string;
  supplier_product_id: string;
  purchase_price_cny?: number;
  domestic_delivery_cny?: number;
  currency: string;
  package_length_mm?: number;
  package_width_mm?: number;
  package_height_mm?: number;
  package_weight_g?: number;
  description_category_id?: number;
  type_id?: number;
  category_display: string;
  notes: string;
  status: string;
  tags: string[];
  variant_count: number;
  variant_skus?: string[];
  current_asset_count: number;
  thumbnail?: CatalogAssetLink;
  updated_at: string;
  variants?: CatalogVariant[];
  assets?: CatalogAssetLink[];
  production?: Record<string, unknown>;
  source_snapshot?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
}

export interface CatalogImportRun {
  id: string;
  kind: string;
  source_path: string;
  dry_run: boolean;
  status: string;
  job_id?: string;
  counts: Record<string, number>;
  errors: Array<Record<string, unknown>>;
  report: Record<string, unknown>;
  created_at: string;
}

export interface PublicationStore {
  id: string;
  store_id: string;
  store_name: string;
  offer_id: string;
  status: string;
  content: Record<string, unknown>;
  assets: Record<string, unknown> & { primary_image?: string; images?: string[]; ai_preparation?: Record<string, unknown> };
  pricing: Record<string, unknown>;
  external_task_id: string;
  external_product_id: string;
  stock_status: string;
  promotion_status: string;
  last_error: string;
}

export interface Publication {
  id: string;
  batch_id: string;
  product_id: string;
  variant_id?: string;
  product_name: string;
  sku: string;
  status: string;
  job_id?: string;
  requirements: Record<string, unknown>;
  image_policy: Record<string, unknown>;
  save_generated_to_catalog: boolean;
  submit_via_api: boolean;
  stores: PublicationStore[];
  created_at: string;
}
