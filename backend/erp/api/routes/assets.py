from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from erp.core.database import get_db
from erp.core.errors import NotFoundError
from erp.models import Asset
from erp.schemas import AssetRegister
from erp.serializers import asset_dict
from erp.services.assets import register_local_asset, resolve_asset_path
from erp.core.paths import require_asset_path


router = APIRouter(prefix="/assets", tags=["assets"])


@router.get("")
def list_assets(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    kind: str = "",
    session: Session = Depends(get_db),
) -> dict:
    conditions = [Asset.kind == kind] if kind else []
    total = int(session.scalar(select(func.count()).select_from(Asset).where(*conditions)) or 0)
    items = session.scalars(
        select(Asset).where(*conditions).order_by(Asset.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {"items": [asset_dict(item) for item in items], "page": page, "page_size": page_size, "total": total}


@router.post("", status_code=201)
def register_asset(request: AssetRegister, session: Session = Depends(get_db)) -> dict:
    require_asset_path(Path(request.path))
    item = register_local_asset(
        session, request.path, kind=request.kind, source=request.source, read_only=request.read_only,
        calculate_hash=request.calculate_hash, metadata=request.metadata,
    )
    session.commit()
    session.refresh(item)
    return asset_dict(item)


@router.get("/{asset_id}/content")
def asset_content(asset_id: str, session: Session = Depends(get_db)) -> FileResponse:
    item = session.get(Asset, asset_id)
    if not item:
        raise NotFoundError("资产不存在")
    path = resolve_asset_path(item)
    return FileResponse(path, media_type=item.mime_type, filename=item.name)
