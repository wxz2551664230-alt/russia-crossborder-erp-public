from __future__ import annotations

import base64
import ctypes
import json
import os
import sqlite3
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import select

from erp.core.config import get_settings
from erp.core.jsonutil import dumps
from erp.core.security import vault
from erp.models import (
    Asset,
    CatalogProduct,
    CatalogVariant,
    IntegrationConnection,
    Listing,
    ResearchProduct,
    Store,
    StoreListing,
)
from erp.services.jobs import JobContext, job_handler


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _legacy_dpapi(value: str) -> str:
    if not value:
        return ""
    encoded = value
    if value.startswith("dpapi-machine:"):
        encoded = value.removeprefix("dpapi-machine:")
    elif value.startswith("dpapi:"):
        encoded = value.removeprefix("dpapi:")
    try:
        encrypted = base64.b64decode(encoded, validate=True)
    except ValueError:
        return value
    if os.name != "nt":
        raise RuntimeError("旧配置只能在原 Windows 电脑解密")
    source, source_buffer = _blob(encrypted)
    target = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0x01, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)
        del source_buffer


def _readonly_sqlite(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def inspect_legacy_sources() -> dict[str, Any]:
    settings = get_settings()
    sources = {
        "ozon_listing": settings.legacy_ozon_listing_root,
        "ozon_automation": settings.legacy_automation_root,
        "hicustom_composer": settings.legacy_composer_root,
        "ai_studio": settings.legacy_ai_root,
    }
    result: dict[str, Any] = {}
    for key, root in sources.items():
        files = []
        for relative in (
            "data/products.db", "data/product_library.db", "data/orders.db",
            "data/config.local.json", "data/settings.enc", "user_settings.json",
            "queue_history.json", "配置文件/pipeline_config.json",
        ):
            path = root / relative
            if path.is_file():
                files.append({"path": str(path), "size_bytes": path.stat().st_size})
        result[key] = {"root": str(root), "available": root.is_dir(), "files": files}
    return result


def _upsert_connection(
    context: JobContext,
    *,
    provider: str,
    name: str,
    base_url: str = "",
    config: dict[str, Any] | None = None,
    secrets: dict[str, Any] | None = None,
    legacy_source: str,
    legacy_id: str,
) -> IntegrationConnection:
    session = context.session
    item = session.scalar(select(IntegrationConnection).where(
        IntegrationConnection.legacy_source == legacy_source,
        IntegrationConnection.legacy_id == legacy_id,
    ))
    if not item:
        item = IntegrationConnection(
            provider=provider,
            name=name,
            legacy_source=legacy_source,
            legacy_id=legacy_id,
        )
        session.add(item)
    item.base_url = base_url
    item.config_json = dumps(config or {})
    item.secrets_encrypted = vault.encrypt(secrets or {})
    item.enabled = True
    session.flush()
    return item


def _upsert_store(
    context: JobContext,
    *,
    platform: str,
    name: str,
    external_id: str | None,
    offer_prefix: str = "",
    config: dict[str, Any] | None = None,
    secrets: dict[str, Any] | None = None,
    legacy_source: str,
    legacy_id: str,
) -> Store:
    session = context.session
    item = session.scalar(select(Store).where(Store.legacy_source == legacy_source, Store.legacy_id == legacy_id))
    if not item and external_id:
        item = session.scalar(select(Store).where(Store.platform == platform, Store.external_account_id == external_id))
    if not item:
        item = Store(platform=platform, name=name, legacy_source=legacy_source, legacy_id=legacy_id)
        session.add(item)
    item.external_account_id = external_id or None
    item.offer_prefix = offer_prefix
    item.currency = str((config or {}).get("currency") or "RUB")
    item.config_json = dumps(config or {})
    item.secrets_encrypted = vault.encrypt(secrets or {})
    item.enabled = bool((config or {}).get("enabled", True))
    session.flush()
    return item


def _migrate_ozon_listing_config(context: JobContext) -> dict[str, int]:
    path = get_settings().legacy_ozon_listing_root / "data" / "config.local.json"
    if not path.is_file():
        return {"connections": 0, "stores": 0}
    payload = json.loads(path.read_text(encoding="utf-8"))
    secret_fields = {
        "elim_api_key", "daji_app_secret", "alibaba_app_secret", "alibaba_access_token",
        "ozon_api_key", "ozon_secondary_api_key", "ozon_stores_json", "ai_api_key",
        "vision_api_key", "image_api_key", "oss_access_key_id", "oss_access_key_secret",
        "product_manager_api_token",
    }
    for key in secret_fields:
        if payload.get(key):
            payload[key] = _legacy_dpapi(str(payload[key]))
    connection_specs = [
        ("daji", "大吉 1688", "daji_api_base", {"app_key": payload.get("daji_app_key"), "app_secret": payload.get("daji_app_secret")}),
        ("elim", "Elim 1688", "elim_api_base", {"api_key": payload.get("elim_api_key")}),
        ("alibaba", "1688 开放平台", "", {"app_key": payload.get("alibaba_app_key"), "app_secret": payload.get("alibaba_app_secret"), "access_token": payload.get("alibaba_access_token")}),
        ("ai_chat", "OZON 文案模型", "ai_api_url", {"api_key": payload.get("ai_api_key")}),
        ("ai_image", "OZON 生图模型", "image_api_url", {"api_key": payload.get("image_api_key")}),
        ("oss", "阿里云 OSS", "oss_endpoint", {"access_key_id": payload.get("oss_access_key_id"), "access_key_secret": payload.get("oss_access_key_secret")}),
        ("hicustom", "ECS 产品库", "product_manager_api_url", {"token": payload.get("product_manager_api_token")}),
    ]
    count = 0
    for provider, name, base_key, secrets in connection_specs:
        if any(secrets.values()) or payload.get(base_key):
            non_secret = {}
            if provider == "oss":
                non_secret = {
                    "region": payload.get("oss_region"), "bucket": payload.get("oss_bucket"),
                    "public_base_url": payload.get("oss_public_base_url"), "prefix": payload.get("oss_object_prefix"),
                }
            if provider in {"ai_chat", "ai_image"}:
                non_secret["model"] = payload.get("ai_model" if provider == "ai_chat" else "image_model")
            _upsert_connection(
                context, provider=provider, name=name, base_url=str(payload.get(base_key) or ""),
                config=non_secret, secrets={key: value for key, value in secrets.items() if value},
                legacy_source="ozon_listing", legacy_id=provider,
            )
            count += 1
    raw_stores: list[dict[str, Any]] = []
    if payload.get("ozon_stores_json"):
        try:
            decoded = json.loads(str(payload["ozon_stores_json"]))
            if isinstance(decoded, list):
                raw_stores = [item for item in decoded if isinstance(item, dict)]
        except json.JSONDecodeError:
            pass
    if not raw_stores and payload.get("ozon_client_id"):
        raw_stores.append({
            "name": payload.get("ozon_store_name") or "OZON 默认店铺",
            "client_id": payload.get("ozon_client_id"), "api_key": payload.get("ozon_api_key"),
            "api_base": payload.get("ozon_api_base"), "prefix": payload.get("ozon_store_prefix"),
        })
    if payload.get("ozon_secondary_client_id"):
        raw_stores.append({
            "name": payload.get("ozon_secondary_store_name") or "OZON 第二店铺",
            "client_id": payload.get("ozon_secondary_client_id"), "api_key": payload.get("ozon_secondary_api_key"),
            "api_base": payload.get("ozon_secondary_api_base"), "prefix": payload.get("ozon_secondary_store_prefix"),
        })
    for index, store in enumerate(raw_stores, 1):
        client_id = str(store.get("client_id") or store.get("clientId") or "")
        _upsert_store(
            context, platform="ozon", name=str(store.get("name") or f"OZON 店铺 {index}"),
            external_id=client_id or None,
            offer_prefix=str(store.get("prefix") or store.get("offer_prefix") or ""),
            config={"api_base": store.get("api_base") or payload.get("ozon_api_base") or "https://api-seller.ozon.ru", "enabled": store.get("enabled", True)},
            secrets={"client_id": client_id, "api_key": store.get("api_key") or store.get("apiKey") or ""},
            legacy_source="ozon_listing", legacy_id=str(store.get("id") or client_id or index),
        )
    context.session.commit()
    return {"connections": count, "stores": len(raw_stores)}


def _migrate_composer_config(context: JobContext) -> dict[str, int]:
    root = get_settings().legacy_composer_root / "data"
    vault_path = root / "settings.enc"
    key_path = root / "master.key"
    if not vault_path.is_file() or not key_path.is_file():
        return {"connections": 0, "stores": 0}
    payload = json.loads(Fernet(key_path.read_bytes().strip()).decrypt(vault_path.read_bytes()).decode("utf-8"))
    specs = [
        ("hicustom", "HICUSTOM", "hicustom_base_url", {"app_key": payload.get("hicustom_app_key"), "app_secret": payload.get("hicustom_app_secret")}),
        ("s2b", "S2B", "s2b_base_url", {"app_key": payload.get("s2b_app_key"), "app_secret": payload.get("s2b_app_secret")}),
        ("sds", "SDS", "sds_base_url", {"app_id": payload.get("sds_app_id"), "app_secret": payload.get("sds_app_secret")}),
        ("tms", "TMS", "tms_base_url", {"app_key": payload.get("tms_app_key"), "app_secret": payload.get("tms_app_secret")}),
        ("oss", "阿里云 OSS（产品库）", "oss_endpoint", {"access_key_id": payload.get("oss_access_key_id"), "access_key_secret": payload.get("oss_access_key_secret")}),
        ("ai_chat", "产品文案模型", "copy_api_host", {"api_key": payload.get("copy_api_token")}),
    ]
    count = 0
    for provider, name, base_key, secrets in specs:
        if any(secrets.values()) or payload.get(base_key):
            _upsert_connection(
                context, provider=provider, name=name, base_url=str(payload.get(base_key) or ""),
                config={}, secrets={key: value for key, value in secrets.items() if value},
                legacy_source="hicustom_composer", legacy_id=provider,
            )
            count += 1
    stores = [item for item in payload.get("order_stores") or [] if isinstance(item, dict)]
    for index, store in enumerate(stores, 1):
        platform = "wildberries" if str(store.get("platform") or "").lower() == "wb" else "ozon"
        external = str(store.get("client_id") or "") or None
        secrets = (
            {"client_id": store.get("client_id") or "", "api_key": store.get("api_key") or "", "performance_token": store.get("performance_token") or ""}
            if platform == "ozon" else {"token": store.get("token") or ""}
        )
        _upsert_store(
            context, platform=platform, name=str(store.get("name") or f"{platform} 店铺 {index}"),
            external_id=external, config={"enabled": store.get("enabled", True)}, secrets=secrets,
            legacy_source="hicustom_composer", legacy_id=str(store.get("id") or index),
        )
    context.session.commit()
    return {"connections": count, "stores": len(stores)}


def _migrate_research_products(context: JobContext) -> int:
    path = get_settings().legacy_ozon_listing_root / "data" / "products.db"
    if not path.is_file():
        return 0
    with _readonly_sqlite(path) as source:
        rows = source.execute("SELECT * FROM products ORDER BY id").fetchall()
        for index, row in enumerate(rows, 1):
            legacy_id = str(row["id"])
            item = context.session.scalar(select(ResearchProduct).where(
                ResearchProduct.legacy_source == "ozon_listing",
                ResearchProduct.legacy_id == legacy_id,
            ))
            if not item:
                item = ResearchProduct(
                    source="seerfar", source_sku=str(row["sku"]),
                    legacy_source="ozon_listing", legacy_id=legacy_id,
                )
                context.session.add(item)
            raw = json.loads(row["raw_json"] or "{}")
            images = raw.get("_ozon_image_urls") if isinstance(raw, dict) else []
            item.title = str(row["title"] or "")
            item.category = str(row["category"] or "")
            item.ozon_url = str(row["ozon_url"] or "")
            item.image_url = str(row["image_url"] or "")
            item.image_urls_json = dumps(images if isinstance(images, list) else [])
            item.brand = str(row["brand"] or "")
            item.selling_price_rub = row["selling_price_rub"]
            item.sales = row["sales"]
            item.rating = row["rating"]
            item.weight_grams = row["weight_grams"]
            item.status = str(row["status"] or "pending_sourcing")
            item.source_file = str(row["source_file"] or "")
            item.source_row = row["source_row"]
            item.raw_json = row["raw_json"] or "{}"
            if index % 250 == 0:
                context.session.commit()
                context.set_progress("research_products", index * 100 / max(1, len(rows)), f"已迁移 {index}/{len(rows)} 个选品商品")
        context.session.commit()
    return len(rows)


def _migrate_catalog(context: JobContext) -> dict[str, int]:
    path = get_settings().legacy_composer_root / "data" / "product_library.db"
    if not path.is_file():
        return {"products": 0, "variants": 0, "assets": 0}
    counts = {"products": 0, "variants": 0, "assets": 0}
    product_map: dict[str, str] = {}
    with _readonly_sqlite(path) as source:
        products = source.execute("SELECT * FROM products ORDER BY created_at").fetchall()
        for row in products:
            legacy_id = str(row["id"])
            item = context.session.scalar(select(CatalogProduct).where(
                CatalogProduct.legacy_source == "hicustom_composer", CatalogProduct.legacy_id == legacy_id,
            ))
            if not item:
                item = CatalogProduct(sku=str(row["sku"]), legacy_source="hicustom_composer", legacy_id=legacy_id)
                context.session.add(item)
            item.name = str(row["name"] or "")
            item.category = str(row["category_id"] or "")
            item.status = str(row["status"] or "draft")
            item.tags_json = row["tags_json"] or "[]"
            context.session.flush()
            product_map[legacy_id] = item.id
            counts["products"] += 1
        variants = source.execute("SELECT * FROM product_variants ORDER BY created_at").fetchall()
        for row in variants:
            parent_id = product_map.get(str(row["product_id"]))
            if not parent_id:
                continue
            legacy_id = str(row["id"])
            item = context.session.scalar(select(CatalogVariant).where(
                CatalogVariant.legacy_source == "hicustom_composer", CatalogVariant.legacy_id == legacy_id,
            ))
            if not item:
                item = CatalogVariant(
                    product_id=parent_id, sku=str(row["sku"]),
                    legacy_source="hicustom_composer", legacy_id=legacy_id,
                )
                context.session.add(item)
            item.color = str(row["color_name"] or "")
            item.size = str(row["size_name"] or "")
            item.weight_grams = float(row["weight"] or 0) * 1000 if row["weight"] else None
            item.properties_json = dumps({"stock_item_id": row["stock_item_id"], "prices": json.loads(row["prices_json"] or "{}")})
            counts["variants"] += 1
        assets = source.execute("SELECT * FROM product_oss_assets ORDER BY uploaded_at").fetchall()
        for row in assets:
            legacy_id = str(row["id"])
            item = context.session.scalar(select(Asset).where(
                Asset.legacy_source == "hicustom_composer", Asset.legacy_id == legacy_id,
            ))
            if not item:
                item = Asset(
                    name=Path(str(row["local_path"] or row["oss_key"] or legacy_id)).name,
                    legacy_source="hicustom_composer", legacy_id=legacy_id,
                )
                context.session.add(item)
            item.kind = str(row["asset_type"] or "product_asset")
            item.source = "legacy"
            item.local_path = str(row["local_path"] or "")
            item.oss_bucket = str(row["oss_bucket"] or "")
            item.oss_key = str(row["oss_key"] or "")
            item.oss_url = str(row["oss_url"] or "")
            item.sha256 = str(row["sha256"] or "")
            item.mime_type = str(row["content_type"] or "application/octet-stream")
            item.size_bytes = int(row["size"] or 0)
            item.read_only = True
            item.mirror_status = "mirrored" if item.oss_key else "not_requested"
            counts["assets"] += 1
        context.session.commit()
    return counts


def _source_counts() -> dict[str, Any]:
    settings = get_settings()
    result: dict[str, Any] = {"sources": inspect_legacy_sources()}
    databases = {
        "ozon_listing": settings.legacy_ozon_listing_root / "data" / "products.db",
        "catalog": settings.legacy_composer_root / "data" / "product_library.db",
        "orders": settings.legacy_composer_root / "data" / "orders.db",
    }
    for key, path in databases.items():
        if not path.is_file():
            continue
        with _readonly_sqlite(path) as connection:
            tables = [str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )]
            result[key] = {
                table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in tables
            }
    return result


@job_handler("legacy_migration")
def legacy_migration_job(context: JobContext) -> None:
    dry_run = bool(context.payload.get("dry_run", True))
    context.start_step("inspect")
    report = _source_counts()
    context.complete_step("inspect", details=report)
    if dry_run:
        context.event("迁移预演完成，没有写入历史业务数据")
        for key in ("connections", "research_products", "catalog"):
            context.complete_step(key, details={"skipped": "dry_run"})
        return
    database_path = get_settings().data_dir / "erp.db"
    if database_path.is_file():
        backup_path = get_settings().data_dir / "backups" / f"erp-before-migration-{datetime.now():%Y%m%d-%H%M%S}.db"
        with sqlite3.connect(database_path) as source, sqlite3.connect(backup_path) as target:
            source.backup(target)
        context.event("已创建迁移前数据库备份", data={"path": str(backup_path)})
    context.start_step("connections")
    config_result = {
        "ozon_listing": _migrate_ozon_listing_config(context),
        "hicustom_composer": _migrate_composer_config(context),
    }
    context.complete_step("connections", details=config_result)
    context.start_step("research_products")
    research_count = _migrate_research_products(context)
    context.complete_step("research_products", details={"imported": research_count})
    context.start_step("catalog")
    catalog_counts = _migrate_catalog(context)
    context.complete_step("catalog", details=catalog_counts)
    context.event("第一阶段历史数据迁移完成", data={"research_products": research_count, **catalog_counts})
