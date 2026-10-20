from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from erp.modules.ozon_follow.legacy.models import ProductCandidate
from erp.modules.ozon_follow.legacy.paths import DATABASE_PATH, ensure_runtime_directories


class ProductRepository:
    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path) if database_path else DATABASE_PATH
        if database_path:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            ensure_runtime_directories()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sku TEXT NOT NULL UNIQUE,
                    source_file TEXT NOT NULL,
                    source_row INTEGER NOT NULL,
                    rank INTEGER,
                    title TEXT NOT NULL,
                    ozon_url TEXT,
                    image_url TEXT,
                    brand TEXT,
                    category TEXT,
                    sale_method TEXT,
                    selling_price_rub REAL,
                    sales INTEGER,
                    revenue_rub REAL,
                    sales_growth REAL,
                    revenue_growth REAL,
                    gross_margin REAL,
                    rating REAL,
                    review_count INTEGER,
                    seller TEXT,
                    seller_type TEXT,
                    delivery_method TEXT,
                    weight_grams REAL,
                    listed_date TEXT,
                    status TEXT NOT NULL DEFAULT '待找货源',
                    raw_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ozon_listing_drafts (
                    source_sku TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    task_id INTEGER,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(source_sku) REFERENCES products(sku)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sourcing_tasks (
                    task_id TEXT PRIMARY KEY,
                    source_sku TEXT NOT NULL,
                    session_id TEXT,
                    status TEXT NOT NULL,
                    next_index INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(source_sku) REFERENCES products(sku)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sourcing_queue (
                    source_sku TEXT PRIMARY KEY,
                    candidate_count INTEGER NOT NULL DEFAULT 10,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(source_sku) REFERENCES products(sku)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS offer_id_sequences (
                    prefix TEXT NOT NULL,
                    date_key TEXT NOT NULL,
                    last_value INTEGER NOT NULL,
                    PRIMARY KEY (prefix, date_key)
                )
                """
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ozon_category_attribute_cache (
                    description_category_id INTEGER NOT NULL, type_id INTEGER NOT NULL,
                    language TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (description_category_id, type_id, language)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ozon_attribute_value_cache (
                    description_category_id INTEGER NOT NULL, type_id INTEGER NOT NULL,
                    attribute_id INTEGER NOT NULL, language TEXT NOT NULL,
                    payload_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (description_category_id, type_id, attribute_id, language)
                )"""
            )
            connection.commit()

    def get_category_attributes_cache(self, category_id: int, type_id: int, language: str) -> list[dict] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json, updated_at FROM ozon_category_attribute_cache WHERE description_category_id=? AND type_id=? AND language=?",
                (int(category_id), int(type_id), str(language).upper()),
            ).fetchone()
        return self._cached_list(row)

    def save_category_attributes_cache(self, category_id: int, type_id: int, language: str, items: list[dict]) -> None:
        self._save_cache("ozon_category_attribute_cache", (int(category_id), int(type_id), str(language).upper()), items)

    def get_attribute_values_cache(self, category_id: int, type_id: int, attribute_id: int, language: str) -> list[dict] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json, updated_at FROM ozon_attribute_value_cache WHERE description_category_id=? AND type_id=? AND attribute_id=? AND language=?",
                (int(category_id), int(type_id), int(attribute_id), str(language).upper()),
            ).fetchone()
        return self._cached_list(row)

    def save_attribute_values_cache(self, category_id: int, type_id: int, attribute_id: int, language: str, items: list[dict]) -> None:
        self._save_cache("ozon_attribute_value_cache", (int(category_id), int(type_id), int(attribute_id), str(language).upper()), items)

    @staticmethod
    def _cached_list(row: sqlite3.Row | None) -> list[dict] | None:
        if not row:
            return None
        try:
            updated_at = datetime.fromisoformat(str(row["updated_at"]))
        except (KeyError, TypeError, ValueError):
            return None
        if updated_at < datetime.now() - timedelta(days=7):
            return None
        try:
            value = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, list) else None

    def _save_cache(self, table: str, keys: tuple, items: list[dict]) -> None:
        placeholders = ",".join("?" for _ in range(len(keys) + 2))
        key_columns = "description_category_id,type_id,language" if len(keys) == 3 else "description_category_id,type_id,attribute_id,language"
        columns = key_columns + ",payload_json,updated_at"
        with closing(self._connect()) as connection:
            connection.execute(
                f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})",
                (*keys, json.dumps(items, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")),
            )
            connection.commit()

    def allocate_offer_id(self, prefix: str = "AUTO", at: datetime | None = None) -> str:
        normalized_prefix = re.sub(r"[^A-Za-z0-9._]+", "", str(prefix or "").strip()).upper() or "AUTO"
        date_key = (at or datetime.now()).strftime("%Y%m%d")
        pattern = re.compile(rf"^{re.escape(normalized_prefix)}-{date_key}-(\d+)$", re.IGNORECASE)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT last_value FROM offer_id_sequences WHERE prefix = ? AND date_key = ?",
                (normalized_prefix, date_key),
            ).fetchone()
            last_value = int(row["last_value"]) if row else 0
            for saved in connection.execute("SELECT payload_json FROM ozon_listing_drafts"):
                try:
                    offer_id = str(json.loads(saved["payload_json"]).get("offer_id") or "")
                except (AttributeError, TypeError, json.JSONDecodeError):
                    continue
                match = pattern.fullmatch(offer_id)
                if match:
                    last_value = max(last_value, int(match.group(1)))
            next_value = last_value + 1
            connection.execute(
                """
                INSERT INTO offer_id_sequences (prefix, date_key, last_value)
                VALUES (?, ?, ?)
                ON CONFLICT(prefix, date_key) DO UPDATE SET last_value = excluded.last_value
                """,
                (normalized_prefix, date_key, next_value),
            )
            connection.commit()
        return f"{normalized_prefix}-{date_key}-{next_value:03d}"

    def upsert_many(self, products: list[ProductCandidate]) -> tuple[int, int]:
        inserted = 0
        updated = 0
        now = datetime.now().isoformat(timespec="seconds")
        sql = """
            INSERT INTO products (
                sku, source_file, source_row, rank, title, ozon_url, image_url,
                brand, category, sale_method, selling_price_rub, sales,
                revenue_rub, sales_growth, revenue_growth, gross_margin, rating,
                review_count, seller, seller_type, delivery_method, weight_grams,
                listed_date, raw_json, updated_at
            ) VALUES (
                :sku, :source_file, :source_row, :rank, :title, :ozon_url,
                :image_url, :brand, :category, :sale_method, :selling_price_rub,
                :sales, :revenue_rub, :sales_growth, :revenue_growth,
                :gross_margin, :rating, :review_count, :seller, :seller_type,
                :delivery_method, :weight_grams, :listed_date, :raw_json,
                :updated_at
            )
            ON CONFLICT(sku) DO UPDATE SET
                source_file=excluded.source_file,
                source_row=excluded.source_row,
                rank=excluded.rank,
                title=excluded.title,
                ozon_url=excluded.ozon_url,
                image_url=excluded.image_url,
                brand=excluded.brand,
                category=excluded.category,
                sale_method=excluded.sale_method,
                selling_price_rub=excluded.selling_price_rub,
                sales=excluded.sales,
                revenue_rub=excluded.revenue_rub,
                sales_growth=excluded.sales_growth,
                revenue_growth=excluded.revenue_growth,
                gross_margin=excluded.gross_margin,
                rating=excluded.rating,
                review_count=excluded.review_count,
                seller=excluded.seller,
                seller_type=excluded.seller_type,
                delivery_method=excluded.delivery_method,
                weight_grams=excluded.weight_grams,
                listed_date=excluded.listed_date,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
        """
        with closing(self._connect()) as connection:
            for product in products:
                existed = connection.execute(
                    "SELECT 1 FROM products WHERE sku = ?", (product.sku,)
                ).fetchone()
                payload = product.to_dict()
                raw_data = payload.pop("raw_data")
                payload["raw_json"] = json.dumps(raw_data, ensure_ascii=False)
                payload["updated_at"] = now
                connection.execute(sql, payload)
                if existed:
                    updated += 1
                else:
                    inserted += 1
            connection.commit()
        return inserted, updated

    def list_products(self, limit: int = 1000) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT sku, rank, title, category, selling_price_rub, sales,
                       seller, status, ozon_url, image_url, weight_grams,
                       rating, review_count, listed_date
                FROM products
                ORDER BY COALESCE(rank, 999999), id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_product_by_sku(self, sku: str) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM products WHERE sku = ? LIMIT 1", (str(sku).strip(),)
            ).fetchone()
        return dict(row) if row else None

    def delete_product(self, sku: str) -> bool:
        """Delete one local product and its dependent local workflow records."""
        normalized = str(sku or "").strip()
        if not normalized:
            raise ValueError("删除商品时SKU不能为空")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM products WHERE sku = ?", (normalized,)
            ).fetchone()
            if not exists:
                connection.rollback()
                return False
            connection.execute("DELETE FROM sourcing_queue WHERE source_sku = ?", (normalized,))
            connection.execute("DELETE FROM sourcing_tasks WHERE source_sku = ?", (normalized,))
            connection.execute("DELETE FROM ozon_listing_drafts WHERE source_sku = ?", (normalized,))
            connection.execute("DELETE FROM products WHERE sku = ?", (normalized,))
            connection.commit()
        return True

    def delete_products(self, skus: list[str]) -> list[str]:
        """Delete multiple local products and their local dependent records atomically."""
        normalized = list(dict.fromkeys(str(sku or "").strip() for sku in skus if str(sku or "").strip()))
        if not normalized:
            raise ValueError("批量删除至少需要选择一个商品")
        placeholders = ",".join("?" for _ in normalized)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = [str(row[0]) for row in connection.execute(
                f"SELECT sku FROM products WHERE sku IN ({placeholders})", normalized
            ).fetchall()]
            if existing:
                existing_placeholders = ",".join("?" for _ in existing)
                connection.execute(
                    f"DELETE FROM sourcing_queue WHERE source_sku IN ({existing_placeholders})", existing
                )
                connection.execute(
                    f"DELETE FROM sourcing_tasks WHERE source_sku IN ({existing_placeholders})", existing
                )
                connection.execute(
                    f"DELETE FROM ozon_listing_drafts WHERE source_sku IN ({existing_placeholders})", existing
                )
                connection.execute(
                    f"DELETE FROM products WHERE sku IN ({existing_placeholders})", existing
                )
            connection.commit()
        return existing

    def save_listing_draft(self, source_sku: str, payload: dict) -> None:
        sku = str(source_sku or "").strip()
        if not sku:
            raise ValueError("上架草稿缺少来源SKU")
        now = datetime.now().isoformat(timespec="seconds")
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO ozon_listing_drafts (
                    source_sku, payload_json, status, task_id, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_sku) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    status=excluded.status,
                    task_id=excluded.task_id,
                    updated_at=excluded.updated_at
                """,
                (
                    sku,
                    json.dumps(payload, ensure_ascii=False),
                    str(payload.get("status") or "DRAFT"),
                    payload.get("task_id"),
                    now,
                ),
            )
            connection.execute(
                "UPDATE products SET status = ?, updated_at = ? WHERE sku = ?",
                (str(payload.get("status") or "DRAFT"), now, sku),
            )
            connection.commit()

    def get_listing_draft(self, source_sku: str) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM ozon_listing_drafts WHERE source_sku = ?",
                (str(source_sku or "").strip(),),
            ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def list_listing_drafts(self) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT source_sku, payload_json FROM ozon_listing_drafts ORDER BY updated_at"
            ).fetchall()
        drafts: list[dict] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                payload.setdefault("source_sku", str(row["source_sku"]))
                drafts.append(payload)
        return drafts

    def save_sourcing_task(self, payload: dict) -> None:
        task_id = str(payload.get("task_id") or "").strip()
        source_sku = str(payload.get("source_sku") or "").strip()
        if not task_id or not source_sku:
            raise ValueError("找货源任务缺少任务 ID 或来源 SKU")
        now = datetime.now().isoformat(timespec="seconds")
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO sourcing_tasks (
                    task_id, source_sku, session_id, status, next_index,
                    payload_json, error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    session_id=excluded.session_id,
                    status=excluded.status,
                    next_index=excluded.next_index,
                    payload_json=excluded.payload_json,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    task_id,
                    source_sku,
                    str(payload.get("session_id") or ""),
                    str(payload.get("status") or "UNKNOWN"),
                    int(payload.get("next_index") or 0),
                    json.dumps(payload, ensure_ascii=False),
                    str(payload.get("error") or ""),
                    now,
                ),
            )
            connection.execute(
                "UPDATE products SET status = ?, updated_at = ? WHERE sku = ?",
                (f'找货源:{payload.get("status") or "UNKNOWN"}', now, source_sku),
            )
            connection.commit()

    def enqueue_sourcing_job(self, source_sku: str, candidate_count: int = 10) -> None:
        sku = str(source_sku or "").strip()
        if not sku:
            raise ValueError("找货源队列缺少来源 SKU")
        count = max(1, min(50, int(candidate_count or 10)))
        now = datetime.now().isoformat(timespec="seconds")
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO sourcing_queue (source_sku, candidate_count, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_sku) DO UPDATE SET
                    candidate_count=excluded.candidate_count,
                    updated_at=excluded.updated_at
                """,
                (sku, count, now, now),
            )
            connection.commit()

    def remove_sourcing_job(self, source_sku: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "DELETE FROM sourcing_queue WHERE source_sku = ?",
                (str(source_sku or "").strip(),),
            )
            connection.commit()

    def list_sourcing_queue(self) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT source_sku, candidate_count, created_at, updated_at
                   FROM sourcing_queue ORDER BY created_at, source_sku"""
            ).fetchall()
        return [dict(row) for row in rows]

    def list_sourcing_tasks(self, limit: int = 200) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT task_id, source_sku, session_id, status, next_index,
                       payload_json, error, updated_at
                FROM sourcing_tasks
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        results: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                payload = json.loads(item.pop("payload_json"))
            except (TypeError, json.JSONDecodeError):
                payload = {}
            item["payload"] = payload if isinstance(payload, dict) else {}
            results.append(item)
        return results

    def get_sourcing_task(self, task_id: str) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM sourcing_tasks WHERE task_id = ?",
                (str(task_id or "").strip(),),
            ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def get_latest_sourcing_task_for_sku(self, source_sku: str) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT payload_json FROM sourcing_tasks
                   WHERE source_sku = ? ORDER BY updated_at DESC, rowid DESC LIMIT 1""",
                (str(source_sku or "").strip(),),
            ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def overview(self) -> dict[str, int]:
        with closing(self._connect()) as connection:
            product_count = int(connection.execute("SELECT COUNT(*) FROM products").fetchone()[0])
            ready_count = int(connection.execute(
                "SELECT COUNT(*) FROM ozon_listing_drafts WHERE status = 'READY'"
            ).fetchone()[0])
            published_count = int(connection.execute(
                "SELECT COUNT(*) FROM ozon_listing_drafts WHERE status = 'COMPLETED'"
            ).fetchone()[0])
            active_sourcing = int(connection.execute(
                """SELECT COUNT(*) FROM sourcing_tasks
                   WHERE status NOT IN ('END', 'ERROR', 'KILL')"""
            ).fetchone()[0])
        return {
            "products": product_count,
            "ready_listings": ready_count,
            "published": published_count,
            "active_sourcing": active_sourcing,
        }
