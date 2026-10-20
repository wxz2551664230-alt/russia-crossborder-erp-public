from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from erp.core.errors import AppError, NotFoundError
from erp.core.jsonutil import dumps
from erp.models import Asset
from erp.core.paths import require_asset_path


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_image(path: Path) -> tuple[int | None, int | None]:
    try:
        with Image.open(path) as image:
            return image.width, image.height
    except (OSError, ValueError):
        return None, None


def register_local_asset(
    session: Session,
    path_value: str | Path,
    *,
    kind: str = "file",
    source: str = "legacy",
    read_only: bool = True,
    calculate_hash: bool = False,
    metadata: dict[str, Any] | None = None,
    legacy_source: str | None = None,
    legacy_id: str | None = None,
) -> Asset:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise NotFoundError(f"文件不存在：{path}")
    if legacy_source and legacy_id:
        existing = session.scalar(
            select(Asset).where(Asset.legacy_source == legacy_source, Asset.legacy_id == legacy_id)
        )
        if existing:
            return existing
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    width, height = inspect_image(path) if mime_type.startswith("image/") else (None, None)
    item = Asset(
        kind=kind,
        name=path.name,
        source=source,
        local_path=str(path),
        sha256=sha256_file(path) if calculate_hash else "",
        mime_type=mime_type,
        size_bytes=path.stat().st_size,
        width=width,
        height=height,
        read_only=read_only,
        metadata_json=dumps(metadata or {}),
        legacy_source=legacy_source,
        legacy_id=legacy_id,
    )
    session.add(item)
    session.flush()
    return item


def resolve_asset_path(asset: Asset) -> Path:
    if asset.mirror_status == "deleted":
        raise AppError("该资产已删除", code="asset_deleted", status_code=410)
    if asset.local_path:
        try:
            path = require_asset_path(Path(asset.local_path))
        except AppError:
            # A migrated Windows path is not a valid path on another computer.
            # Never open it outside the allowed roots; resolve the OSS copy.
            if not asset.oss_url:
                raise
        else:
            if path.is_file():
                return path
    if asset.oss_url:
        from erp.services.asset_cache import cached_asset_path
        return cached_asset_path(asset)
    if not asset.local_path:
        raise AppError("该资产没有可读取的副本", code="asset_not_local", status_code=404)
    raise AppError("资产对应的文件已不存在且没有 OSS 副本", code="asset_file_missing", status_code=410)
