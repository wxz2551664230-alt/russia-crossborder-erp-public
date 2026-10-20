from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from erp.core.config import get_settings
from erp.core.errors import NotFoundError


UI_FILES = ("index.html", "styles.css", "app.js")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def archive_root() -> Path:
    return (get_settings().data_dir / "pod" / "ui-archive").resolve()


def ensure_ui_archive() -> dict[str, Any]:
    """Create an ERP-owned immutable snapshot without changing the source program."""
    settings = get_settings()
    source_root = (settings.legacy_composer_root / "web").resolve()
    target_root = archive_root()
    target_root.mkdir(parents=True, exist_ok=True)
    manifest_path = target_root / "manifest.json"
    if not manifest_path.is_file():
        missing = [name for name in UI_FILES if not (source_root / name).is_file()]
        if missing:
            raise NotFoundError("原版 POD 工作台文件不完整", details={"missing": missing})
        initial_assets: list[dict[str, Any]] = []
        for name in UI_FILES:
            source = source_root / name
            target = target_root / name
            source_hash = _sha256(source)
            temporary = target.with_suffix(f"{target.suffix}.{os.getpid()}.tmp")
            shutil.copy2(source, temporary)
            if _sha256(temporary) != source_hash:
                temporary.unlink(missing_ok=True)
                raise RuntimeError(f"POD 工作台归档校验失败：{name}")
            os.replace(temporary, target)
            initial_assets.append({"name": name, "archived_sha256": source_hash, "size": target.stat().st_size})
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_root": str(source_root),
            "source_read_only": True,
            "retention": "permanent",
            "assets": initial_assets,
        }
        temporary = manifest_path.with_suffix(f".json.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, manifest_path)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {str(item["name"]): str(item["archived_sha256"]) for item in manifest.get("assets", [])}
    assets: list[dict[str, Any]] = []
    for name in UI_FILES:
        target = target_root / name
        archived_hash = _sha256(target) if target.is_file() else ""
        source = source_root / name
        source_hash = _sha256(source) if source.is_file() else ""
        assets.append({
            "name": name,
            "archived_sha256": archived_hash,
            "size": target.stat().st_size if target.is_file() else 0,
            "integrity_ok": bool(archived_hash and archived_hash == expected.get(name)),
            "source_available": source.is_file(),
            "matches_source": bool(source_hash and archived_hash == source_hash),
        })
    return {
        "root": str(target_root),
        "manifest": str(manifest_path),
        "source_root": str(source_root),
        "source_read_only": True,
        "assets": assets,
        "ready": all(item["integrity_ok"] for item in assets),
    }


def archived_ui_file(name: str) -> Path:
    if name not in UI_FILES:
        raise NotFoundError("POD 工作台文件不存在")
    ensure_ui_archive()
    path = archive_root() / name
    if not path.is_file():
        raise NotFoundError("POD 工作台归档文件不存在")
    return path
