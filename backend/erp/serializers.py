from __future__ import annotations

from pathlib import Path
from typing import Any

from erp.core.jsonutil import loads
from erp.core.security import vault
from erp.models import (
    Asset,
    CatalogDuplicateSuggestion,
    CatalogImportRun,
    CatalogProduct,
    CatalogProductAsset,
    CatalogVariant,
    IntegrationConnection,
    Job,
    JobEvent,
    Listing,
    Publication,
    PublicationStore,
    ResearchProduct,
    SourcingCandidate,
    Store,
    StoreListing,
)


def iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def connection_dict(item: IntegrationConnection) -> dict[str, Any]:
    secrets = vault.decrypt(item.secrets_encrypted) if item.secrets_encrypted else {}
    return {
        "id": item.id,
        "provider": item.provider,
        "name": item.name,
        "base_url": item.base_url,
        "config": loads(item.config_json, {}),
        "secret_status": vault.public_status(secrets),
        "enabled": item.enabled,
        "status": item.status,
        "last_error": item.last_error,
        "last_tested_at": iso(item.last_tested_at),
        "created_at": iso(item.created_at),
        "updated_at": iso(item.updated_at),
    }


def store_dict(item: Store) -> dict[str, Any]:
    secrets = vault.decrypt(item.secrets_encrypted) if item.secrets_encrypted else {}
    return {
        "id": item.id,
        "platform": item.platform,
        "name": item.name,
        "external_account_id": item.external_account_id or "",
        "offer_prefix": item.offer_prefix,
        "currency": item.currency,
        "enabled": item.enabled,
        "connection_id": item.connection_id,
        "config": loads(item.config_json, {}),
        "secret_status": vault.public_status(secrets),
        "created_at": iso(item.created_at),
        "updated_at": iso(item.updated_at),
    }


def asset_dict(item: Asset) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "name": item.name,
        "source": item.source,
        "local_path": item.local_path,
        "local_exists": bool(item.local_path and Path(item.local_path).is_file()),
        "oss_bucket": item.oss_bucket,
        "oss_key": item.oss_key,
        "oss_url": item.oss_url,
        "sha256": item.sha256,
        "mime_type": item.mime_type,
        "size_bytes": item.size_bytes,
        "width": item.width,
        "height": item.height,
        "read_only": item.read_only,
        "mirror_status": item.mirror_status,
        "metadata": loads(item.metadata_json, {}),
        "created_at": iso(item.created_at),
    }


def catalog_variant_dict(item: CatalogVariant) -> dict[str, Any]:
    return {
        "id": item.id,
        "sku": item.sku,
        "supplier_product_id": item.supplier_product_id,
        "supplier_sku_id": item.supplier_sku_id,
        "specification": item.specification,
        "color": item.color,
        "size": item.size,
        "purchase_price_cny": item.purchase_price_cny,
        "domestic_delivery_cny": item.domestic_delivery_cny,
        "package_length_mm": item.package_length_mm,
        "package_width_mm": item.package_width_mm,
        "package_height_mm": item.package_height_mm,
        "weight_grams": item.weight_grams,
        "barcode": item.barcode,
        "active": item.active,
        "properties": loads(item.properties_json, {}),
        "legacy_source": item.legacy_source,
        "legacy_id": item.legacy_id,
    }


def catalog_asset_link_dict(item: CatalogProductAsset) -> dict[str, Any]:
    return {
        "id": item.id,
        "product_id": item.product_id,
        "variant_id": item.variant_id,
        "role": item.role,
        "position": item.position,
        "is_current": item.is_current,
        "source_kind": item.source_kind,
        "metadata": loads(item.metadata_json, {}),
        "asset": asset_dict(item.asset),
    }


def catalog_product_dict(item: CatalogProduct, *, include_details: bool = True) -> dict[str, Any]:
    current_links = [link for link in item.asset_links if link.is_current]
    thumbnail_link = next((link for link in current_links if link.role == "main"), current_links[0] if current_links else None)
    value = {
        "id": item.id,
        "sku": item.sku,
        "group_code": item.group_code,
        "source_type": item.source_type,
        "name": item.name,
        "category": item.category,
        "source_url": item.source_url,
        "supplier": item.supplier,
        "supplier_product_id": item.supplier_product_id,
        "purchase_price_cny": item.purchase_price_cny,
        "domestic_delivery_cny": item.domestic_delivery_cny,
        "currency": item.currency,
        "package_length_mm": item.package_length_mm,
        "package_width_mm": item.package_width_mm,
        "package_height_mm": item.package_height_mm,
        "package_weight_g": item.package_weight_g,
        "description_category_id": item.description_category_id,
        "type_id": item.type_id,
        "category_display": item.category_display,
        "notes": item.notes,
        "status": item.status,
        "tags": loads(item.tags_json, []),
        "legacy_source": item.legacy_source,
        "legacy_id": item.legacy_id,
        "variant_count": len(item.variants),
        "variant_skus": [variant.sku for variant in item.variants if variant.active],
        "current_asset_count": sum(1 for link in item.asset_links if link.is_current),
        "thumbnail": catalog_asset_link_dict(thumbnail_link) if thumbnail_link else None,
        "created_at": iso(item.created_at),
        "updated_at": iso(item.updated_at),
    }
    if include_details:
        value["production"] = loads(item.production_json, {})
        value["metadata"] = loads(item.metadata_json, {})
        value["source_snapshot"] = loads(item.source_snapshot_json, {})
        value["variants"] = [catalog_variant_dict(variant) for variant in item.variants]
        value["assets"] = [
            catalog_asset_link_dict(link)
            for link in sorted(item.asset_links, key=lambda row: (row.role != "main", row.position, str(row.id)))
            if link.is_current
        ]
    return value


def catalog_import_dict(item: CatalogImportRun) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "source_path": item.source_path,
        "dry_run": item.dry_run,
        "status": item.status,
        "job_id": item.job_id,
        "counts": loads(item.counts_json, {}),
        "errors": loads(item.errors_json, []),
        "report": loads(item.report_json, {}),
        "created_at": iso(item.created_at),
        "updated_at": iso(item.updated_at),
    }


def catalog_duplicate_dict(item: CatalogDuplicateSuggestion) -> dict[str, Any]:
    return {
        "id": item.id,
        "product_id": item.product_id,
        "candidate_product_id": item.candidate_product_id,
        "score": item.score,
        "reasons": loads(item.reasons_json, []),
        "status": item.status,
        "created_at": iso(item.created_at),
    }


def publication_store_dict(item: PublicationStore) -> dict[str, Any]:
    return {
        "id": item.id,
        "store_id": item.store_id,
        "store_name": item.store.name if item.store else "",
        "store_enabled": bool(item.store.enabled) if item.store else False,
        "offer_id": item.offer_id,
        "status": item.status,
        "content": loads(item.content_json, {}),
        "attributes": loads(item.attributes_json, []),
        "complex_attributes": loads(item.complex_attributes_json, []),
        "assets": loads(item.assets_json, {}),
        "pricing": loads(item.pricing_json, {}),
        "package": loads(item.package_json, {}),
        "external_task_id": item.external_task_id,
        "external_product_id": item.external_product_id,
        "stock_status": item.stock_status,
        "promotion_status": item.promotion_status,
        "last_error": item.last_error,
        "updated_at": iso(item.updated_at),
    }


def publication_store_summary_dict(item: PublicationStore) -> dict[str, Any]:
    assets=loads(item.assets_json,{})
    preparation=assets.get("ai_preparation") if isinstance(assets,dict) else {}
    return {
        "id":item.id,"store_id":item.store_id,"store_name":item.store.name if item.store else "",
        "store_enabled":bool(item.store.enabled) if item.store else False,"offer_id":item.offer_id,"status":item.status,
        "content":loads(item.content_json,{}),
        "assets":{"primary_image":assets.get("primary_image"),"original_primary_image":assets.get("original_primary_image"),"images":assets.get("images") or [],"personal_review":assets.get("personal_review") or {},"ai_preparation":{"producer":(preparation or {}).get("producer"),"generated_additional_urls":(preparation or {}).get("generated_additional_urls") or [],"image_review_status":(preparation or {}).get("image_review_status"),"image_stage_status":(preparation or {}).get("image_stage_status")}},
        "pricing":loads(item.pricing_json,{}),"package":loads(item.package_json,{}),
        "external_task_id":item.external_task_id,"external_product_id":item.external_product_id,
        "stock_status":item.stock_status,"promotion_status":item.promotion_status,"last_error":item.last_error,"updated_at":iso(item.updated_at),
    }


def publication_dict(item: Publication) -> dict[str, Any]:
    return {
        "id": item.id,
        "batch_id": item.batch_id,
        "product_id": item.product_id,
        "variant_id": item.variant_id,
        "product_name": item.product.name if item.product else "",
        "sku": item.variant.sku if item.variant else (item.product.sku if item.product else ""),
        "status": item.status,
        "requirements": loads(item.requirements_json, {}),
        "image_policy": loads(item.image_policy_json, {}),
        "save_generated_to_catalog": item.save_generated_to_catalog,
        "submit_via_api": item.submit_via_api,
        "product_snapshot": loads(item.product_snapshot_json, {}),
        "job_id": item.job_id,
        "stores": [publication_store_dict(store) for store in item.stores],
        "created_at": iso(item.created_at),
        "updated_at": iso(item.updated_at),
    }


def publication_summary_dict(item: Publication) -> dict[str, Any]:
    snapshot=loads(item.product_snapshot_json,{})
    return {
        "id":item.id,"batch_id":item.batch_id,"product_id":item.product_id,"variant_id":item.variant_id,
        "product_name":item.product.name if item.product else "","sku":item.variant.sku if item.variant else (item.product.sku if item.product else ""),
        "status":item.status,"save_generated_to_catalog":item.save_generated_to_catalog,"submit_via_api":item.submit_via_api,"generation_provider":loads(item.requirements_json,{}).get('generation_provider','model'),
        "product_snapshot":{"assets":snapshot.get("assets") or []},"job_id":item.job_id,
        "stores":[publication_store_summary_dict(store) for store in item.stores],"created_at":iso(item.created_at),"updated_at":iso(item.updated_at),
    }


def research_product_dict(item: ResearchProduct, *, include_raw: bool = False) -> dict[str, Any]:
    value = {
        "id": item.id,
        "source": item.source,
        "source_sku": item.source_sku,
        "title": item.title,
        "category": item.category,
        "ozon_url": item.ozon_url,
        "image_url": item.image_url,
        "image_urls": loads(item.image_urls_json, []),
        "brand": item.brand,
        "selling_price_rub": item.selling_price_rub,
        "sales": item.sales,
        "rating": item.rating,
        "weight_grams": item.weight_grams,
        "status": item.status,
        "description_category_id": item.description_category_id,
        "type_id": item.type_id,
        "category_display": item.category_display,
        "source_file": item.source_file,
        "source_row": item.source_row,
        "updated_at": iso(item.updated_at),
    }
    if include_raw:
        value["raw"] = loads(item.raw_json, {})
    return value


def candidate_dict(item: SourcingCandidate) -> dict[str, Any]:
    raw = loads(item.raw_json, {})
    return {
        "id": item.id,
        "research_product_id": item.research_product_id,
        "research_product": research_product_dict(item.research_product) if item.research_product else None,
        "provider": item.provider,
        "external_product_id": item.external_product_id,
        "external_sku_id": item.external_sku_id,
        "title": item.title,
        "sku_name": item.sku_name,
        "image_url": item.image_url,
        "source_url": item.source_url,
        "unit_price_cny": item.unit_price_cny,
        "domestic_delivery_cny": item.domestic_delivery_cny,
        "landed_cost_cny": item.landed_cost_cny,
        "sales": item.sales,
        "minimum_order_quantity": item.minimum_order_quantity,
        "rank": item.rank,
        "confidence": item.confidence,
        "color": str(raw.get("color") or ""),
        "size": str(raw.get("size") or ""),
        "remark": str(raw.get("remark") or ""),
        "status": item.status,
        "selected": item.selected,
    }


def store_listing_dict(item: StoreListing) -> dict[str, Any]:
    return {
        "id": item.id,
        "store_id": item.store_id,
        "store_name": item.store.name if item.store else "",
        "store_enabled": bool(item.store.enabled) if item.store else False,
        "platform": item.store.platform if item.store else "",
        "offer_id": item.offer_id,
        "status": item.status,
        "external_task_id": item.external_task_id,
        "external_product_id": item.external_product_id,
        "stock_status": item.stock_status,
        "promotion_status": item.promotion_status,
        "content": loads(item.content_json, {}),
        "attributes": loads(item.attributes_json, []),
        "complex_attributes": loads(item.complex_attributes_json, []),
        "assets": loads(item.assets_json, {}),
        "pricing": loads(item.pricing_json, {}),
        "last_error": item.last_error,
        "updated_at": iso(item.updated_at),
    }


def listing_dict(item: Listing) -> dict[str, Any]:
    return {
        "id": item.id,
        "research_product_id": item.research_product_id,
        "catalog_product_id": item.catalog_product_id,
        "base_offer_id": item.base_offer_id,
        "title": item.title,
        "description_ru": item.description_ru,
        "tags": loads(item.tags_json, []),
        "attributes": loads(item.attributes_json, []),
        "complex_attributes": loads(item.complex_attributes_json, []),
        "asset_ids": loads(item.asset_ids_json, []),
        "pricing": loads(item.pricing_json, {}),
        "package": loads(item.package_json, {}),
        "requirements": loads(item.requirements_json, {}),
        "image_policy": loads(item.image_policy_json, {}),
        "save_generated_to_catalog": item.save_generated_to_catalog,
        "publication_mode": item.publication_mode,
        "catalog_snapshot": loads(item.catalog_snapshot_json, {}),
        "status": item.status,
        "blockers": loads(item.blockers_json, []),
        "stores": [store_listing_dict(child) for child in item.store_listings],
        "updated_at": iso(item.updated_at),
    }


def job_dict(item: Job, *, include_events: bool = False) -> dict[str, Any]:
    value = {
        "id": item.id,
        "created_by": item.created_by,
        "target_worker_id": item.target_worker_id,
        "lease_expires_at": iso(item.lease_expires_at),
        "type": item.type,
        "status": item.status,
        "priority": item.priority,
        "progress": item.progress,
        "cancel_requested": item.cancel_requested,
        "pause_requested": item.pause_requested,
        "attempts": item.attempts,
        "max_attempts": item.max_attempts,
        "external_ref": item.external_ref,
        "error_code": item.error_code,
        "error_message": item.error_message,
        "payload": loads(item.payload_json, {}),
        "started_at": iso(item.started_at),
        "finished_at": iso(item.finished_at),
        "created_at": iso(item.created_at),
        "updated_at": iso(item.updated_at),
        "steps": [
            {
                "id": step.id,
                "key": step.key,
                "name": step.name,
                "position": step.position,
                "status": step.status,
                "progress": step.progress,
                "attempts": step.attempts,
                "error_message": step.error_message,
                "details": loads(step.details_json, {}),
                "started_at": iso(step.started_at),
                "finished_at": iso(step.finished_at),
            }
            for step in sorted(item.steps, key=lambda row: row.position)
        ],
    }
    if include_events:
        value["events"] = [
            {
                "id": event.id,
                "level": event.level,
                "message": event.message,
                "data": loads(event.data_json, {}),
                "created_at": iso(event.created_at),
            }
            for event in item.events[-300:]
        ]
    return value
