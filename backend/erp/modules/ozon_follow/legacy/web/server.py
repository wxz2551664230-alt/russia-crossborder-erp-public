from __future__ import annotations

import copy
import json
import mimetypes
import re
import secrets
import threading
import traceback
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from erp.modules.ozon_follow.legacy.config import AppConfig, ConfigStore, SECRET_FIELDS
from erp.modules.ozon_follow.legacy.paths import DATA_DIR, ROOT_DIR, ensure_runtime_directories
from erp.modules.ozon_follow.legacy.services.cloud_newton_client import (
    CloudNewtonClient,
    NewtonChunk,
    NewtonTaskResult,
    NewtonTaskSnapshot,
    build_ozon_sourcing_message,
    build_selected_data,
)
from erp.modules.ozon_follow.legacy.services.ai_listing import (
    approve_pending_main_image,
    apply_content_attributes,
    enforce_system_attributes,
    normalize_existing_draft,
    prepare_main_image_from_checkpoint,
    normalize_hashtags,
    prepare_listing_with_ai,
    pending_main_image_path,
    prepare_sourcing_image,
    prepare_sourcing_image_file,
    repair_ozon_rejected_attributes,
    refresh_additional_images,
    regenerate_pending_main_image,
    store_creative_profile,
    upload_captured_gallery_images,
)
from erp.modules.ozon_follow.legacy.services.alibaba_client import AlibabaNewtonClient
from erp.modules.ozon_follow.legacy.services.daji_client import DajiApiLimitError, DajiClient, normalize_daji_candidates
from erp.modules.ozon_follow.legacy.services.elim_client import ElimApiLimitError, ElimClient, normalize_elim_candidates
from erp.modules.ozon_follow.legacy.services.ozon_client import OzonApiError, OzonSellerClient
from erp.modules.ozon_follow.legacy.services.ozon_gallery import additional_gallery_images, save_extension_gallery_capture
from erp.modules.ozon_follow.legacy.services.ozon_listing import (
    OzonListingDraft,
    OzonListingWorkflow,
    flatten_ozon_categories,
    prepare_listing_draft,
    required_attribute_skeleton,
    validate_listing_draft,
)
from erp.modules.ozon_follow.legacy.services.pricing import (
    OZON_RFBS_COMMISSION_TARIFFS,
    calculate_actual_sale_for_target_cost_margin,
)
from erp.modules.ozon_follow.legacy.services.product_manager import ProductManagerApi, build_product_manager_payload
from erp.modules.ozon_follow.legacy.services.seerfar_importer import import_seerfar_workbook
from erp.modules.ozon_follow.legacy.services.sourcing_selector import (
    ExactMatchDecision,
    MatchVerdict,
    SourcingCandidate,
    build_manual_review_candidates,
    build_exact_match_verification_message,
    extract_newton_candidate_rows,
    parse_exact_match_decision_chunks,
    select_best_source,
)
from erp.modules.ozon_follow.legacy.storage import ProductRepository


STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_REQUEST_BYTES = 64 * 1024 * 1024


@dataclass(slots=True)
class BackgroundJob:
    id: str
    kind: str
    source_sku: str = ""
    status: str = "RUNNING"
    message: str = ""
    result: Any = None
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, BackgroundJob] = {}
        self._lock = threading.Lock()

    def start(
        self,
        kind: str,
        work: Callable[[BackgroundJob], Any],
        *,
        source_sku: str = "",
        on_reserved: Callable[[], None] | None = None,
    ) -> BackgroundJob:
        with self._lock:
            if source_sku:
                running = next((
                    current for current in self._jobs.values()
                    if current.source_sku == source_sku and current.status == "RUNNING"
                ), None)
                if running is not None:
                    raise RuntimeError(
                        f"商品 {source_sku} 正在执行“{running.kind}”，请等待当前任务完成"
                    )
            job = BackgroundJob(
                id=secrets.token_urlsafe(12), kind=kind, source_sku=source_sku
            )
            if on_reserved:
                on_reserved()
            self._jobs[job.id] = job

        def runner() -> None:
            try:
                job.result = work(job)
                job.status = "DONE"
            except Exception as exc:
                job.status = "ERROR"
                job.error = str(exc)
                traceback.print_exc()

        threading.Thread(target=runner, daemon=True, name=f"web-job-{kind}").start()
        return job

    def get(self, job_id: str) -> BackgroundJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[BackgroundJob]:
        with self._lock:
            return list(self._jobs.values())


class WebApplication:
    def __init__(self) -> None:
        self.repository = ProductRepository()
        self.config_store = ConfigStore()
        self.jobs = JobRegistry()
        # Daji accounts have a daily call quota. Serial sourcing prevents a
        # large import from exhausting it in a short burst.
        self.sourcing_slots = threading.Semaphore(1)
        self.daji_blocked_error = ""
        self.daji_blocked_date = ""
        self.elim_blocked_error = ""
        self.elim_blocked_date = ""
        self.sourcing_recovery_started = False
        self.workflow_recovery_started = False
        self.csrf_token = secrets.token_urlsafe(32)
        self.ozon_categories_cache: list[dict[str, Any]] = []

    def start_sourcing_recovery(self) -> int:
        # Automatic image sourcing is intentionally paused. Keep the stored
        # queue and provider integrations intact so the feature can be enabled
        # again later without losing configuration or history.
        self.sourcing_recovery_started = True
        return 0

    def start_workflow_recovery(self) -> int:
        if self.workflow_recovery_started:
            return 0
        self.workflow_recovery_started = True
        worker = object.__new__(RequestHandler)
        worker.server = SimpleNamespace(app=self)
        recovered = 0
        config = self.config_store.load()
        for payload in self.repository.list_listing_drafts():
            draft = OzonListingDraft.from_dict(payload)
            sku = str(draft.source_sku or "").strip()
            if not sku:
                continue
            if draft.workflow_state == "RUNNING":
                draft.workflow_state = "ERROR"
                draft.workflow_error = "程序上次关闭时任务中断；已保留全部草稿数据，请重试当前步骤"
                draft.workflow_message = draft.workflow_error
                draft.workflow_updated_at = datetime.now().isoformat(timespec="seconds")
                self.repository.save_listing_draft(sku, draft.to_dict())
            migrated = _migrate_store_publications(draft, config)
            pending_store_import = any(
                isinstance(record.get("draft"), dict)
                and record["draft"].get("task_id")
                and record["draft"].get("status") in {
                    "IMPORT_SUBMITTED", "IMPORT_PENDING", "IMPORT_UNCERTAIN",
                }
                for record in draft.store_publications.values()
                if isinstance(record, dict)
            )
            if migrated:
                self.repository.save_listing_draft(sku, draft.to_dict())
            recovery_action = "submit" if pending_store_import else "refresh"
            legacy_pending = (
                not draft.store_publications
                and draft.task_id
                and draft.status in {"IMPORT_SUBMITTED", "IMPORT_PENDING"}
            )
            if pending_store_import or legacy_pending:
                try:
                    self.jobs.start(
                        recovery_action,
                        lambda state, source_sku=sku, action=recovery_action: worker._run_listing_job_with_state(
                            state, source_sku, action
                        ),
                        source_sku=sku,
                    )
                except RuntimeError:
                    continue
                recovered += 1
        return recovered

    def ozon_client(self) -> OzonSellerClient:
        config = self.config_store.load()
        return OzonSellerClient(
            config.ozon_client_id,
            config.ozon_api_key,
            config.ozon_api_base,
            repository=self.repository,
        )

    def secondary_ozon_client(self) -> OzonSellerClient:
        config = self.config_store.load()
        return OzonSellerClient(
            config.ozon_secondary_client_id,
            config.ozon_secondary_api_key,
            config.ozon_secondary_api_base,
            repository=self.repository,
        )

    def newton_client(self) -> CloudNewtonClient:
        config = self.config_store.load()
        return CloudNewtonClient(
            config.alibaba_app_key,
            config.alibaba_app_secret,
            config.alibaba_access_token,
        )

    def daji_client(self) -> DajiClient:
        config = self.config_store.load()
        return DajiClient(
            config.daji_app_key,
            config.daji_app_secret,
            config.daji_api_base,
        )

    def elim_client(self) -> ElimClient:
        config = self.config_store.load()
        return ElimClient(config.elim_api_key, config.elim_api_base)

    def alibaba_client(self) -> AlibabaNewtonClient:
        config = self.config_store.load()
        return AlibabaNewtonClient(
            config.alibaba_app_key,
            config.alibaba_app_secret,
            config.alibaba_access_token,
        )

    def product_manager_client(self) -> ProductManagerApi:
        config = self.config_store.load()
        return ProductManagerApi(
            config.product_manager_api_url,
            config.product_manager_api_token,
        )


def _chunk_dict(chunk: NewtonChunk) -> dict[str, Any]:
    return {
        "type": chunk.type,
        "content": chunk.content,
        "timestamp": chunk.timestamp,
    }


def _extract_alibaba_variants(payload: Any) -> list[dict[str, Any]]:
    """Normalize nested SKU lists returned by different product-info response versions."""
    found: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and (item.get("skuId") or item.get("sku_id")):
                    sku_id = str(item.get("skuId") or item.get("sku_id") or "").strip()
                    attributes = item.get("attributes") or item.get("specAttrs") or item.get("skuAttributes") or []
                    parts: list[str] = []
                    if isinstance(attributes, list):
                        for attribute in attributes:
                            if isinstance(attribute, dict):
                                name = str(attribute.get("attributeName") or attribute.get("name") or attribute.get("key") or "").strip()
                                text = str(attribute.get("attributeValue") or attribute.get("value") or attribute.get("valueName") or "").strip()
                                if text:
                                    parts.append(f"{name}:{text}" if name else text)
                    sku_name = str(item.get("skuName") or item.get("specName") or "").strip() or ";".join(parts)
                    price = item.get("price") or item.get("retailPrice") or item.get("discountPrice") or ""
                    found.append({"sku_id": sku_id, "sku_name": sku_name, "price": str(price or "")})
                walk(item)
        elif isinstance(value, dict):
            for nested in value.values():
                walk(nested)

    walk(payload)
    return list({item["sku_id"]: item for item in found if item["sku_id"]}.values())


def _normalize_1688_source(source_url: Any, item_id: Any) -> tuple[str, str]:
    """Return a canonical 1688 offer URL and item id for manual variant edits."""
    url = str(source_url or "").strip()
    normalized_item_id = str(item_id or "").strip()
    if url:
        matched = re.search(r"/offer/(\d+)(?:\.html)?", url)
        if not matched:
            raise ValueError("产品链接必须是包含商品ID的1688商品链接")
        normalized_item_id = matched.group(1)
    if not re.fullmatch(r"\d{6,}", normalized_item_id):
        raise ValueError("请填写正确的1688产品链接")
    return f"https://detail.1688.com/offer/{normalized_item_id}.html", normalized_item_id


def _backfill_manual_review(repository: ProductRepository, sourcing: dict[str, Any] | None) -> dict[str, Any] | None:
    """Make completed tasks created before manual review support actionable."""
    if not sourcing or sourcing.get("selected"):
        return sourcing
    if sourcing.get("provider") == "elim":
        rows = extract_newton_candidate_rows(sourcing.get("tables", []))
        repaired = False
        for row in rows:
            if str(row.get("imageUrl") or "").strip():
                continue
            elim_data = row.get("elimData") if isinstance(row.get("elimData"), dict) else {}
            search = elim_data.get("search") if isinstance(elim_data.get("search"), dict) else {}
            sku = elim_data.get("sku") if isinstance(elim_data.get("sku"), dict) else {}
            image = (
                sku.get("img_url") or sku.get("image_url") or sku.get("image")
                or search.get("img_url") or search.get("image_url") or search.get("white_image")
            )
            if image:
                row["imageUrl"] = str(image).strip()
                repaired = True
        if repaired:
            sourcing["manual_candidates"] = build_manual_review_candidates(rows)
            repository.save_sourcing_task(sourcing)
    if sourcing.get("manual_review_rule_version") == 5:
        return sourcing
    if sourcing.get("status") != "END":
        return sourcing
    rows = extract_newton_candidate_rows(sourcing.get("tables", []))
    if not rows:
        # Some historical tasks did not persist the Newton result table. Keep
        # their existing candidates untouched because the original ranking can
        # no longer be reconstructed safely.
        sourcing["manual_review_rule_version"] = 5
        sourcing.setdefault("manual_review_error", "")
        repository.save_sourcing_task(sourcing)
        return sourcing
    sourcing["manual_candidates"] = build_manual_review_candidates(rows)
    sourcing["manual_review_status"] = "PENDING" if sourcing["manual_candidates"] else ""
    sourcing["manual_review_rule_version"] = 5
    sourcing.setdefault("manual_review_error", "")
    repository.save_sourcing_task(sourcing)
    return sourcing


def _draft_for_product(repository: ProductRepository, sku: str) -> OzonListingDraft:
    product = repository.get_product_by_sku(sku)
    if not product:
        raise ValueError(f"找不到商品 SKU {sku}")
    saved = repository.get_listing_draft(sku)
    draft = OzonListingDraft.from_dict(saved) if saved else prepare_listing_draft(
        product, category_match=None, offer_id=repository.allocate_offer_id()
    )
    changed = False
    if not draft.task_id and re.fullmatch(
        r"AUTO-" + re.escape(str(sku)), draft.offer_id, flags=re.IGNORECASE
    ):
        draft.offer_id = repository.allocate_offer_id()
        changed = True
    changed = normalize_existing_draft(draft, product) or changed
    sourcing = repository.get_latest_sourcing_task_for_sku(sku) or {}
    selected = sourcing.get("selected")
    if isinstance(selected, dict):
        selected_source = SourcingCandidate.from_newton_row(selected)
        selected_key = (
            str(selected.get("itemId") or "").strip(),
            str(selected.get("skuId") or "").strip(),
        )
        if selected_key == (draft.selected_1688_item_id, draft.selected_1688_sku_id):
            exact_title = selected.get("title") if isinstance(selected.get("title"), str) else ""
            exact_sku_name = selected.get("skuName") if isinstance(selected.get("skuName"), str) else ""
            restored = {
                "selected_1688_title": exact_title,
                "selected_1688_sku_name": exact_sku_name,
                "selected_1688_url": selected_source.offer_url,
                "purchase_price_cny": format(selected_source.unit_price, "f") if selected_source.unit_price is not None else "",
                "domestic_delivery_cny": format(selected_source.delivery_price, "f") if selected_source.delivery_price is not None else "",
                "selected_1688_landed_cost": format(selected_source.landed_cost, "f") if selected_source.landed_cost is not None else "",
            }
            for field_name, exact_value in restored.items():
                if exact_value and getattr(draft, field_name) != exact_value:
                    setattr(draft, field_name, exact_value)
                    changed = True
    if changed:
        repository.save_listing_draft(sku, draft.to_dict())
    return draft


def _update_draft(repository: ProductRepository, sku: str, value: dict[str, Any]) -> OzonListingDraft:
    draft = _draft_for_product(repository, sku)
    store_id = str(value.get("store_id") or "").strip()
    if store_id:
        record = draft.store_publications.get(store_id)
        if not isinstance(record, dict) or not isinstance(record.get("draft"), dict):
            raise ValueError("找不到指定店铺的商品草稿")
        incoming = value.get("draft")
        if not isinstance(incoming, dict):
            raise ValueError("店铺商品草稿格式错误")
        publication = OzonListingDraft.from_dict(record["draft"])
        old_primary_image = publication.primary_image
        old_images = list(publication.images)
        _apply_editable_draft_fields(publication, incoming)
        if publication.primary_image != old_primary_image or publication.images != old_images:
            publication.images_oss_ready = False
        publication.source_sku = sku
        if publication.status not in {
            "IMPORT_SUBMITTED", "IMPORT_PENDING", "IMPORT_UNCERTAIN",
            "IMPORT_COMPLETED_STOCK_PENDING", "PUBLISHED",
            "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED",
        }:
            publication.blockers = validate_listing_draft(publication)
            publication.status = "READY" if not publication.blockers else "DRAFT_BLOCKED"
        record.update({
            "offer_id": publication.offer_id,
            "title": publication.title,
            "description_ru": publication.description_ru,
            "primary_image": publication.primary_image,
            "draft": publication.to_dict(),
        })
        draft.store_publications[store_id] = record
        repository.save_listing_draft(sku, draft.to_dict())
        return draft
    old_primary_image = draft.primary_image
    old_images = list(draft.images)
    _apply_editable_draft_fields(draft, value)
    if draft.primary_image != old_primary_image or draft.images != old_images:
        draft.images_oss_ready = False
    product = repository.get_product_by_sku(sku) or {}
    draft.source_sku = sku
    draft.exported_category = str(product.get("category") or "")
    draft.weight_g = product.get("weight_grams")
    if draft.status not in {
        "IMPORT_SUBMITTED", "IMPORT_PENDING", "IMPORT_UNCERTAIN",
        "IMPORT_COMPLETED_STOCK_PENDING", "PUBLISHED",
        "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED",
    }:
        draft.blockers = validate_listing_draft(draft)
        draft.status = "READY" if not draft.blockers else "DRAFT_BLOCKED"
    repository.save_listing_draft(sku, draft.to_dict())
    return draft


def _apply_editable_draft_fields(draft: OzonListingDraft, value: dict[str, Any]) -> None:
    editable = {
        "offer_id", "title", "description_ru", "tags_ru", "depth_mm", "width_mm", "height_mm", "price",
        "old_price", "currency_code", "vat", "primary_image", "images",
        "attributes", "complex_attributes", "warehouse_id", "stock",
        "selected_1688_item_id", "selected_1688_sku_id", "selected_1688_url",
        "selected_1688_title", "selected_1688_sku_name",
        "product_manager_color", "product_manager_size", "product_manager_remark",
        "selected_1688_landed_cost",
        "purchase_price_cny", "domestic_delivery_cny", "cross_border_freight_cny",
        "pricing_result", "promotion_action_id",
        "store_inventory", "store_promotions", "store_action_target",
    }
    if draft.task_id or draft.status in {
        "IMPORT_SUBMITTED", "IMPORT_PENDING", "IMPORT_UNCERTAIN",
        "IMPORT_COMPLETED_STOCK_PENDING", "PUBLISHED",
        "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED",
    }:
        editable.discard("offer_id")
    for name in editable:
        if name in value:
            setattr(draft, name, value[name])


def _selected_newton_source(
    sourcing: dict[str, Any] | None, draft: OzonListingDraft
) -> SourcingCandidate:
    if not sourcing:
        raise ValueError("尚未完成1688找货源，不能自动定价")
    selected = sourcing.get("selected")
    if isinstance(selected, dict):
        candidate = SourcingCandidate.from_newton_row(selected)
        if candidate.item_id and candidate.sku_id:
            return candidate
    rows = extract_newton_candidate_rows(sourcing.get("tables", []))
    if draft.selected_1688_item_id and draft.selected_1688_sku_id:
        for row in rows:
            if (str(row.get("itemId") or "") == draft.selected_1688_item_id
                    and str(row.get("skuId") or "") == draft.selected_1688_sku_id):
                return SourcingCandidate.from_newton_row(row)
    raise ValueError("找货源结果尚未确定严格同款SKU；程序不会按最低价猜选相似款")


def _save_selected_source(repository: ProductRepository, source_sku: str, source: SourcingCandidate) -> OzonListingDraft:
    draft = _draft_for_product(repository, source_sku)
    draft.selected_1688_item_id = source.item_id
    draft.selected_1688_sku_id = source.sku_id
    draft.selected_1688_url = str(source.raw.get("sourceUrl") or source.offer_url).strip()
    draft.selected_1688_title = source.raw.get("title") if isinstance(source.raw.get("title"), str) else ""
    draft.selected_1688_sku_name = source.raw.get("skuName") if isinstance(source.raw.get("skuName"), str) else ""
    draft.purchase_price_cny = format(source.unit_price, "f") if source.unit_price is not None else ""
    draft.domestic_delivery_cny = format(source.delivery_price, "f") if source.delivery_price is not None else ""
    draft.selected_1688_landed_cost = format(source.landed_cost, "f") if source.landed_cost is not None else ""
    repository.save_listing_draft(draft.source_sku, draft.to_dict())
    return draft


def _apply_automatic_pricing(
    draft: OzonListingDraft, sourcing: dict[str, Any] | None, config: AppConfig
) -> None:
    source = _selected_newton_source(sourcing, draft)
    if source.unit_price is None:
        raise ValueError("选中货源缺少1688 SKU采购价")
    if source.delivery_price is None:
        raise ValueError("选中货源缺少发往国内仓库的运费")
    landed_cost = source.unit_price + source.delivery_price
    result = calculate_actual_sale_for_target_cost_margin(
        target_cost_profit_margin=config.target_cost_profit_margin,
        purchase_price=landed_cost,
        length_mm=draft.depth_mm, width_mm=draft.width_mm, height_mm=draft.height_mm,
        weight_g=draft.weight_g,
        category_module=config.pricing_category_module,
        product_category=config.pricing_product_category,
        sticker_fee=config.pricing_sticker_fee,
        advertising_cost=config.pricing_operating_cost,
        return_rate=config.pricing_return_rate,
        logistics_service_rate=config.pricing_logistics_service_rate,
        price_multiplier=config.pricing_price_multiplier,
        old_price_multiplier=config.pricing_old_price_multiplier,
    )
    draft.selected_1688_item_id = source.item_id
    draft.selected_1688_sku_id = source.sku_id
    draft.selected_1688_url = str(source.raw.get("sourceUrl") or source.offer_url).strip()
    draft.selected_1688_title = source.raw.get("title") if isinstance(source.raw.get("title"), str) else ""
    draft.selected_1688_sku_name = source.raw.get("skuName") if isinstance(source.raw.get("skuName"), str) else ""
    draft.selected_1688_landed_cost = format(landed_cost, "f")
    draft.purchase_price_cny = format(source.unit_price, "f")
    draft.domestic_delivery_cny = format(source.delivery_price, "f")
    draft.cross_border_freight_cny = format(result.freight, "f")
    draft.price = format(result.ozon_price, "f")
    draft.old_price = format(result.ozon_old_price, "f")
    draft.currency_code = "CNY"
    draft.pricing_result = {
        "currency": "CNY", "target_cost_profit_margin_percent": config.target_cost_profit_margin,
        "actual_sale": format(result.actual_sale, "f"),
        "purchase_price": draft.purchase_price_cny,
        "domestic_delivery": draft.domestic_delivery_cny,
        "landed_purchase_cost": draft.selected_1688_landed_cost,
        "cross_border_freight": draft.cross_border_freight_cny,
        "sticker_payment_fee": format(result.sticker_fee, "f"),
        "operating_cost": format(result.advertising_cost, "f"),
        "return_loss": format(result.return_cost, "f"),
        "return_rate_percent": config.pricing_return_rate,
        "commission_rate_percent": format(result.commission_rate * 100, "f"),
        "commission": format(result.commission, "f"),
        "logistics_service_rate_percent": config.pricing_logistics_service_rate,
        "logistics_service_fee": format(result.logistics_commission, "f"),
        "profit": format(result.profit, "f"),
        "sales_profit_margin_percent": format(result.profit_margin * 100, ".2f"),
        "cost_profit_margin_percent": format(result.cost_profit_margin * 100, ".2f"),
        "price": draft.price, "old_price": draft.old_price,
        "category_module": config.pricing_category_module,
        "product_category": config.pricing_product_category,
        "source_item_id": source.item_id, "source_sku_id": source.sku_id,
    }


def _product_manager_enabled(config: AppConfig) -> bool:
    return str(config.product_manager_enabled or "").strip().casefold() not in {
        "", "0", "false", "no", "off",
    }


def _multi_store_enabled(config: AppConfig) -> bool:
    return str(config.multi_store_enabled or "").strip().casefold() not in {
        "", "0", "false", "no", "off",
    }


def _store_offer_id(prefix: str, base_offer_id: str) -> str:
    safe_prefix = re.sub(r"[^A-Za-z0-9._-]+", "-", str(prefix or "").strip()).strip("-._")
    base = str(base_offer_id or "").strip()
    if not safe_prefix:
        raise ValueError("多店铺上品已启用，但店铺Offer ID前缀为空")
    if not base:
        raise ValueError("商品的基础Offer ID为空")
    return f"{safe_prefix}-{base}"


def _configured_ozon_stores(config: AppConfig, *, require_credentials: bool = False) -> list[dict[str, str]]:
    """Return the new arbitrary store list, migrating legacy two-store settings in memory."""
    stores: list[dict[str, str]] = []
    if str(config.ozon_stores_json or "").strip():
        try:
            raw_stores = json.loads(config.ozon_stores_json)
        except json.JSONDecodeError as exc:
            raise ValueError("Ozon店铺列表配置损坏，请在系统设置中重新保存") from exc
        if not isinstance(raw_stores, list):
            raise ValueError("Ozon店铺列表必须是数组")
        for index, raw in enumerate(raw_stores):
            if not isinstance(raw, dict) or str(raw.get("enabled", "1")).strip() in {"0", "false", "False"}:
                continue
            stores.append({
                "id": re.sub(r"[^A-Za-z0-9._-]+", "-", str(raw.get("id") or f"store-{index + 1}")).strip("-") or f"store-{index + 1}",
                "name": str(raw.get("name") or f"店铺{index + 1}").strip(),
                "prefix": str(raw.get("prefix") or "").strip(),
                "client_id": str(raw.get("client_id") or "").strip(),
                "api_key": str(raw.get("api_key") or "").strip(),
                "api_base": str(raw.get("api_base") or "https://api-seller.ozon.ru").strip(),
            })
    if not stores:
        stores.append({
            "id": "primary", "name": str(config.ozon_store_name or "").strip() or "店铺1",
            "prefix": str(config.ozon_store_prefix or "").strip(), "client_id": config.ozon_client_id,
            "api_key": config.ozon_api_key, "api_base": config.ozon_api_base,
        })
        if _multi_store_enabled(config):
            stores.append({
                "id": "secondary", "name": str(config.ozon_secondary_store_name or "").strip() or "店铺2",
                "prefix": str(config.ozon_secondary_store_prefix or "").strip(),
                "client_id": config.ozon_secondary_client_id, "api_key": config.ozon_secondary_api_key,
                "api_base": config.ozon_secondary_api_base,
            })
    ids = [store["id"] for store in stores]
    names = [store["name"] for store in stores]
    prefixes = [store["prefix"].casefold() for store in stores]
    if len(set(ids)) != len(ids) or len(set(names)) != len(names):
        raise ValueError("Ozon店铺ID和店铺名称必须唯一")
    if len(stores) > 1 and (any(not value for value in prefixes) or len(set(prefixes)) != len(prefixes)):
        raise ValueError("多店铺的Offer ID前缀必须全部填写且不能重复")
    if require_credentials:
        missing = [store["name"] for store in stores if not store["client_id"] or not store["api_key"]]
        if missing:
            raise ValueError("以下Ozon店铺缺少Client ID或API Key：" + "、".join(missing))
    return stores


def _store_client(store: dict[str, str], repository: ProductRepository | None = None) -> OzonSellerClient:
    return OzonSellerClient(store["client_id"], store["api_key"], store["api_base"], repository=repository)


def _store_by_id(config: AppConfig, store_id: str, *, require_credentials: bool = True) -> dict[str, str]:
    normalized = str(store_id or "").strip()
    stores = _configured_ozon_stores(config, require_credentials=require_credentials)
    if not normalized and len(stores) == 1:
        return stores[0]
    store = next((item for item in stores if item["id"] == normalized), None)
    if not store:
        raise ValueError("找不到指定的Ozon店铺，请刷新系统设置后重试")
    return store


def _aggregate_store_status(publications: dict[str, Any]) -> str:
    """Keep the legacy product status useful while stores progress independently."""
    records = [item for item in publications.values() if isinstance(item, dict)]
    if not records:
        return "STORE_PUBLICATIONS_FAILED"
    statuses = [str(item.get("status") or "") for item in records]
    if all(value == "COMPLETED" for value in statuses):
        return "COMPLETED"
    if any(value in {"PUBLISHED", "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED"} for value in statuses):
        return "PUBLISHED"
    if any(value == "IMPORT_COMPLETED_STOCK_PENDING" for value in statuses):
        return "IMPORT_COMPLETED_STOCK_PENDING"
    if any(value == "READY" for value in statuses):
        return "READY"
    return "STORE_PUBLICATIONS_FAILED"


def _manually_complete_store_publication(
    draft: OzonListingDraft, store_id: str
) -> dict[str, Any]:
    """Mark one post-import store flow complete without calling an external API."""
    normalized_store_id = str(store_id or "").strip()
    if not normalized_store_id:
        raise ValueError("没有指定需要手动完成的店铺")
    record = draft.store_publications.get(normalized_store_id)
    if not isinstance(record, dict):
        raise ValueError("找不到指定店铺的发布记录")
    previous_status = str(record.get("status") or "")
    if previous_status not in {
        "IMPORT_COMPLETED_STOCK_PENDING", "PUBLISHED",
        "STOCK_COMPLETED_PROMOTION_PENDING",
    }:
        raise ValueError("该店铺当前不在待添加库存或待添加促销阶段")
    completed_at = datetime.now().isoformat(timespec="seconds")
    publication_payload = copy.deepcopy(record.get("draft")) if isinstance(record.get("draft"), dict) else {}
    publication_payload["status"] = "COMPLETED"
    record.update({
        "status": "COMPLETED",
        "stage": "MANUALLY_COMPLETED",
        "error": "",
        "draft": publication_payload,
        "completion_mode": "MANUAL",
        "manual_completion_from_status": previous_status,
        "manual_completed_at": completed_at,
        "updated_at": completed_at,
    })
    draft.status = _aggregate_store_status(draft.store_publications)
    draft.store_action_target = ""
    return record


def _migrate_store_publications(draft: OzonListingDraft, config: AppConfig) -> bool:
    """Normalize legacy store-name keys and seed already-published single-store drafts."""
    stores = _configured_ozon_stores(config)
    changed = False
    for store in stores:
        if store["id"] in draft.store_publications:
            record = draft.store_publications[store["id"]]
        elif store["name"] in draft.store_publications:
            record = draft.store_publications.pop(store["name"])
            draft.store_publications[store["id"]] = record
            changed = True
        else:
            continue
        if isinstance(record, dict):
            if record.get("store_id") != store["id"] or record.get("store_name") != store["name"]:
                record["store_id"] = store["id"]
                record["store_name"] = store["name"]
                changed = True
    legacy_statuses = {
        "IMPORT_COMPLETED_STOCK_PENDING", "PUBLISHED",
        "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED",
    }
    if not draft.store_publications and draft.status in legacy_statuses:
        store = stores[0]
        publication = draft.to_dict()
        publication["store_publications"] = {}
        publication["store_inventory"] = {}
        publication["store_promotions"] = {}
        publication["store_action_target"] = ""
        draft.store_publications[store["id"]] = {
            "store_id": store["id"], "store_name": store["name"],
            "status": draft.status, "stage": "LEGACY_MIGRATED",
            "offer_id": draft.offer_id, "task_id": draft.task_id,
            "title": draft.title, "description_ru": draft.description_ru,
            "primary_image": draft.primary_image, "draft": publication,
            "error": "", "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        changed = True
    return changed


def _sync_product_manager(app: WebApplication, draft: OzonListingDraft) -> dict[str, Any]:
    config = app.config_store.load()
    if not _product_manager_enabled(config):
        result = {
            "status": "SKIPPED",
            "operation": "skipped",
            "message": "ECS产品库自动同步未启用",
            "synced_at": datetime.now().isoformat(timespec="seconds"),
        }
        draft.product_manager_sync = result
        return result
    if draft.status not in {
        "IMPORT_COMPLETED_STOCK_PENDING", "PUBLISHED", "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED",
    }:
        raise ValueError("Ozon商品尚未导入成功，不能同步ECS产品库")
    shop_id = str(config.product_manager_shop_id or "").strip()
    if not shop_id.isdigit() or int(shop_id) <= 0:
        raise ValueError("请先在系统设置中选择ECS产品库店铺")
    sourcing = app.repository.get_latest_sourcing_task_for_sku(draft.source_sku)
    source = _selected_newton_source(sourcing, draft)
    payload = build_product_manager_payload(draft, source, int(shop_id))
    attempted_at = datetime.now().isoformat(timespec="seconds")
    try:
        result = app.product_manager_client().upsert_product(payload)
    except Exception as exc:
        draft.product_manager_sync = {
            "status": "FAILED",
            "error": str(exc),
            "attempted_at": attempted_at,
            "shop_id": int(shop_id),
            "sku": draft.base_offer_id or draft.offer_id,
            "source_url": payload["source_url"],
            "spec_model": payload["spec_model"],
        }
        raise
    draft.product_manager_sync = {
        **result,
        "status": "SYNCED",
        "synced_at": datetime.now().isoformat(timespec="seconds"),
        "shop_id": int(shop_id),
        "shop_name": result.get("shop_name") or config.product_manager_shop_name,
        "sku": result.get("sku") or draft.base_offer_id or draft.offer_id,
        "source_url": payload["source_url"],
        "purchase_price": payload["purchase_price"],
        "spec_model": payload["spec_model"],
        "image_url": payload["image_url"],
    }
    return draft.product_manager_sync


def _is_elastic_promotion(action: dict[str, Any]) -> bool:
    text = " ".join(str(action.get(key) or "") for key in (
        "title", "name", "description", "action_type", "discount_type",
    )).casefold()
    return any(marker in text for marker in ("эластич", "бустинг", "elastic", "boost", "弹性"))


def _promotion_display(action: dict[str, Any]) -> str:
    title = str(action.get("title") or action.get("name") or f"活动 {action.get('id')}")
    date_start = str(action.get("date_start") or "")[:10]
    date_end = str(action.get("date_end") or "")[:10]
    dates = f" | {date_start}—{date_end}" if date_start or date_end else ""
    return f"{title} | ID {action.get('id')}{dates}"


def _validate_promotion_result(result: dict[str, Any], product_id: int) -> None:
    payload = result.get("result", {}) if isinstance(result, dict) else {}
    if not isinstance(payload, dict):
        raise RuntimeError("Ozon促销接口未返回明确结果，商品保留在待添加促销页面")
    rejected = payload.get("rejected") or []
    if rejected:
        rejected = rejected if isinstance(rejected, list) else [rejected]
        raise RuntimeError("Ozon拒绝加入促销：" + json.dumps(rejected, ensure_ascii=False))
    accepted = payload.get("product_ids") or []
    accepted_ids = {int(value) for value in accepted if str(value).strip().isdigit()}
    if int(product_id) not in accepted_ids:
        raise RuntimeError("Ozon促销接口未确认接受该商品，商品保留在待添加促销页面")


def _promotion_action_price(draft: OzonListingDraft, config: AppConfig) -> Decimal:
    """Use the target transaction price, never the inflated listing price.

    Older drafts may predate pricing_result persistence. Their listing price was
    generated as actual_sale * pricing_price_multiplier, so reconstruct the
    target transaction price instead of sending the full listing price.
    """
    actual_sale = (draft.pricing_result or {}).get("actual_sale")
    if actual_sale not in (None, ""):
        try:
            value = Decimal(str(actual_sale))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("商品的实际成交基准价无效，不能加入促销") from exc
        if value <= 0:
            raise ValueError("商品的实际成交基准价必须大于0")
        return value.quantize(Decimal("0.01"))
    try:
        listing_price = Decimal(str(draft.price))
        multiplier = Decimal(str(config.pricing_price_multiplier))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("商品缺少有效的实际成交基准价，不能加入促销") from exc
    if listing_price <= 0 or multiplier <= 0:
        raise ValueError("商品缺少有效的实际成交基准价，不能加入促销")
    return (listing_price / multiplier).quantize(Decimal("0.01"))


def _activate_promotion(
    app: WebApplication, draft: OzonListingDraft, action_id: int,
    client: OzonSellerClient | None = None,
) -> dict[str, Any]:
    if draft.status not in {"PUBLISHED", "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED"}:
        raise ValueError("商品库存尚未添加成功，不能加入促销")
    if int(draft.stock or 0) <= 0:
        raise ValueError("Ozon促销要求可售库存大于0")
    client = client or app.ozon_client()
    actions = [item for item in client.promotions() if _is_elastic_promotion(item)]
    action = next((item for item in actions if int(item.get("id") or 0) == int(action_id)), None)
    if not action:
        raise ValueError("选择的弹性提升活动已失效，请重新读取活动")
    product = client.product_info_by_offer(draft.offer_id)
    product_id = int((product or {}).get("id") or (product or {}).get("product_id") or 0)
    if not product_id:
        raise RuntimeError("Ozon尚未返回该商品的Product ID，请稍后重试")
    candidate = client.find_promotion_candidate(action_id, product_id)
    if candidate is None:
        raise RuntimeError(
            "该商品目前不在活动候选列表。Ozon要求商品已有库存，且新商品可能暂时不能加入促销；"
            "商品已保留，可稍后重试。"
        )
    config_store = getattr(app, "config_store", None)
    config = config_store.load() if config_store is not None else AppConfig()
    action_price = _promotion_action_price(draft, config)
    maximum_raw = candidate.get("max_action_price")
    if maximum_raw not in (None, ""):
        maximum = Decimal(str(maximum_raw))
        if maximum > 0 and action_price > maximum:
            raise ValueError(f"促销价 {action_price} CNY 高于活动允许的最高促销价 {maximum} CNY")
    result = client.activate_promotion(action_id, product_id, action_price, int(draft.stock))
    _validate_promotion_result(result, product_id)
    draft.promotion_action_id = int(action_id)
    draft.promotion_action_title = str(action.get("title") or action.get("name") or action_id)
    draft.promotion_result = {
        **result,
        "product_id": product_id,
        "action_price": format(action_price, "f"),
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    }
    draft.status = "COMPLETED"
    return result


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "OzonLocalWeb/1.0"

    @property
    def app(self) -> WebApplication:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def do_GET(self) -> None:
        try:
            self._handle_get()
        except Exception as exc:
            self._error(exc)

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            is_gallery_bridge = (
                path == "/api/ozon-gallery/capture"
                and secrets.compare_digest(
                    self.headers.get("X-Ozon-Gallery-Bridge", ""),
                    "ozon-gallery-bridge-v1",
                )
            )
            if not is_gallery_bridge:
                self._require_csrf()
            self._handle_post()
        except Exception as exc:
            self._error(exc)

    def do_PUT(self) -> None:
        try:
            self._require_csrf()
            self._handle_put()
        except Exception as exc:
            self._error(exc)

    def do_DELETE(self) -> None:
        try:
            self._require_csrf()
            self._handle_delete()
        except Exception as exc:
            self._error(exc)

    def _handle_get(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/bootstrap":
            config = self.app.config_store.load()
            self._json({
                "csrf_token": self.app.csrf_token,
                "overview": self.app.repository.overview(),
                "configuration": _public_config(config),
            })
            return
        if path == "/api/product-manager/shops":
            shops = self.app.product_manager_client().shops()
            self._json({"items": shops})
            return
        if path == "/api/products":
            self._json({"items": self.app.repository.list_products()})
            return
        if path == "/api/workflows":
            jobs_by_sku = {
                job.source_sku: job.to_dict()
                for job in self.app.jobs.list()
                if job.source_sku
            }
            items = []
            config = self.app.config_store.load()
            for product in self.app.repository.list_products():
                sku = str(product.get("sku") or "")
                sourcing = _backfill_manual_review(
                    self.app.repository, self.app.repository.get_latest_sourcing_task_for_sku(sku)
                )
                saved_draft = self.app.repository.get_listing_draft(sku)
                if saved_draft:
                    migrated = OzonListingDraft.from_dict(saved_draft)
                    if _migrate_store_publications(migrated, config):
                        self.app.repository.save_listing_draft(sku, migrated.to_dict())
                        saved_draft = migrated.to_dict()
                items.append({
                    "product": product,
                    "draft": saved_draft,
                    "sourcing": sourcing,
                    "job": jobs_by_sku.get(sku),
                })
            self._json({"items": items})
            return
        match = re.fullmatch(r"/api/listings/([^/]+)/main-image-preview", path)
        if match:
            sku = unquote(match.group(1))
            draft = _draft_for_product(self.app.repository, sku)
            if _migrate_store_publications(draft, self.app.config_store.load()):
                self.app.repository.save_listing_draft(sku, draft.to_dict())
            query = parse_qs(urlparse(self.path).query)
            store_id = str((query.get("store_id") or [""])[0]).strip()
            preview_draft = draft
            if store_id:
                record = draft.store_publications.get(store_id) or {}
                if not isinstance(record.get("draft"), dict):
                    raise ValueError("指定店铺没有待核验的主图")
                preview_draft = OzonListingDraft.from_dict(record["draft"])
            target = pending_main_image_path(preview_draft)
            data = target.read_bytes()
            mime = mimetypes.guess_type(target.name)[0] or "image/jpeg"
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        match = re.fullmatch(r"/api/products/([^/]+)", path)
        if match:
            sku = unquote(match.group(1))
            product = self.app.repository.get_product_by_sku(sku)
            if not product:
                self._json({"error": "商品不存在"}, HTTPStatus.NOT_FOUND)
                return
            draft = _draft_for_product(self.app.repository, sku)
            if _migrate_store_publications(draft, self.app.config_store.load()):
                self.app.repository.save_listing_draft(sku, draft.to_dict())
            definitions: list[dict[str, Any]] = []
            if draft.description_category_id > 0 and draft.type_id > 0:
                try:
                    definitions = self.app.ozon_client().category_attributes(
                        draft.description_category_id, draft.type_id, "ZH_HANS"
                    )
                except (OSError, RuntimeError):
                    definitions = []
            if definitions and draft.status not in {
                "IMPORT_SUBMITTED", "IMPORT_PENDING", "IMPORT_UNCERTAIN", "IMPORT_FAILED",
                "IMPORT_COMPLETED_STOCK_PENDING", "PUBLISHED",
                "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED",
            }:
                apply_content_attributes(draft, definitions, {
                    "title_ru": draft.title,
                    "description_ru": draft.description_ru,
                    "tags_ru": draft.tags_ru,
                })
                enforce_system_attributes(draft, definitions)
                draft.blockers = validate_listing_draft(draft, definitions)
                draft.status = "READY" if not draft.blockers else "DRAFT_BLOCKED"
                self.app.repository.save_listing_draft(sku, draft.to_dict())
            self._json({"product": product, "draft": draft.to_dict(), "definitions": definitions})
            return
        match = re.fullmatch(r"/api/products/([^/]+)/sourcing-image", path)
        if match:
            sku = unquote(match.group(1))
            product = self.app.repository.get_product_by_sku(sku)
            if not product:
                self._json({"error": "商品不存在"}, HTTPStatus.NOT_FOUND)
                return
            draft = _draft_for_product(self.app.repository, sku)
            target = prepare_sourcing_image_file(product=product, draft=draft)
            data = target.read_bytes()
            mime = mimetypes.guess_type(target.name)[0] or "image/jpeg"
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", mime)
            self.send_header("Content-Disposition", f'inline; filename="ozon-{sku}.jpg"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/sourcing":
            items = self.app.repository.list_sourcing_tasks()
            for item in items:
                payload = item.get("payload")
                if isinstance(payload, dict):
                    item["payload"] = _backfill_manual_review(self.app.repository, payload) or payload
            self._json({"items": items})
            return
        if path == "/api/promotions":
            query = parse_qs(urlparse(self.path).query)
            store = _store_by_id(
                self.app.config_store.load(),
                str((query.get("store_id") or [""])[0]),
            )
            actions = [
                item for item in _store_client(store, self.app.repository).promotions()
                if _is_elastic_promotion(item)
            ]
            self._json({
                "store_id": store["id"], "store_name": store["name"],
                "items": [{**item, "display": _promotion_display(item)} for item in actions],
            })
            return
        match = re.fullmatch(r"/api/jobs/([^/]+)", path)
        if match:
            job = self.app.jobs.get(match.group(1))
            if not job:
                self._json({"error": "后台任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._json(job.to_dict())
            return
        self._serve_static(path)

    def _handle_put(self) -> None:
        path = urlparse(self.path).path
        match = re.fullmatch(r"/api/listings/([^/]+)", path)
        if match:
            sku = unquote(match.group(1))
            draft = _update_draft(self.app.repository, sku, self._read_json())
            self._json({"draft": draft.to_dict()})
            return
        if path == "/api/config":
            incoming = self._read_json()
            current = self.app.config_store.load()
            values = asdict(current)
            if isinstance(incoming.get("ozon_stores"), list):
                existing = {item["id"]: item for item in _configured_ozon_stores(current)}
                normalized_stores = []
                for index, raw in enumerate(incoming["ozon_stores"]):
                    if not isinstance(raw, dict):
                        continue
                    store_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(raw.get("id") or f"store-{index + 1}")).strip("-")
                    api_key = str(raw.get("api_key") or "").strip()
                    if not api_key and store_id in existing:
                        api_key = existing[store_id].get("api_key", "")
                    normalized_stores.append({
                        "id": store_id, "name": str(raw.get("name") or "").strip(),
                        "prefix": str(raw.get("prefix") or "").strip(),
                        "client_id": str(raw.get("client_id") or "").strip(), "api_key": api_key,
                        "api_base": str(raw.get("api_base") or "https://api-seller.ozon.ru").strip(),
                        "enabled": "1" if raw.get("enabled", True) not in {False, 0, "0"} else "0",
                    })
                values["ozon_stores_json"] = json.dumps(normalized_stores, ensure_ascii=False)
            for name in AppConfig.__dataclass_fields__:
                if name == "ozon_stores_json":
                    continue
                if name not in incoming or incoming[name] is None:
                    continue
                normalized = str(incoming[name]).strip()
                # Empty password fields intentionally mean "keep the saved secret";
                # ordinary fields may be cleared by the user.
                if name in SECRET_FIELDS and not normalized:
                    continue
                values[name] = normalized
            candidate = AppConfig(**values)
            _configured_ozon_stores(candidate)
            self.app.config_store.save(candidate)
            self._json({"configuration": _public_config(self.app.config_store.load())})
            return
        self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def _handle_delete(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/products":
            payload = self._read_json()
            skus = [str(value or "").strip() for value in payload.get("skus", [])]
            if not skus:
                raise ValueError("请至少选择一个需要删除的商品")
            running = {
                job.source_sku for job in self.app.jobs.list()
                if job.source_sku and job.status == "RUNNING"
            }
            blocked = sorted(set(skus) & running)
            if blocked:
                raise ValueError(f"以下商品仍有后台任务运行，不能删除：{', '.join(blocked)}")
            deleted = self.app.repository.delete_products(skus)
            self._json({
                "deleted": deleted,
                "count": len(deleted),
                "message": f"已删除 {len(deleted)} 个本地商品及其草稿和找源记录；Ozon、ECS及OSS云端数据未删除",
            })
            return
        match = re.fullmatch(r"/api/products/([^/]+)", path)
        if not match:
            self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
            return
        sku = unquote(match.group(1))
        if any(job.source_sku == sku and job.status == "RUNNING" for job in self.app.jobs.list()):
            raise ValueError("该商品仍有后台任务运行，请等待任务结束后再删除")
        if not self.app.repository.delete_product(sku):
            self._json({"error": "商品不存在"}, HTTPStatus.NOT_FOUND)
            return
        self._json({
            "deleted": True,
            "sku": sku,
            "message": "已删除本地商品、上架草稿和找源记录；Ozon、ECS及OSS云端数据未删除",
        })

    def _handle_post(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/ozon-gallery/capture":
            if not secrets.compare_digest(
                self.headers.get("X-Ozon-Gallery-Bridge", ""),
                "ozon-gallery-bridge-v1",
            ):
                raise PermissionError("图库桥接请求无效")
            payload = self._read_json()
            sku = str(payload.get("sku") or "").strip()
            page_url = str(payload.get("page_url") or "").strip()
            if not re.fullmatch(r"\d{6,}", sku):
                raise ValueError("图库桥接没有返回正确的Ozon SKU")
            if not page_url.startswith(("https://www.ozon.ru/product/", "https://ozon.ru/product/")):
                raise ValueError("图库桥接页面不是Ozon商品页")
            product = self.app.repository.get_product_by_sku(sku)
            if not product:
                raise ValueError(f"本地不存在Ozon SKU {sku}")
            images = save_extension_gallery_capture(
                sku,
                page_url=page_url,
                title=str(payload.get("title") or ""),
                images=list(payload.get("images") or []),
            )
            draft = _draft_for_product(self.app.repository, sku)
            additional = additional_gallery_images(
                images, str(product.get("image_url") or "")
            )
            # Ozon URLs are only capture inputs. Never persist them as listing
            # images; the background upload replaces them with OSS URLs.
            draft.images = [
                value for value in draft.images
                if "ozone.ru/" not in str(value or "").casefold()
            ]
            draft.images_oss_ready = False
            draft.ai_preparation = {
                **draft.ai_preparation,
                "gallery_status": "CAPTURED",
                "gallery_source": "edge-extension",
                "gallery_image_count": len(images),
                "gallery_warning": "",
            }
            self.app.repository.save_listing_draft(sku, draft.to_dict())
            job = self._queue_captured_gallery_upload(
                sku,
                page_url=page_url,
                title=str(payload.get("title") or ""),
                images=images,
            )
            self._json({
                "accepted": True,
                "sku": sku,
                "image_count": len(images),
                "additional_image_count": len(additional),
                "upload_job": job.to_dict() if job else None,
            })
            return
        if path == "/api/import":
            self._import_workbook()
            return
        if path == "/api/alibaba/variants":
            payload = self._read_json()
            source_url = str(payload.get("source_url") or "").strip()
            item_id = str(payload.get("item_id") or "").strip()
            if source_url:
                match_id = re.search(r"/offer/(\d+)", source_url)
                if not match_id:
                    raise ValueError("产品链接必须是包含商品ID的1688商品链接")
                item_id = match_id.group(1)
            else:
                source_url = f"https://detail.1688.com/offer/{item_id}.html"
            if not re.fullmatch(r"\d{6,}", item_id):
                raise ValueError("请先填写正确的1688商品链接或商品ID")
            product_info = self.app.alibaba_client().get_cross_product_info(item_id)
            variants = _extract_alibaba_variants(product_info)
            if not variants:
                raise ValueError("1688商品详情接口没有返回可选择的SKU变体")
            self._json({"item_id": item_id, "items": variants})
            return
        match = re.fullmatch(r"/api/listings/([^/]+)/(resolve|ai-prepare|ai-content|ai-image|prepare-store|approve-main-image|regenerate-main-image|gallery|price|submit|refresh|sync|stock|promotion|manual-complete)", path)
        if match:
            sku, action = unquote(match.group(1)), match.group(2)
            incoming = self._read_json()
            if incoming:
                _update_draft(self.app.repository, sku, incoming)
            job = self.app.jobs.start(
                action,
                lambda state: self._run_listing_job_with_state(state, sku, action),
                source_sku=sku,
            )
            self._json({"job": job.to_dict()}, HTTPStatus.ACCEPTED)
            return
        match = re.fullmatch(r"/api/listings/([^/]+)/attribute-values", path)
        if match:
            sku = unquote(match.group(1))
            payload = self._read_json()
            attribute_id = int(payload.get("attribute_id") or 0)

            def load_values(_state: BackgroundJob) -> list[dict[str, Any]]:
                draft = _draft_for_product(self.app.repository, sku)
                if draft.description_category_id <= 0 or draft.type_id <= 0:
                    raise ValueError("请先匹配Ozon实时类目")
                return self.app.ozon_client().category_attribute_values(
                    draft.description_category_id,
                    draft.type_id,
                    attribute_id,
                    language="ZH_HANS",
                )

            job = self.app.jobs.start("attribute-values", load_values)
            self._json({"job": job.to_dict()}, HTTPStatus.ACCEPTED)
            return
        if path == "/api/warehouses":
            payload = self._read_json()
            store = _store_by_id(
                self.app.config_store.load(), str(payload.get("store_id") or "")
            )
            job = self.app.jobs.start(
                "warehouses",
                lambda state: {
                    "store_id": store["id"], "store_name": store["name"],
                    "items": _store_client(store, self.app.repository).warehouses(),
                },
            )
            self._json({"job": job.to_dict()}, HTTPStatus.ACCEPTED)
            return
        if path == "/api/ozon-categories":
            def load_categories(state: BackgroundJob) -> list[dict[str, Any]]:
                if self.app.ozon_categories_cache:
                    state.message = "已读取Ozon实时类目缓存"
                    return self.app.ozon_categories_cache
                state.message = "正在从Ozon读取中文实时类目"
                categories = flatten_ozon_categories(
                    self.app.ozon_client().category_tree("ZH_HANS")
                )
                categories.sort(key=lambda item: str(item.get("display") or ""))
                self.app.ozon_categories_cache = categories
                return categories

            job = self.app.jobs.start("ozon-categories", load_categories)
            self._json({"job": job.to_dict()}, HTTPStatus.ACCEPTED)
            return
        if path == "/api/sourcing/start":
            payload = self._read_json()
            sku = str(payload.get("sku") or "").strip()
            count = int(payload.get("candidate_count") or 10)
            job = self.app.jobs.start(
                "sourcing",
                lambda state: self._run_queued_sourcing_job(
                    state, sku, count, force_new=True
                ),
                source_sku=sku,
                on_reserved=lambda: self.app.repository.enqueue_sourcing_job(sku, count),
            )
            self._json({"job": job.to_dict()}, HTTPStatus.ACCEPTED)
            return
        match = re.fullmatch(r"/api/sourcing/([^/]+)/(poll|resume|kill)", path)
        if match:
            task_id, action = unquote(match.group(1)), match.group(2)
            payload = self._read_json()
            job = self.app.jobs.start(
                f"sourcing-{action}",
                lambda state: self._sourcing_followup(state, task_id, action, payload),
                source_sku=str(saved.get("source_sku") or "") if (saved := self.app.repository.get_sourcing_task(task_id)) else "",
            )
            self._json({"job": job.to_dict()}, HTTPStatus.ACCEPTED)
            return
        match = re.fullmatch(r"/api/sourcing/([^/]+)/elim-detail", path)
        if match:
            task_id = unquote(match.group(1))
            payload = self._read_json()
            saved = self.app.repository.get_sourcing_task(task_id)
            if not saved or saved.get("provider") != "elim":
                raise ValueError("找不到 Elim 找货源任务")
            item_id = str(payload.get("item_id") or "").strip()
            rows = extract_newton_candidate_rows(saved.get("tables", []))
            matching = [row for row in rows if str(row.get("itemId") or "").strip() == item_id]
            if not matching:
                raise ValueError("该商品不在 Elim 搜图结果中")
            if not any(bool((row.get("newtonData") or {}).get("elim_needs_detail")) for row in matching):
                self._json({"task": saved, "cached": True})
                return
            elim_data = matching[0].get("elimData") if isinstance(matching[0].get("elimData"), dict) else {}
            offer = elim_data.get("search") if isinstance(elim_data.get("search"), dict) else {}
            detail = self.app.elim_client().product_detail(item_id)
            hydrated = normalize_elim_candidates({"items": [offer]}, {item_id: detail})
            if not hydrated:
                raise ValueError("Elim 商品详情没有返回可用的一件代发 SKU")
            remaining = [row for row in rows if str(row.get("itemId") or "").strip() != item_id]
            saved["tables"] = [{"result": [*remaining, *hydrated]}]
            saved["manual_candidates"] = build_manual_review_candidates([*remaining, *hydrated])
            saved["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self.app.repository.save_sourcing_task(saved)
            self._json({"task": saved, "cached": False, "sku_count": len(hydrated)})
            return
        match = re.fullmatch(r"/api/sourcing/([^/]+)/confirm-source", path)
        if match:
            task_id = unquote(match.group(1))
            payload = self._read_json()
            saved = self.app.repository.get_sourcing_task(task_id)
            if not saved:
                raise ValueError("找不到找货源任务")
            item_id = str(payload.get("item_id") or "").strip()
            sku_id = str(payload.get("sku_id") or "").strip()
            allowed = {
                (str(item.get("itemId") or ""), str(item.get("skuId") or ""))
                for item in saved.get("manual_candidates", []) if isinstance(item, dict)
            }
            if (item_id, sku_id) not in allowed:
                raise ValueError("该货源不在本任务的待人工核验候选中")
            rows = extract_newton_candidate_rows(saved.get("tables", []))
            row = next((item for item in rows if (
                str(item.get("itemId") or "").strip(),
                str(item.get("skuId") or "").strip(),
            ) == (item_id, sku_id)), None)
            if row is None:
                raise ValueError("找不到该货源的原始接口数据")
            if saved.get("provider") == "daji" and not str(row.get("realtimeDeliveryPrice") or "").strip():
                config = self.app.config_store.load()
                freight_payload = self.app.daji_client().freight_estimate(
                    item_id,
                    [(sku_id, 1)],
                    province_code=config.daji_freight_province_code,
                    city_code=config.daji_freight_city_code,
                    district_code=config.daji_freight_district_code,
                )
                freight_data = freight_payload.get("data") if isinstance(freight_payload, dict) else None
                if not isinstance(freight_data, dict):
                    raise ValueError("大吉运费接口没有返回业务数据")
                freight = freight_data.get("freight")
                if freight in (None, "") and freight_data.get("freePostage") is True:
                    freight = 0
                if freight in (None, ""):
                    raise ValueError("大吉没有返回该 SKU 发往国内仓库的运费")
                row["realtimeDeliveryPrice"] = freight
                row.setdefault("dajiData", {})["freight"] = freight_data
                saved["tables"] = [{"result": rows}]
                saved["manual_candidates"] = build_manual_review_candidates(rows)
            candidate = SourcingCandidate.from_newton_row(row)
            if candidate.minimum_order_quantity is None or not Decimal("0") < candidate.minimum_order_quantity <= Decimal("1"):
                raise ValueError("只允许确认一件起批货源")
            saved["manual_confirmed_at"] = datetime.now().isoformat(timespec="seconds")
            saved["manual_confirmed_item_id"] = item_id
            saved["manual_confirmed_sku_id"] = sku_id
            saved["selection_status"] = "SELECTED"
            saved["selection_message"] = "已由人工确认商品和具体变体"
            saved["selection_rejected"] = {}
            saved["selected"] = candidate.raw
            saved["manual_review_status"] = "CONFIRMED"
            saved["manual_review_error"] = ""
            draft = _save_selected_source(
                self.app.repository, str(saved.get("source_sku") or ""), candidate
            )
            self.app.repository.save_sourcing_task(saved)
            job = self._queue_ai_prepare(str(saved.get("source_sku") or ""))
            self._json({
                "task": saved,
                "draft": draft.to_dict(),
                "job": job.to_dict() if job else None,
            }, HTTPStatus.ACCEPTED if job else HTTPStatus.OK)
            return
        match = re.fullmatch(r"/api/sourcing/([^/]+)/manual-source", path)
        if match:
            task_id = unquote(match.group(1))
            saved = self.app.repository.get_sourcing_task(task_id)
            if not saved:
                raise ValueError("找不到找货源任务")
            payload = self._read_json()
            source_url, item_id = _normalize_1688_source(
                payload.get("source_url"), payload.get("item_id")
            )
            sku_id = str(payload.get("sku_id") or "").strip()
            sku_name = str(payload.get("sku_name") or "").strip()
            if not item_id or not sku_name:
                raise ValueError("请填写1688产品链接和准确规格型号")
            if not sku_id:
                sku_id = f"MANUAL-{item_id}"
            try:
                unit_price = float(payload.get("unit_price"))
                delivery_price = float(payload.get("delivery_price"))
                moq = 1.0
            except (TypeError, ValueError) as exc:
                raise ValueError("采购价和运费必须是数字") from exc
            row = {
                "itemId": item_id, "skuId": sku_id,
                "title": str(payload.get("title") or "人工添加1688货源").strip(),
                "skuName": sku_name,
                "imageUrl": str(payload.get("image_url") or "").strip(),
                "company": str(payload.get("company") or "").strip(),
                "realtimeSinglePrice": unit_price,
                "realtimeDeliveryPrice": delivery_price,
                "newtonData": {"min_order_qty": moq},
                "sourceUrl": source_url,
                "manualSource": True,
            }
            candidate = SourcingCandidate.from_newton_row(row)
            saved["selected"] = candidate.raw
            saved["selection_status"] = "SELECTED"
            saved["selection_message"] = "已人工添加并确认1688货源"
            saved["selection_rejected"] = {}
            saved["manual_review_status"] = "CONFIRMED"
            saved["manual_review_error"] = ""
            saved["manual_confirmed_at"] = datetime.now().isoformat(timespec="seconds")
            saved["manual_confirmed_item_id"] = item_id
            saved["manual_confirmed_sku_id"] = sku_id
            saved["manual_candidates"] = []
            draft = _save_selected_source(self.app.repository, str(saved.get("source_sku") or ""), candidate)
            draft.product_manager_color = str(payload.get("color") or "").strip()
            draft.product_manager_size = str(payload.get("size") or "").strip()
            draft.product_manager_remark = str(payload.get("remark") or "").strip()
            self.app.repository.save_listing_draft(draft.source_sku, draft.to_dict())
            self.app.repository.save_sourcing_task(saved)
            job = self._queue_ai_prepare(str(saved.get("source_sku") or ""))
            self._json({"task": saved, "draft": draft.to_dict(), "job": job.to_dict() if job else None}, HTTPStatus.ACCEPTED if job else HTTPStatus.OK)
            return
        self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def _queue_captured_gallery_upload(
        self,
        sku: str,
        *,
        page_url: str,
        title: str,
        images: list[str],
    ) -> BackgroundJob | None:
        for current in self.app.jobs.list():
            if current.source_sku == sku and current.status == "RUNNING":
                return current

        def upload(job: BackgroundJob) -> dict[str, Any]:
            draft = _draft_for_product(self.app.repository, sku)
            product = self.app.repository.get_product_by_sku(sku)
            if not product:
                raise ValueError("找不到来源商品")
            draft.workflow_stage = "gallery-upload"
            draft.workflow_state = "RUNNING"
            draft.workflow_message = "正在把Ozon附图上传到OSS"
            draft.workflow_error = ""
            self.app.repository.save_listing_draft(sku, draft.to_dict())
            gallery = SimpleNamespace(
                url=page_url,
                title=title,
                images=list(images),
                source="edge-extension",
            )
            try:
                result = upload_captured_gallery_images(
                    config=self.app.config_store.load(),
                    product=product,
                    draft=draft,
                    gallery=gallery,
                    on_progress=lambda message: setattr(job, "message", message),
                )
                draft.blockers = validate_listing_draft(draft)
                draft.status = "READY" if not draft.blockers else "DRAFT_BLOCKED"
                draft.workflow_state = "DONE"
                draft.workflow_message = f"已上传{len(draft.images)}张附图到OSS"
                draft.workflow_error = ""
                self.app.repository.save_listing_draft(sku, draft.to_dict())
                return {"draft": draft.to_dict(), "gallery": result}
            except Exception as exc:
                draft.workflow_state = "ERROR"
                draft.workflow_message = "Ozon附图上传OSS失败"
                draft.workflow_error = str(exc)
                self.app.repository.save_listing_draft(sku, draft.to_dict())
                raise

        return self.app.jobs.start(
            "gallery-upload",
            upload,
            source_sku=sku,
        )

    def _queue_ai_prepare(self, sku: str) -> BackgroundJob | None:
        """Generate content and then the main image after manual source selection."""
        sku = str(sku or "").strip()
        if not sku:
            return None
        draft = _draft_for_product(self.app.repository, sku)
        if not (draft.selected_1688_item_id and draft.selected_1688_sku_id):
            return None
        if draft.ai_preparation.get("prepared_at"):
            return None
        if draft.task_id or draft.status in {
            "IMPORT_SUBMITTED", "IMPORT_PENDING", "IMPORT_UNCERTAIN",
            "IMPORT_COMPLETED_STOCK_PENDING",
            "PUBLISHED", "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED",
        }:
            return None
        for current in self.app.jobs.list():
            if current.source_sku == sku and current.kind in {"ai-prepare", "ai-content", "ai-image"} and current.status == "RUNNING":
                return current
        return self.app.jobs.start(
            "ai-prepare",
            lambda state: self._run_listing_job_with_state(state, sku, "ai-prepare"),
            source_sku=sku,
        )

    def _run_listing_job_with_state(
        self, job: BackgroundJob, sku: str, action: str
    ) -> Any:
        draft = _draft_for_product(self.app.repository, sku)
        if _migrate_store_publications(draft, self.app.config_store.load()):
            self.app.repository.save_listing_draft(sku, draft.to_dict())
        draft.workflow_stage = action
        draft.workflow_state = "RUNNING"
        draft.workflow_message = "任务已启动"
        draft.workflow_error = ""
        draft.workflow_updated_at = datetime.now().isoformat(timespec="seconds")
        self.app.repository.save_listing_draft(sku, draft.to_dict())
        try:
            result = self._listing_job(job, sku, action)
        except Exception as exc:
            current = _draft_for_product(self.app.repository, sku)
            current.workflow_stage = action
            current.workflow_state = "ERROR"
            current.workflow_message = job.message
            current.workflow_error = str(exc)
            current.workflow_updated_at = datetime.now().isoformat(timespec="seconds")
            self.app.repository.save_listing_draft(sku, current.to_dict())
            raise
        current = _draft_for_product(self.app.repository, sku)
        current.workflow_stage = action
        current.workflow_state = "DONE"
        current.workflow_message = job.message
        current.workflow_error = ""
        current.workflow_updated_at = datetime.now().isoformat(timespec="seconds")
        self.app.repository.save_listing_draft(sku, current.to_dict())
        if isinstance(result, dict) and "draft" in result:
            result["draft"] = current.to_dict()
        return result

    def _prepare_store_variants(
        self,
        job: BackgroundJob,
        draft: OzonListingDraft,
        product: dict[str, Any],
        sourcing: dict[str, Any] | None,
        config: AppConfig,
        *,
        generate_image: bool = True,
        target_store_id: str = "",
    ) -> list[dict[str, Any]]:
        stores = _configured_ozon_stores(config, require_credentials=True)
        base_offer_id = (draft.base_offer_id or draft.offer_id).strip()
        draft.base_offer_id = base_offer_id
        results: list[dict[str, Any]] = []
        for index, store in enumerate(stores):
            if target_store_id and store["id"] != target_store_id:
                continue
            existing = draft.store_publications.get(store["id"]) or {}
            if not target_store_id and existing.get("status") in {
                "READY", "IMPORT_COMPLETED_STOCK_PENDING", "PUBLISHED",
                "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED",
            }:
                results.append({"store_id": store["id"], "status": existing["status"], "skipped": True})
                continue
            client = _store_client(store, self.app.repository)
            definitions = client.category_attributes(
                draft.description_category_id, draft.type_id, "ZH_HANS"
            )
            publication = OzonListingDraft.from_dict(copy.deepcopy(
                draft.to_dict() if index == 0 else existing.get("draft") or draft.to_dict()
            ))
            publication.store_publications = {}
            publication.store_inventory = {}
            publication.store_promotions = {}
            publication.store_action_target = ""
            publication.base_offer_id = base_offer_id
            publication.offer_id = (
                _store_offer_id(store["prefix"], base_offer_id)
                if len(stores) > 1 or store["prefix"] else base_offer_id
            )
            publication.task_id = None
            publication.product_manager_sync = {}
            publication.ozon_repair = {}
            publication.ai_preparation = {
                **publication.ai_preparation,
                "content_variant_label": store["name"],
                "store_creative_profile": store_creative_profile(index),
            }
            try:
                if index > 0 or bool(target_store_id):
                    job.message = f"{store['name']}：正在生成独立文案和属性"
                    other_variants = [
                        {
                            "store_name": item.get("store_name"),
                            "title_ru": item.get("title"),
                            "description_ru": item.get("description_ru"),
                        }
                        for item in draft.store_publications.values()
                        if item.get("store_id") != store["id"] and item.get("title")
                    ]
                    publication.ai_preparation = {
                        **publication.ai_preparation,
                        "must_differ_from_existing_copy": {
                            "primary": ({
                                "title_ru": draft.title,
                                "description_ru": draft.description_ru,
                                "tags_ru": draft.tags_ru,
                            } if index > 0 else {}),
                            "other_store_variants": other_variants,
                        },
                    }
                    draft.store_publications[store["id"]] = {
                        **existing, "store_id": store["id"], "store_name": store["name"],
                        "offer_id": publication.offer_id, "status": "GENERATING",
                        "stage": "GENERATING_CONTENT_AND_IMAGE", "error": "",
                        "draft": publication.to_dict(),
                    }
                    self.app.repository.save_listing_draft(draft.source_sku, draft.to_dict())
                    if generate_image and publication.ai_preparation.get("content_prepared_at") and not target_store_id:
                        prepare_main_image_from_checkpoint(
                            config=config, draft=publication,
                            on_progress=lambda message, name=store["name"]: setattr(job, "message", f"{name}：{message}"),
                        )
                    else:
                        prepare_listing_with_ai(
                            config=config, product=product, draft=publication,
                            definitions=definitions, ozon_client=client, sourcing=sourcing,
                            on_progress=lambda message, name=store["name"]: setattr(job, "message", f"{name}：{message}"),
                            generate_image=generate_image,
                        )
                    if generate_image:
                        _apply_automatic_pricing(publication, sourcing, config)
                if not generate_image:
                    status, stage, error = "CONTENT_READY", "CONTENT_READY", ""
                    publication.status = "DRAFT_BLOCKED"
                    record = {
                        **existing, "store_id": store["id"], "store_name": store["name"],
                        "offer_id": publication.offer_id, "status": status, "stage": stage,
                        "title": publication.title, "description_ru": publication.description_ru,
                        "primary_image": "", "draft": publication.to_dict(), "error": error,
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    draft.store_publications[store["id"]] = record
                    self.app.repository.save_listing_draft(draft.source_sku, draft.to_dict())
                    results.append({"store_id": store["id"], "store_name": store["name"], "status": status})
                    continue
                enforce_system_attributes(publication, definitions)
                publication.blockers = validate_listing_draft(publication, definitions)
                review_pending = publication.ai_preparation.get("image_review_status") == "PENDING"
                status = "IMAGE_REVIEW_PENDING" if review_pending else "READY" if not publication.blockers else "FAILED"
                stage = "IMAGE_REVIEW_PENDING" if review_pending else "READY_TO_SUBMIT" if status == "READY" else "PREPARATION_FAILED"
                error = "" if status in {"READY", "IMAGE_REVIEW_PENDING"} else "；".join(publication.blockers)
            except Exception as exc:
                status, stage, error = "FAILED", "PREPARATION_FAILED", str(exc)
            record = {
                **existing, "store_id": store["id"], "store_name": store["name"],
                "offer_id": publication.offer_id, "status": status, "stage": stage,
                "title": publication.title, "description_ru": publication.description_ru,
                "primary_image": publication.primary_image, "draft": publication.to_dict(),
                "error": error, "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            draft.store_publications[store["id"]] = record
            self.app.repository.save_listing_draft(draft.source_sku, draft.to_dict())
            results.append({"store_id": store["id"], "store_name": store["name"], "status": status, "error": error})
        if not generate_image:
            return results
        pending = [
            item for item in draft.store_publications.values()
            if item.get("status") not in {
                "READY", "IMPORT_COMPLETED_STOCK_PENDING", "PUBLISHED",
                "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED",
            }
        ]
        base_blockers = validate_listing_draft(draft)
        store_blockers = [
            f"{item.get('store_name') or item.get('store_id')}："
            + ("主图等待人工核验" if item.get("status") == "IMAGE_REVIEW_PENDING" else item.get("error") or "店铺版本准备失败")
            for item in pending
        ]
        draft.blockers = list(dict.fromkeys([*base_blockers, *store_blockers]))
        has_published_store = any(
            item.get("status") in {
                "IMPORT_COMPLETED_STOCK_PENDING", "PUBLISHED",
                "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED",
            }
            for item in draft.store_publications.values()
        )
        draft.status = (
            _aggregate_store_status(draft.store_publications)
            if has_published_store else "READY" if not draft.blockers else "DRAFT_BLOCKED"
        )
        return results

    def _listing_job(self, job: BackgroundJob, sku: str, action: str) -> Any:
        draft = _draft_for_product(self.app.repository, sku)
        workflow = OzonListingWorkflow(self.app.ozon_client())
        if action == "resolve":
            raise ValueError("Ozon类目已在导入Excel前确认，不再提供自动匹配类目操作")
        if action == "manual-complete":
            store_id = str(draft.store_action_target or "").strip()
            record = _manually_complete_store_publication(draft, store_id)
            job.message = f"{record.get('store_name') or store_id}：已手动移入完成"
            self.app.repository.save_listing_draft(sku, draft.to_dict())
            return {
                "draft": draft.to_dict(),
                "store_results": [{
                    "store_id": store_id,
                    "store_name": record.get("store_name") or store_id,
                    "status": "COMPLETED",
                    "completion_mode": "MANUAL",
                }],
            }
        if action == "prepare-store":
            store_id = str(draft.store_action_target or "").strip()
            if not store_id:
                raise ValueError("没有指定需要重新生成的店铺")
            product = self.app.repository.get_product_by_sku(sku) or {}
            sourcing = self.app.repository.get_latest_sourcing_task_for_sku(sku)
            results = self._prepare_store_variants(
                job, draft, product, sourcing, self.app.config_store.load(),
                target_store_id=store_id,
            )
            draft.store_action_target = ""
            self.app.repository.save_listing_draft(sku, draft.to_dict())
            if results and all(item.get("status") == "FAILED" for item in results):
                raise RuntimeError(results[0].get("error") or "店铺版本生成失败")
            return {"draft": draft.to_dict(), "store_results": results}
        if action == "gallery":
            product = self.app.repository.get_product_by_sku(sku)
            if not product:
                raise ValueError("找不到来源商品")
            job.message = "正在从Ozon商品页读取完整图库"
            result = refresh_additional_images(
                config=self.app.config_store.load(),
                product=product,
                draft=draft,
                on_progress=lambda message: setattr(job, "message", message),
            )
            draft.blockers = validate_listing_draft(draft)
            draft.status = "READY" if not draft.blockers else "DRAFT_BLOCKED"
            self.app.repository.save_listing_draft(sku, draft.to_dict())
            return {"draft": draft.to_dict(), "gallery": result}
        if action in {"ai-prepare", "ai-content", "ai-image"}:
            job.message = "正在读取已确认Ozon类目的属性"
            if draft.description_category_id <= 0 or draft.type_id <= 0:
                raise ValueError("该商品没有导入前确认的Ozon类目，请重新导入Excel并选择类目")
            definitions = workflow.client.category_attributes(
                draft.description_category_id, draft.type_id, "ZH_HANS"
            )
            if not draft.attributes and not draft.complex_attributes:
                draft.attributes, draft.complex_attributes = required_attribute_skeleton(definitions)
            product = self.app.repository.get_product_by_sku(sku)
            if not product:
                raise ValueError("找不到来源商品")
            sourcing = self.app.repository.get_latest_sourcing_task_for_sku(sku)
            config = self.app.config_store.load()
            stores = _configured_ozon_stores(config, require_credentials=True)
            primary_store = stores[0]
            draft.ai_preparation = {
                **draft.ai_preparation,
                "content_variant_label": primary_store["name"],
                "store_creative_profile": store_creative_profile(0),
            }
            saved_content = draft.ai_preparation.get("content")
            if isinstance(saved_content, dict):
                saved_content["store_creative_profile"] = store_creative_profile(0)
            pricing_error = ""
            try:
                if action == "ai-image" or (action == "ai-prepare" and draft.ai_preparation.get("content_prepared_at")):
                    job.message = "文案和属性已完成，正在单独生成主图"
                    result = prepare_main_image_from_checkpoint(
                        config=config,
                        draft=draft,
                        on_progress=lambda message: setattr(job, "message", message),
                    )
                else:
                    result = prepare_listing_with_ai(
                        config=config,
                        product=product,
                        draft=draft,
                        definitions=definitions,
                        ozon_client=workflow.client,
                        sourcing=sourcing,
                        on_progress=lambda message: setattr(job, "message", message),
                        on_content_checkpoint=lambda: self.app.repository.save_listing_draft(sku, draft.to_dict()),
                        generate_image=action == "ai-prepare",
                    )
                if action == "ai-content":
                    draft.blockers = validate_listing_draft(draft, definitions)
                    draft.status = "DRAFT_BLOCKED"
                    job.message = "主店文案已完成，正在生成其他店铺文案"
                    store_results = self._prepare_store_variants(
                        job, draft, product, sourcing, config,
                        generate_image=False,
                    )
                    return {
                        "draft": draft.to_dict(), "definitions": definitions,
                        "ai_result": result, "store_results": store_results,
                    }
                try:
                    job.message = "正在按目标利润率计算人民币售价"
                    _apply_automatic_pricing(draft, sourcing, config)
                except ValueError as exc:
                    pricing_error = str(exc)
                    draft.pricing_result = {"currency": "CNY", "error": pricing_error}
                draft.blockers = validate_listing_draft(draft, definitions)
                if pricing_error:
                    draft.blockers = list(dict.fromkeys([pricing_error, *draft.blockers]))
                draft.status = "READY" if not draft.blockers else "DRAFT_BLOCKED"
                store_results = []
                if draft.ai_preparation.get("image_review_status") == "APPROVED":
                    job.message = "主店版本已完成，正在生成其他店铺版本"
                    store_results = self._prepare_store_variants(
                        job, draft, product, sourcing, config
                    )
            finally:
                self.app.repository.save_listing_draft(sku, draft.to_dict())
            return {
                "draft": draft.to_dict(), "definitions": definitions,
                "ai_result": result, "store_results": store_results,
            }
        if action in {"approve-main-image", "regenerate-main-image"}:
            preparation = draft.ai_preparation
            config = self.app.config_store.load()
            target_store_id = str(draft.store_action_target or "").strip()
            if target_store_id:
                store = _store_by_id(config, target_store_id)
                record = draft.store_publications.get(target_store_id) or {}
                if not isinstance(record.get("draft"), dict):
                    raise ValueError("指定店铺没有待人工核验的主图")
                publication = OzonListingDraft.from_dict(record["draft"])
                if publication.ai_preparation.get("image_review_status") != "PENDING":
                    raise ValueError("指定店铺没有待人工核验的主图")
                if action == "approve-main-image":
                    job.message = f"{store['name']}：正在上传人工核验通过的主图"
                    result = approve_pending_main_image(
                        config=config, draft=publication,
                        on_progress=lambda message: setattr(job, "message", f"{store['name']}：{message}"),
                        reviewer="HUMAN",
                    )
                else:
                    job.message = f"{store['name']}：正在重新生成主图"
                    result = regenerate_pending_main_image(
                        config=config, draft=publication,
                        on_progress=lambda message: setattr(job, "message", f"{store['name']}：{message}"),
                    )
                definitions = _store_client(store, self.app.repository).category_attributes(
                    publication.description_category_id, publication.type_id, "ZH_HANS"
                )
                publication.blockers = validate_listing_draft(publication, definitions)
                review_pending = publication.ai_preparation.get("image_review_status") == "PENDING"
                record.update({
                    "status": "IMAGE_REVIEW_PENDING" if review_pending else "READY" if not publication.blockers else "FAILED",
                    "stage": "IMAGE_REVIEW_PENDING" if review_pending else "READY_TO_SUBMIT" if not publication.blockers else "PREPARATION_FAILED",
                    "error": "" if review_pending or not publication.blockers else "；".join(publication.blockers),
                    "title": publication.title, "description_ru": publication.description_ru,
                    "primary_image": publication.primary_image, "draft": publication.to_dict(),
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                })
                draft.store_action_target = ""
                base_blockers = validate_listing_draft(draft)
                pending_records = [
                    item for item in draft.store_publications.values()
                    if item.get("status") not in {
                        "READY", "IMPORT_COMPLETED_STOCK_PENDING", "PUBLISHED",
                        "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED",
                    }
                ]
                draft.blockers = list(dict.fromkeys([
                    *base_blockers,
                    *[
                        f"{item.get('store_name') or item.get('store_id')}："
                        + ("主图等待人工核验" if item.get("status") == "IMAGE_REVIEW_PENDING" else item.get("error") or "店铺版本准备失败")
                        for item in pending_records
                    ],
                ]))
                draft.status = "READY" if not draft.blockers else "DRAFT_BLOCKED"
                self.app.repository.save_listing_draft(sku, draft.to_dict())
                return {"draft": draft.to_dict(), "store_id": target_store_id, "ai_result": result}
            if preparation.get("image_review_status") != "PENDING":
                raise ValueError("该商品没有待人工核验的主图")
            definitions = workflow.client.category_attributes(
                draft.description_category_id, draft.type_id, "ZH_HANS"
            )
            if action == "approve-main-image":
                job.message = "正在上传人工核验通过的主图和附图"
                result = approve_pending_main_image(
                    config=config,
                    draft=draft,
                    on_progress=lambda message: setattr(job, "message", message),
                    reviewer="HUMAN",
                )
            else:
                job.message = "正在重新生成主图"
                result = regenerate_pending_main_image(
                    config=config,
                    draft=draft,
                    on_progress=lambda message: setattr(job, "message", message),
                )
            if draft.ai_preparation.get("prepared_at"):
                sourcing = self.app.repository.get_latest_sourcing_task_for_sku(sku)
                try:
                    _apply_automatic_pricing(draft, sourcing, config)
                except ValueError as exc:
                    draft.pricing_result = {"currency": "CNY", "error": str(exc)}
            draft.blockers = validate_listing_draft(draft, definitions)
            draft.status = "READY" if not draft.blockers else "DRAFT_BLOCKED"
            store_results = []
            if draft.ai_preparation.get("image_review_status") == "APPROVED":
                product = self.app.repository.get_product_by_sku(sku) or {}
                sourcing = self.app.repository.get_latest_sourcing_task_for_sku(sku)
                store_results = self._prepare_store_variants(
                    job, draft, product, sourcing, config
                )
            self.app.repository.save_listing_draft(sku, draft.to_dict())
            return {
                "draft": draft.to_dict(), "definitions": definitions,
                "ai_result": result, "store_results": store_results,
            }
        if action == "price":
            job.message = "正在按目标利润率计算人民币售价"
            sourcing = self.app.repository.get_latest_sourcing_task_for_sku(sku)
            _apply_automatic_pricing(draft, sourcing, self.app.config_store.load())
            draft.blockers = validate_listing_draft(draft)
            draft.status = "READY" if not draft.blockers else "DRAFT_BLOCKED"
            self.app.repository.save_listing_draft(sku, draft.to_dict())
            return {"draft": draft.to_dict()}
        if action == "submit":
            config = self.app.config_store.load()
            base_offer_id = (draft.base_offer_id or draft.offer_id).strip()
            draft.base_offer_id = base_offer_id
            stores = _configured_ozon_stores(config, require_credentials=True)
            product = self.app.repository.get_product_by_sku(sku) or {}
            sourcing = self.app.repository.get_latest_sourcing_task_for_sku(sku)
            results: list[dict[str, Any]] = []
            for index, store in enumerate(stores):
                if draft.store_action_target and store["id"] != draft.store_action_target:
                    continue
                existing = draft.store_publications.get(store["id"]) or draft.store_publications.get(store["name"]) or {}
                if existing.get("status") in {"IMPORT_COMPLETED_STOCK_PENDING", "PUBLISHED", "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED"}:
                    results.append({"store_id": store["id"], "store_name": store["name"], "status": existing["status"], "skipped": True})
                    continue
                if (
                    not isinstance(existing.get("draft"), dict)
                    or existing.get("stage") in {
                        "GENERATING_CONTENT_AND_IMAGE", "IMAGE_REVIEW_PENDING",
                        "PREPARATION_FAILED", "CONTENT_FAILED", "IMAGE_FAILED",
                    }
                ):
                    error = f"{store['name']}尚未完成独立文案和主图，请返回AI内容主图步骤生成或重试"
                    results.append({
                        "store_id": store["id"], "store_name": store["name"],
                        "status": "FAILED", "error": error,
                    })
                    continue
                publication = OzonListingDraft.from_dict(copy.deepcopy(existing.get("draft") or draft.to_dict()))
                publication.store_publications = {}
                publication.base_offer_id = base_offer_id
                publication.offer_id = _store_offer_id(store["prefix"], base_offer_id) if len(stores) > 1 or store["prefix"] else base_offer_id
                resume_import = bool(
                    publication.task_id
                    and publication.status in {"IMPORT_SUBMITTED", "IMPORT_PENDING", "IMPORT_UNCERTAIN"}
                )
                if not resume_import:
                    publication.task_id = None
                publication.product_manager_sync = {}
                publication.ozon_repair = {}
                client = _store_client(store, self.app.repository)
                store_workflow = OzonListingWorkflow(client)
                try:
                    definitions = client.category_attributes(publication.description_category_id, publication.type_id, "ZH_HANS")
                    publication.tags_ru = normalize_hashtags(publication.tags_ru)
                    enforce_system_attributes(publication, definitions)
                    publication.blockers = validate_listing_draft(publication, definitions)
                    if publication.blockers:
                        raise RuntimeError("；".join(publication.blockers))
                    publication.status = "READY"
                    if resume_import:
                        job.message = f"{store['name']}：正在查询原Ozon导入任务 {publication.task_id}"
                        import_result = store_workflow.refresh_import(publication)
                        if publication.status != "IMPORT_COMPLETED_STOCK_PENDING":
                            raise RuntimeError(f"Ozon导入任务 {publication.task_id} 尚未完成，请稍后只重试本店")
                        submission_data = {
                            "task_id": publication.task_id, "status": publication.status,
                            "import_result": import_result, "stock_pending": True,
                        }
                    else:
                        job.message = f"{store['name']}：正在提交并等待Ozon导入结果"
                        submission_data = asdict(store_workflow.submit(publication))
                    record = {
                        "store_id": store["id"], "store_name": store["name"], "status": publication.status,
                        "stage": "IMPORT_COMPLETED", "offer_id": publication.offer_id, "task_id": publication.task_id,
                        "title": publication.title, "description_ru": publication.description_ru,
                        "primary_image": publication.primary_image, "submission": submission_data,
                        "draft": publication.to_dict(), "error": "", "updated_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    draft.store_publications[store["id"]] = record
                    results.append({"store_id": store["id"], "store_name": store["name"], "status": publication.status})
                except Exception as exc:
                    stage = (
                        "IMPORT_PENDING"
                        if publication.task_id and publication.status in {"IMPORT_SUBMITTED", "IMPORT_PENDING", "IMPORT_UNCERTAIN"}
                        else "IMPORT_FAILED" if publication.task_id else "CONTENT_FAILED"
                    )
                    draft.store_publications[store["id"]] = {
                        **existing, "store_id": store["id"], "store_name": store["name"],
                        "status": "FAILED", "stage": stage, "offer_id": publication.offer_id,
                        "task_id": publication.task_id, "error": str(exc), "draft": publication.to_dict(),
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    results.append({"store_id": store["id"], "store_name": store["name"], "status": "FAILED", "error": str(exc)})
                finally:
                    self.app.repository.save_listing_draft(sku, draft.to_dict())
            successful_records = [
                item for item in draft.store_publications.values()
                if item.get("status") in {
                    "IMPORT_COMPLETED_STOCK_PENDING", "PUBLISHED",
                    "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED",
                }
            ]
            if successful_records:
                draft.status = _aggregate_store_status(draft.store_publications)
                first_success = next((item for item in draft.store_publications.values() if item.get("status") in {"IMPORT_COMPLETED_STOCK_PENDING", "PUBLISHED", "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED"}), {})
                draft.offer_id = first_success.get("offer_id") or draft.offer_id
                draft.task_id = first_success.get("task_id") or draft.task_id
                if draft.product_manager_sync.get("status") in {"SYNCED", "SKIPPED"}:
                    sync_result = draft.product_manager_sync
                else:
                    job.message = "已完成可发布店铺，正在以无前缀基础ID同步ECS产品库"
                    sync_result = _sync_product_manager(self.app, draft)
            else:
                draft.status = _aggregate_store_status(draft.store_publications)
                sync_result = None
            draft.store_action_target = ""
            self.app.repository.save_listing_draft(sku, draft.to_dict())
            failed_results = [item for item in results if item.get("status") == "FAILED"]
            if results and len(failed_results) == len(results):
                raise RuntimeError("；".join(
                    f"{item.get('store_name') or item.get('store_id')}：{item.get('error') or '提交失败'}"
                    for item in failed_results
                ))
            return {
                "draft": draft.to_dict(),
                "store_results": results,
                "product_manager_sync": sync_result,
            }
        if action == "refresh":
            try:
                result = workflow.refresh_import(draft)
                sync_result = None
                if draft.status == "IMPORT_COMPLETED_STOCK_PENDING" and draft.product_manager_sync.get("status") not in {"SYNCED", "SKIPPED"}:
                    job.message = "Ozon导入成功，正在同步ECS产品库"
                    sync_result = _sync_product_manager(self.app, draft)
            finally:
                self.app.repository.save_listing_draft(sku, draft.to_dict())
            return {"draft": draft.to_dict(), "result": result, "product_manager_sync": sync_result}
        if action == "sync":
            job.message = "正在同步ECS产品库"
            try:
                result = _sync_product_manager(self.app, draft)
            finally:
                self.app.repository.save_listing_draft(sku, draft.to_dict())
            return {"draft": draft.to_dict(), "product_manager_sync": result}
        if action == "stock":
            if draft.product_manager_sync.get("status") not in {"SYNCED", "SKIPPED"}:
                raise ValueError("请先完成ECS产品库同步，再发布Ozon库存")
            stores = {item["id"]: item for item in _configured_ozon_stores(self.app.config_store.load(), require_credentials=True)}
            results = []
            for store_id, record in draft.store_publications.items():
                if draft.store_action_target and store_id != draft.store_action_target:
                    continue
                if record.get("status") not in {"IMPORT_COMPLETED_STOCK_PENDING", "PUBLISHED", "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED"}:
                    continue
                if record.get("status") in {"PUBLISHED", "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED"}:
                    results.append({"store_id": store_id, "status": record["status"], "skipped": True})
                    continue
                store = stores.get(record.get("store_id") or store_id)
                if not store:
                    record.update(status="FAILED", stage="STOCK_FAILED", error="该店铺已从系统设置中删除")
                    continue
                settings = draft.store_inventory.get(store_id) or {}
                publication = OzonListingDraft.from_dict(record.get("draft") or {})
                publication.warehouse_id = int(settings.get("warehouse_id") or draft.warehouse_id or 0) or None
                publication.stock = int(settings.get("stock") or draft.stock or 0)
                try:
                    job.message = f"{store['name']}：正在等待商品生效并发布库存"
                    result = OzonListingWorkflow(_store_client(store, self.app.repository)).publish_stock(publication)
                    record.update(status=publication.status, stage="STOCK_COMPLETED", error="", draft=publication.to_dict(), stock_result=result)
                    results.append({"store_id": store_id, "status": publication.status})
                except Exception as exc:
                    record.update(status="IMPORT_COMPLETED_STOCK_PENDING", stage="STOCK_FAILED", error=str(exc), draft=publication.to_dict())
                    results.append({"store_id": store_id, "status": "FAILED", "error": str(exc)})
                finally:
                    self.app.repository.save_listing_draft(sku, draft.to_dict())
            draft.status = _aggregate_store_status(draft.store_publications)
            draft.store_action_target = ""
            self.app.repository.save_listing_draft(sku, draft.to_dict())
            return {"draft": draft.to_dict(), "store_results": results}
        stores = {item["id"]: item for item in _configured_ozon_stores(self.app.config_store.load(), require_credentials=True)}
        results = []
        for store_id, record in draft.store_publications.items():
            if draft.store_action_target and store_id != draft.store_action_target:
                continue
            if record.get("status") not in {"PUBLISHED", "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED"}:
                continue
            if record.get("status") == "COMPLETED":
                results.append({"store_id": store_id, "status": "COMPLETED", "skipped": True})
                continue
            store = stores.get(record.get("store_id") or store_id)
            settings = draft.store_promotions.get(store_id) or {}
            action_id = int(settings.get("action_id") or draft.promotion_action_id or 0)
            publication = OzonListingDraft.from_dict(record.get("draft") or {})
            if not store or not action_id:
                record.update(stage="PROMOTION_FAILED", error="店铺缺少促销活动选择或配置")
                results.append({"store_id": store_id, "status": "FAILED", "error": record["error"]})
                continue
            try:
                job.message = f"{store['name']}：正在检查活动资格并加入Ozon促销"
                result = _activate_promotion(self.app, publication, action_id, _store_client(store, self.app.repository))
                record.update(status="COMPLETED", stage="PROMOTION_COMPLETED", error="", draft=publication.to_dict(), promotion_result=result)
                results.append({"store_id": store_id, "status": "COMPLETED"})
            except Exception as exc:
                record.update(status=publication.status, stage="PROMOTION_FAILED", error=str(exc), draft=publication.to_dict())
                results.append({"store_id": store_id, "status": "FAILED", "error": str(exc)})
            finally:
                self.app.repository.save_listing_draft(sku, draft.to_dict())
        draft.status = _aggregate_store_status(draft.store_publications)
        draft.store_action_target = ""
        self.app.repository.save_listing_draft(sku, draft.to_dict())
        return {"draft": draft.to_dict(), "store_results": results}

    def _sourcing_job(self, job: BackgroundJob, sku: str, count: int) -> Any:
        product = self.app.repository.get_product_by_sku(sku)
        if not product:
            raise ValueError("找不到来源商品")
        config = self.app.config_store.load()
        provider = str(config.sourcing_provider or "").strip().lower()
        if provider == "elim":
            today = datetime.now().date().isoformat()
            if self.app.elim_blocked_date != today:
                self.app.elim_blocked_date = ""
                self.app.elim_blocked_error = ""
            if self.app.elim_blocked_error:
                raise RuntimeError(
                    "Elim 今日配额已暂停，程序不会继续消耗接口次数："
                    + self.app.elim_blocked_error
                )
            try:
                return self._elim_sourcing_job(job, product, sku, count)
            except ElimApiLimitError as exc:
                self.app.elim_blocked_date = today
                self.app.elim_blocked_error = str(exc)
                raise
        if provider == "daji" or (not provider and str(config.daji_enabled).strip() == "1"):
            today = datetime.now().date().isoformat()
            if self.app.daji_blocked_date != today:
                self.app.daji_blocked_date = ""
                self.app.daji_blocked_error = ""
            if self.app.daji_blocked_error:
                raise RuntimeError(
                    "大吉今日调用额度已暂停，程序不会继续消耗接口次数："
                    + self.app.daji_blocked_error
                )
            try:
                return self._daji_sourcing_job(job, product, sku, count, config)
            except DajiApiLimitError as exc:
                self.app.daji_blocked_date = today
                self.app.daji_blocked_error = str(exc)
                raise
        original_image_url = re.sub(r"/wc\d+/", "/wc2000/", str(product.get("image_url") or ""))
        if not original_image_url:
            raise ValueError("来源商品没有主图")
        draft = _draft_for_product(self.app.repository, sku)
        job.message = "正在把Ozon主图转存到OSS，确保牛顿可以读取"
        image_url = prepare_sourcing_image(
            config=self.app.config_store.load(), product=product, draft=draft
        )
        client = self.app.newton_client()
        message = build_ozon_sourcing_message(
            title=str(product.get("title") or ""),
            ozon_url=str(product.get("ozon_url") or ""),
            image_url=image_url,
            candidate_count=count,
        )
        job.message = "正在创建云牛顿找货源任务"
        created = client.create_procurement_search(
            message,
            image_urls=[image_url],
            image_names=[f"ozon_{sku}.jpg"],
        )
        base = {
            "task_id": created.task_id,
            "source_sku": sku,
            "session_id": created.session_id,
            "status": created.status,
            "next_index": created.next_index,
            "image_url": image_url,
            "original_image_url": original_image_url,
            "image_transport": "OSS",
            "message": message,
            "chunks": [_chunk_dict(chunk) for chunk in created.chunks],
            "questions": created.questions(),
            "tables": [],
            "error": created.error,
        }
        self.app.repository.save_sourcing_task(base)

        def update(snapshot: NewtonTaskSnapshot) -> None:
            job.message = f"云牛顿任务状态：{snapshot.status}"
            base.update({
                "status": snapshot.status,
                "session_id": snapshot.session_id or base["session_id"],
                "next_index": snapshot.next_index,
                "error": snapshot.error,
                "chunks": [
                    *base.get("chunks", []),
                    *[_chunk_dict(chunk) for chunk in snapshot.chunks],
                ],
            })
            self.app.repository.save_sourcing_task(base)

        result = client.collect_task(
            created.task_id,
            session_id=created.session_id,
            poll_interval=3,
            timeout_seconds=300,
            on_snapshot=update,
        )
        return self._store_sourcing_result(client, base, result, job)

    def _daji_sourcing_job(
        self,
        job: BackgroundJob,
        product: dict[str, Any],
        sku: str,
        count: int,
        config: AppConfig,
    ) -> dict[str, Any]:
        original_image_url = re.sub(
            r"/wc\d+/", "/wc2000/", str(product.get("image_url") or "")
        )
        if not original_image_url:
            raise ValueError("来源商品没有主图")
        draft = _draft_for_product(self.app.repository, sku)
        job.message = "正在准备大吉图搜图片"
        image_path = prepare_sourcing_image_file(product=product, draft=draft)
        client = self.app.daji_client()
        job.message = "正在上传图片到大吉 1688 图搜"
        image_id = client.upload_image(image_path)
        job.message = "正在搜索支持一件代发的 1688 候选"
        search = client.search_by_image(
            image_id,
            size=max(1, min(20, int(count or 10))),
            country=config.daji_country,
            filters=("isOnePsale",),
        )
        # One search result can contain many SKUs. Fetching the top three offer
        # details is enough for manual comparison and keeps daily API usage sane.
        offers = client.image_search_items(search)[:max(1, min(3, int(count or 10)))]
        details: dict[str, dict[str, Any]] = {}
        for index, offer in enumerate(offers, start=1):
            offer_id = str(offer.get("offerId") or "").strip()
            if not offer_id:
                continue
            job.message = f"正在读取大吉候选 SKU（{index}/{len(offers)}）"
            try:
                details[offer_id] = client.product_detail(
                    offer_id, country=config.daji_country
                )
            except DajiApiLimitError:
                # Do not swallow quota errors and continue calling the API for
                # the remaining offers.
                raise
            except Exception as exc:
                # Keep other candidates usable when one seller/detail is unavailable.
                details[offer_id] = {"data": {}, "detail_error": str(exc)}
        rows = normalize_daji_candidates(search, details)
        task_id = f"daji-{secrets.token_hex(10)}"
        manual_candidates = build_manual_review_candidates(rows)
        base = {
            "task_id": task_id,
            "provider": "daji",
            "source_sku": sku,
            "status": "END",
            "next_index": 0,
            "image_id": image_id,
            "image_url": original_image_url,
            "original_image_url": original_image_url,
            "image_transport": "DAJI_UPLOAD",
            "chunks": [],
            "questions": [],
            "tables": [{"result": rows}],
            "selected": None,
            "selection_status": "MANUAL_REVIEW_REQUIRED",
            "selection_message": "大吉已返回一件代发候选，请人工核验商品和具体变体",
            "selection_rejected": {},
            "manual_candidates": manual_candidates,
            "manual_review_status": "PENDING" if manual_candidates else "",
            "manual_review_rule_version": 5,
            "manual_review_error": "" if manual_candidates else "大吉没有返回可确认的一件代发 SKU",
            "daji_search": search,
            "error": "",
        }
        self.app.repository.save_sourcing_task(base)
        job.message = f"大吉找源完成，返回 {len(manual_candidates)} 个待核验 SKU"
        return {"task": base, "next_job": None}

    def _elim_sourcing_job(
        self,
        job: BackgroundJob,
        product: dict[str, Any],
        sku: str,
        count: int,
    ) -> dict[str, Any]:
        original_image_url = re.sub(
            r"/wc\d+/", "/wc2000/", str(product.get("image_url") or "")
        )
        if not original_image_url:
            raise ValueError("来源商品没有主图")
        draft = _draft_for_product(self.app.repository, sku)
        job.message = "正在准备 Elim 图搜图片"
        image_path = prepare_sourcing_image_file(product=product, draft=draft)
        client = self.app.elim_client()
        job.message = "正在上传图片到 Elim 1688 图搜"
        image_id = client.upload_image(image_path)
        job.message = "正在通过 Elim 搜索 1688 候选"
        search = client.search_by_image(image_id, size=max(1, min(20, int(count or 10))))
        # Keep the initial search at two calls (upload + image search). Product
        # details and real SKUs are loaded only after the user chooses a match.
        rows = normalize_elim_candidates(search, {})
        task_id = f"elim-{secrets.token_hex(10)}"
        manual_candidates = build_manual_review_candidates(rows)
        base = {
            "task_id": task_id,
            "provider": "elim",
            "source_sku": sku,
            "status": "END",
            "next_index": 0,
            "image_id": image_id,
            "image_url": original_image_url,
            "original_image_url": original_image_url,
            "image_transport": "ELIM_UPLOAD",
            "chunks": [],
            "questions": [],
            "tables": [{"result": rows}],
            "selected": None,
            "selection_status": "MANUAL_REVIEW_REQUIRED",
            "selection_message": "Elim 已返回 1688 候选，请人工核验商品和具体变体",
            "selection_rejected": {},
            "manual_candidates": manual_candidates,
            "manual_review_status": "PENDING" if manual_candidates else "",
            "manual_review_rule_version": 5,
            "manual_review_error": "" if manual_candidates else "Elim 没有返回可确认的 SKU",
            "elim_search": search,
            "error": "",
        }
        self.app.repository.save_sourcing_task(base)
        job.message = f"Elim 找源完成，返回 {len(manual_candidates)} 个待核验 SKU"
        return {"task": base, "next_job": None}

    def _resume_sourcing_job(
        self, job: BackgroundJob, saved: dict[str, Any]
    ) -> Any:
        task_id = str(saved.get("task_id") or "")
        if not task_id:
            raise ValueError("已保存的找货源任务缺少任务ID")
        client = self.app.newton_client()
        existing_chunks = [
            NewtonChunk.from_value(item) for item in saved.get("chunks", [])
        ]

        def update(snapshot: NewtonTaskSnapshot) -> None:
            job.message = f"正在恢复云牛顿任务：{snapshot.status}"
            saved.update({
                "status": snapshot.status,
                "session_id": snapshot.session_id or saved.get("session_id", ""),
                "next_index": snapshot.next_index,
                "error": snapshot.error,
                "chunks": [
                    *saved.get("chunks", []),
                    *[_chunk_dict(chunk) for chunk in snapshot.chunks],
                ],
            })
            self.app.repository.save_sourcing_task(saved)

        job.message = "正在从已保存进度恢复云牛顿任务"
        result = client.collect_task(
            task_id,
            session_id=str(saved.get("session_id") or ""),
            from_index=int(saved.get("next_index") or 0),
            poll_interval=3,
            timeout_seconds=300,
            on_snapshot=update,
        )
        if isinstance(result, NewtonTaskResult):
            combined: NewtonTaskResult | NewtonTaskSnapshot = NewtonTaskResult(
                task_id=result.task_id,
                session_id=result.session_id,
                status=result.status,
                chunks=[*existing_chunks, *result.chunks],
                next_index=result.next_index,
                raw=result.raw,
            )
        else:
            combined = NewtonTaskSnapshot(
                task_id=result.task_id,
                session_id=result.session_id,
                status=result.status,
                success=result.success,
                next_index=result.next_index,
                chunks=[*existing_chunks, *result.chunks],
                error=result.error,
                error_code=result.error_code,
                eagle_trace_id=result.eagle_trace_id,
                raw=result.raw,
            )
        return self._store_sourcing_result(client, saved, combined, job)

    def _run_queued_sourcing_job(
        self, job: BackgroundJob, sku: str, count: int, *, force_new: bool = False
    ) -> Any:
        job.message = "已进入可恢复队列，等待云牛顿执行名额"
        with self.app.sourcing_slots:
            saved = self.app.repository.get_latest_sourcing_task_for_sku(sku)
            status = str((saved or {}).get("status") or "").upper()
            if saved and status in {"INIT", "QUEUED", "RUNNING", "WAIT_SKILL"}:
                result = self._resume_sourcing_job(job, saved)
            elif force_new:
                result = self._sourcing_job(job, sku, count)
            elif saved and status == "END" and not saved.get("tables"):
                client = self.app.newton_client()
                completed = NewtonTaskResult(
                    task_id=str(saved.get("task_id") or ""),
                    session_id=str(saved.get("session_id") or ""),
                    status="END",
                    chunks=[NewtonChunk.from_value(item) for item in saved.get("chunks", [])],
                    next_index=int(saved.get("next_index") or 0),
                    raw={},
                )
                result = self._store_sourcing_result(client, saved, completed, job)
            elif saved:
                result = {"task": saved, "next_job": None}
            else:
                result = self._sourcing_job(job, sku, count)
            self.app.repository.remove_sourcing_job(sku)
            return result

    def _sourcing_followup(
        self, job: BackgroundJob, task_id: str, action: str, payload: dict[str, Any]
    ) -> Any:
        saved = self.app.repository.get_sourcing_task(task_id)
        if not saved:
            raise ValueError("找不到云牛顿任务")
        if saved.get("provider") in {"daji", "elim"}:
            if action == "kill":
                raise ValueError("API 图搜已经完成，无需终止")
            return {"task": saved}
        client = self.app.newton_client()
        if action == "kill":
            result = client.kill_task(task_id)
            saved["status"] = "KILL"
            self.app.repository.save_sourcing_task(saved)
            return {"task": saved, "result": result}
        if action == "resume":
            answers = [
                (str(item.get("question") or ""), str(item.get("answer") or ""))
                for item in payload.get("answers", []) if isinstance(item, dict)
            ]
            client.resume_task(
                session_id=str(saved.get("session_id") or ""),
                task_id=task_id,
                selected_data=build_selected_data(answers),
            )
        job.message = "正在读取云牛顿任务进度"
        snapshot = client.get_task(task_id, from_index=int(saved.get("next_index") or 0))
        saved["status"] = snapshot.status
        saved["session_id"] = snapshot.session_id or saved.get("session_id", "")
        saved["next_index"] = snapshot.next_index
        saved["chunks"] = [*saved.get("chunks", []), *[_chunk_dict(c) for c in snapshot.chunks]]
        saved["questions"] = snapshot.questions()
        saved["error"] = snapshot.error
        if snapshot.status == "END":
            result = NewtonTaskResult(
                task_id=task_id,
                session_id=str(saved.get("session_id") or ""),
                status="END",
                chunks=[NewtonChunk.from_value(item) for item in saved.get("chunks", [])],
                next_index=snapshot.next_index,
                raw=snapshot.raw,
            )
            return self._store_sourcing_result(client, saved, result, job)
        self.app.repository.save_sourcing_task(saved)
        return {"task": saved}

    def _store_sourcing_result(
        self,
        client: CloudNewtonClient,
        base: dict[str, Any],
        result: NewtonTaskResult | NewtonTaskSnapshot,
        job: BackgroundJob | None = None,
    ) -> dict[str, Any]:
        base["status"] = result.status
        base["session_id"] = result.session_id or base.get("session_id", "")
        base["next_index"] = result.next_index
        base["chunks"] = [_chunk_dict(chunk) for chunk in result.chunks]
        if isinstance(result, NewtonTaskSnapshot):
            base["questions"] = result.questions()
            base["error"] = result.error
        else:
            base["questions"] = []
            if job:
                job.message = "正在读取牛顿候选商品和SKU价格"
            base["tables"] = client.fetch_result_tables(result)
            rows = extract_newton_candidate_rows(base["tables"])
            base["selected"] = None
            base["selection_status"] = "MANUAL_REVIEW_REQUIRED"
            base["selection_message"] = "所有一件起批候选必须人工审核"
            base["selection_rejected"] = {}
            base["verification_decisions"] = []
            base["manual_candidates"] = build_manual_review_candidates(rows)
            base["manual_review_status"] = "PENDING" if base["manual_candidates"] else ""
            base["manual_review_rule_version"] = 5
            base["manual_review_error"] = "" if base["manual_candidates"] else "牛顿没有返回一件起批候选"
        self.app.repository.save_sourcing_task(base)
        return {
            "task": base,
            "next_job": None,
        }

    def _import_workbook(self) -> None:
        length = self._content_length()
        filename = Path(unquote(self.headers.get("X-Filename", "seerfar.xlsx"))).name
        try:
            description_category_id = int(self.headers.get("X-Ozon-Description-Category-Id", "0"))
            type_id = int(self.headers.get("X-Ozon-Type-Id", "0"))
        except ValueError as exc:
            raise ValueError("导入前选择的Ozon类目ID无效") from exc
        category_display = unquote(self.headers.get("X-Ozon-Category-Display", "")).strip()
        if description_category_id <= 0 or type_id <= 0 or not category_display:
            raise ValueError("请先在页面确认Ozon实时类目，再导入Excel")
        if Path(filename).suffix.lower() not in {".xlsx", ".xlsm"}:
            raise ValueError("仅支持 .xlsx 或 .xlsm 文件")
        ensure_runtime_directories()
        import_dir = DATA_DIR / "imports"
        import_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "seerfar.xlsx"
        target = import_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{safe_name}"
        target.write_bytes(self.rfile.read(length))
        products = import_seerfar_workbook(target)
        for product in products:
            product.category = category_display
        inserted, updated = self.app.repository.upsert_many(products)
        manual_pending = 0
        for product in products:
            sku = str(product.sku or "").strip()
            if not sku:
                continue
            draft = _draft_for_product(self.app.repository, sku)
            if not draft.task_id and draft.status not in {
                "IMPORT_SUBMITTED", "IMPORT_PENDING", "IMPORT_UNCERTAIN",
                "IMPORT_COMPLETED_STOCK_PENDING",
                "PUBLISHED", "STOCK_COMPLETED_PROMOTION_PENDING", "COMPLETED",
            }:
                draft.exported_category = category_display
                draft.description_category_id = description_category_id
                draft.type_id = type_id
                draft.category_display_zh = category_display
                draft.category_display_ru = ""
                draft.attributes = []
                draft.complex_attributes = []
                draft.blockers = validate_listing_draft(draft)
                draft.status = "READY" if not draft.blockers else "DRAFT_BLOCKED"
                self.app.repository.save_listing_draft(sku, draft.to_dict())
            draft = _draft_for_product(self.app.repository, sku)
            if draft.selected_1688_item_id and draft.selected_1688_sku_id:
                continue
            task_id = f"manual-{sku}-{secrets.token_hex(6)}"
            original_image_url = re.sub(
                r"/wc\d+/", "/wc2000/", str(product.image_url or "")
            )
            self.app.repository.save_sourcing_task({
                "task_id": task_id,
                "provider": "manual",
                "source_sku": sku,
                "status": "END",
                "next_index": 0,
                "image_url": original_image_url,
                "original_image_url": original_image_url,
                "image_transport": "URL",
                "chunks": [],
                "questions": [],
                "tables": [],
                "selected": None,
                "selection_status": "MANUAL_REVIEW_REQUIRED",
                "selection_message": "等待人工通过1688网页查找并确认货源",
                "selection_rejected": {},
                "manual_candidates": [],
                "manual_review_status": "PENDING",
                "manual_review_rule_version": 6,
                "manual_review_error": "",
                "error": "",
            })
            manual_pending += 1
        self._json({
            "read": len(products), "inserted": inserted, "updated": updated,
            "auto_sourcing_queued": 0,
            "manual_sourcing_pending": manual_pending,
            "ozon_category": {
                "description_category_id": description_category_id,
                "type_id": type_id,
                "display": category_display,
            },
        })

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else unquote(path.lstrip("/"))
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            self._json({"error": "非法路径"}, HTTPStatus.BAD_REQUEST)
            return
        if not target.is_file():
            target = STATIC_DIR / "index.html"
        data = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self._security_headers()
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = self._content_length()
        if not length:
            return {}
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求 JSON 必须是对象")
        return value

    def _content_length(self) -> int:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("无效的请求长度") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("请求内容过大")
        return length

    def _require_csrf(self) -> None:
        if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.app.csrf_token):
            raise PermissionError("本地会话令牌无效，请刷新页面")

    def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' https: data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        )
        self.send_header("Cache-Control", "no-store")

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, PermissionError):
            status = HTTPStatus.FORBIDDEN
        elif isinstance(exc, (OSError, RuntimeError)):
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        else:
            status = HTTPStatus.BAD_REQUEST
        try:
            self._json({"error": str(exc) or type(exc).__name__}, status)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _public_config(config: AppConfig) -> dict[str, Any]:
    ozon_stores = _configured_ozon_stores(config)
    freight_codes = (
        config.daji_freight_province_code,
        config.daji_freight_city_code,
        config.daji_freight_district_code,
    )
    freight_ready = all(str(value or "").strip() for value in freight_codes)
    known_addresses = {
        ("330000", "330700", "330782"): "浙江省 · 金华市 · 义乌市",
    }
    freight_address = known_addresses.get(
        tuple(str(value or "").strip() for value in freight_codes),
        " / ".join(
            str(value or "").strip()
            for value in freight_codes if str(value or "").strip()
        ),
    )
    return {
        "ozon_stores": [
            {
                "id": store["id"], "name": store["name"], "prefix": store["prefix"],
                "client_id": store["client_id"], "api_base": store["api_base"],
                "api_key_configured": bool(store["api_key"]), "enabled": True,
            }
            for store in ozon_stores
        ],
        "sourcing_provider": str(config.sourcing_provider or "daji").strip().lower(),
        "elim_api_key_configured": bool(config.elim_api_key),
        "elim_api_base": config.elim_api_base,
        "daji_enabled": str(config.daji_enabled).strip() == "1",
        "daji_app_key": config.daji_app_key,
        "daji_app_secret_configured": bool(config.daji_app_secret),
        "daji_api_base": config.daji_api_base,
        "daji_country": config.daji_country,
        "daji_freight_province_code": config.daji_freight_province_code,
        "daji_freight_city_code": config.daji_freight_city_code,
        "daji_freight_district_code": config.daji_freight_district_code,
        "daji_freight_ready": freight_ready,
        "daji_freight_address": freight_address,
        "alibaba_app_key": config.alibaba_app_key,
        "alibaba_app_secret_configured": bool(config.alibaba_app_secret),
        "alibaba_access_token_configured": bool(config.alibaba_access_token),
        "ozon_client_id": config.ozon_client_id,
        "ozon_api_key_configured": bool(config.ozon_api_key),
        "ozon_api_base": config.ozon_api_base,
        "multi_store_enabled": _multi_store_enabled(config),
        "ozon_store_name": config.ozon_store_name,
        "ozon_store_prefix": config.ozon_store_prefix,
        "ozon_secondary_client_id": config.ozon_secondary_client_id,
        "ozon_secondary_api_key_configured": bool(config.ozon_secondary_api_key),
        "ozon_secondary_api_base": config.ozon_secondary_api_base,
        "ozon_secondary_store_name": config.ozon_secondary_store_name,
        "ozon_secondary_store_prefix": config.ozon_secondary_store_prefix,
        "ai_api_url": config.ai_api_url,
        "ai_api_key_configured": bool(config.ai_api_key),
        "ai_model": config.ai_model,
        "vision_api_url": config.vision_api_url,
        "vision_api_key_configured": bool(config.vision_api_key),
        "vision_model": config.vision_model,
        "image_api_url": config.image_api_url,
        "image_api_key_configured": bool(config.image_api_key),
        "image_model": config.image_model,
        "oss_region": config.oss_region,
        "oss_endpoint": config.oss_endpoint,
        "oss_bucket": config.oss_bucket,
        "oss_public_base_url": config.oss_public_base_url,
        "oss_access_key_id_configured": bool(config.oss_access_key_id),
        "oss_access_key_secret_configured": bool(config.oss_access_key_secret),
        "oss_object_prefix": config.oss_object_prefix,
        "target_cost_profit_margin": config.target_cost_profit_margin,
        "pricing_sticker_fee": config.pricing_sticker_fee,
        "pricing_operating_cost": config.pricing_operating_cost,
        "pricing_return_rate": config.pricing_return_rate,
        "pricing_logistics_service_rate": config.pricing_logistics_service_rate,
        "pricing_price_multiplier": config.pricing_price_multiplier,
        "pricing_old_price_multiplier": config.pricing_old_price_multiplier,
        "pricing_category_module": config.pricing_category_module,
        "pricing_product_category": config.pricing_product_category,
        "product_manager_api_url": config.product_manager_api_url,
        "product_manager_api_token_configured": bool(config.product_manager_api_token),
        "product_manager_enabled": _product_manager_enabled(config),
        "product_manager_shop_id": config.product_manager_shop_id,
        "product_manager_shop_name": config.product_manager_shop_name,
        "pricing_tariffs": {
            module: {category: [format(rate * 100, "f") for rate in rates]
                     for category, rates in categories.items()}
            for module, categories in OZON_RFBS_COMMISSION_TARIFFS.items()
        },
    }


class LocalWebServer(ThreadingHTTPServer):
    daemon_threads = True
    # Do not permit multiple Windows processes to share port 8768.  Sharing
    # caused requests to alternate unpredictably between old and new code.
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], app: WebApplication) -> None:
        super().__init__(address, RequestHandler)
        self.app = app


def run(host: str = "127.0.0.1", port: int = 8768, *, open_browser: bool = True) -> None:
    ensure_runtime_directories()
    server = LocalWebServer((host, port), WebApplication())
    recovered = server.app.start_sourcing_recovery()
    recovered_workflows = server.app.start_workflow_recovery()
    url = f"http://{host}:{server.server_port}"
    print(f"Ozon本地工作台已启动：{url}")
    if recovered:
        print(f"已恢复 {recovered} 个待处理找货源任务")
    if recovered_workflows:
        print(f"已恢复 {recovered_workflows} 个待查询Ozon导入任务")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
