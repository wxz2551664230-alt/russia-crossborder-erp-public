"""Back up existing SQLite databases before upgrading the main schema."""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from erp.core.config import PROJECT_ROOT, get_settings


def backup_local_databases() -> Path:
    settings = get_settings()
    destination = settings.data_dir / "backups" / datetime.now().strftime("pre-upgrade-%Y%m%d-%H%M%S-%f")
    destination.mkdir(parents=True)
    sources = [settings.data_dir / "orders/pod/orders.db", settings.data_dir / "ozon_follow/products.db", settings.data_dir / "ai-usage.sqlite3"]
    url = make_url(settings.resolved_database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        sources.insert(0, Path(url.database))
    manifest = []
    for index, source in enumerate(sources):
        if not source.is_file():
            continue
        target = destination / f"{index}-{source.name}"
        with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as reader, sqlite3.connect(target) as writer:
            reader.backup(writer)
            if writer.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("数据库备份完整性验证失败，停止迁移")
        manifest.append({"source": str(source), "backup": target.name})
    key = settings.data_dir / "master.key.dpapi"
    if key.is_file():
        shutil.copy2(key, destination / key.name)
    (destination / "manifest.json").write_text(json.dumps({"databases": manifest, "note": "SQLite 一致性备份；跨库业务切换须停止写入并另行对账。DPAPI 密钥只能由原 Windows 用户解密。环境主密钥须独立保管。"}, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination


def main():
    settings = get_settings()
    if make_url(settings.resolved_database_url).get_backend_name() == "sqlite":
        backup_local_databases()
    command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")


if __name__ == "__main__":
    main()
