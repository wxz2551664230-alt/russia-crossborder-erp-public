"""One-time, fail-closed POD order snapshot migration. Old files are read only.

Run with --execute only before ERP receives new orders. Subsequent execution
returns the receipt, never overwrites ERP order changes with legacy records.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from erp.core.config import get_settings
from erp.core.security import vault


def bind_stores(source: Path, destination: Path, session) -> dict:
    """Verify account IDs/tokens in memory; never identify an account by name alone."""
    from sqlalchemy import select
    from erp.models import Store
    old_cipher = Fernet((source / 'data' / 'master.key').read_bytes().strip())
    config = json.loads(old_cipher.decrypt((source / 'data' / 'settings.enc').read_bytes()))
    old_stores = list(config.get('order_stores') or [])
    if config.get('wildberries_token'):
        old_stores.append({'id': 'legacy-wb', 'platform': 'wb', 'token': config['wildberries_token']})
    if config.get('ozon_client_id'):
        old_stores.append({'id': 'legacy-ozon', 'platform': 'ozon', 'client_id': config['ozon_client_id']})
    bound, unresolved = [], []
    with closing(sqlite3.connect(destination / 'orders.db')) as db:
        db.execute('CREATE TABLE IF NOT EXISTS erp_order_store_bindings (platform TEXT NOT NULL, legacy_store_id TEXT NOT NULL, erp_store_id TEXT NOT NULL, PRIMARY KEY(platform, legacy_store_id))')
        for platform, old_id in db.execute('SELECT DISTINCT platform, store_id FROM ozon_orders').fetchall():
            if session.get(Store, old_id):
                continue
            candidates = []
            originals = [row for row in old_stores if row.get('id') == old_id]
            for store in session.scalars(select(Store)):
                normalized = 'wb' if store.platform in {'wb', 'wildberries'} else store.platform
                if normalized != platform:
                    continue
                secrets = vault.decrypt(store.connection.secrets_encrypted) if store.connection else {}
                secrets.update(vault.decrypt(store.secrets_encrypted))
                if platform == 'ozon':
                    account = str(secrets.get('client_id') or store.external_account_id or '')
                    matched = bool(account) and any(str(row.get('client_id') or '') == account for row in originals)
                else:
                    token = secrets.get('token') or secrets.get('api_key') or secrets.get('wildberries_token')
                    matched = bool(token) and any(row.get('token') == token for row in originals)
                if matched:
                    candidates.append(store)
            if len(candidates) != 1:
                unresolved.append({'platform': platform, 'legacy_store_id': old_id})
                continue
            store = candidates[0]
            db.execute('INSERT OR IGNORE INTO erp_order_store_bindings VALUES (?,?,?)', (platform, old_id, store.id))
            db.execute('UPDATE ozon_orders SET store_id=?,store_name=? WHERE platform=? AND store_id=?', (store.id, store.name, platform, old_id))
            # Preserve joins used by the original order detail cost summaries.
            for table in ('ozon_finance_operations', 'ozon_ad_costs'):
                if db.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (table,)).fetchone():
                    db.execute(f'UPDATE {table} SET store_id=? WHERE store_id=?', (store.id, old_id))
            bound.append({'platform': platform, 'store_name': store.name})
        for old_id, erp_id in db.execute('SELECT legacy_store_id, erp_store_id FROM erp_order_store_bindings').fetchall():
            for table in ('ozon_finance_operations', 'ozon_ad_costs'):
                if db.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (table,)).fetchone():
                    db.execute(f'UPDATE {table} SET store_id=? WHERE store_id=?', (erp_id, old_id))
        db.commit()
    return {'bound': bound, 'unresolved': unresolved}


def migrate(source: Path, destination: Path, *, execute: bool = False, target_cipher=None) -> dict:
    source = source.resolve()
    destination = destination.resolve()
    if destination == source or destination.is_relative_to(source):
        raise ValueError("迁移目标不能位于旧程序目录")
    receipt = destination / "migration-receipt.json"
    if receipt.exists():
        return {**json.loads(receipt.read_text(encoding="utf-8")), "already_imported": True}
    source_db = source / "data" / "orders.db"
    if not source_db.is_file():
        raise ValueError("旧订单数据库不存在")
    with closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as original:
        tables = [row[0] for row in original.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        def quoted(name):
            return '"' + name.replace('"', '""') + '"'
        counts = {name: original.execute(f"SELECT COUNT(*) FROM {quoted(name)}").fetchone()[0] for name in tables}
        report = {"source": str(source_db), "counts": counts, "executed": False,
                  "purchase_worker_enabled": False, "migrated_at": datetime.now(timezone.utc).isoformat()}
        if not execute:
            return report
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / "orders.db"
        if target.exists():
            with closing(sqlite3.connect(target)) as current:
                for name in tables:
                    exists = current.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
                    if exists and current.execute(f"SELECT COUNT(*) FROM {quoted(name)}").fetchone()[0]:
                        raise ValueError("ERP 已有订单数据；禁止覆盖，请执行专用增量迁移")
                with closing(sqlite3.connect(destination / f"before-migration-{datetime.now():%Y%m%d-%H%M%S}.db")) as backup:
                    current.backup(backup)
        old_cipher = Fernet((source / "data" / "master.key").read_bytes().strip())
        cipher = target_cipher or Fernet(vault._key())
        staging = destination / "orders.migrating.db"
        if staging.exists():
            raise ValueError("发现未完成的迁移副本，先检查，不自动覆盖")
        with closing(sqlite3.connect(staging)) as copied:
            original.backup(copied)
            for table, column in (("ozon_orders", "recipient_enc"), ("batch_order_groups", "address_enc")):
                if table not in tables:
                    continue
                for rowid, value in copied.execute(f"SELECT rowid, {column} FROM {table}").fetchall():
                    if value:
                        plain = old_cipher.decrypt(value.encode() if isinstance(value, str) else bytes(value))
                        json.loads(plain)  # Abort on invalid credentials/data, never silently discard an address.
                        copied.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (cipher.encrypt(plain), rowid))
            copied.execute("CREATE TABLE IF NOT EXISTS erp_order_migration (source TEXT NOT NULL, legacy_table TEXT NOT NULL, legacy_id TEXT NOT NULL, PRIMARY KEY(source, legacy_table, legacy_id))")
            for table in ("ozon_orders", "batch_order_groups"):
                if table in tables:
                    field = "posting_number" if table == "ozon_orders" else "id"
                    copied.execute(f"INSERT OR IGNORE INTO erp_order_migration SELECT ?, ?, {field} FROM {table}", (str(source_db), table))
            copied.commit()
            actual = {name: copied.execute(f"SELECT COUNT(*) FROM {quoted(name)}").fetchone()[0] for name in tables}
            if actual != counts or copied.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("迁移数量或数据库完整性校验失败")
            copied.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            copied.execute("PRAGMA journal_mode=DELETE")
        # Use SQLite backup into the destination to cooperate with any existing WAL.
        with closing(sqlite3.connect(staging)) as copied, closing(sqlite3.connect(target)) as current:
            copied.backup(current)
        report["executed"] = True
        report["verified_counts"] = actual
        receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    report = migrate(settings.legacy_composer_root, settings.data_dir / "orders" / "pod", execute=args.execute)
    if args.execute:
        from erp.core.database import SessionLocal
        with SessionLocal() as session:
            report['store_binding'] = bind_stores(settings.legacy_composer_root, settings.data_dir / 'orders' / 'pod', session)
    print(json.dumps(report, ensure_ascii=False, indent=2))
