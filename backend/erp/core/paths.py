from pathlib import Path

from erp.core.config import PROJECT_ROOT, get_settings
from erp.core.errors import AppError

ASSET_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif", ".xlsx", ".xls", ".csv", ".tsv", ".pdf", ".zip"}


def legacy_roots() -> list[Path]:
    s = get_settings()
    return [p.resolve() for p in (s.legacy_composer_root, s.legacy_ozon_listing_root, s.legacy_automation_root, s.legacy_ai_root)]


def require_writable_path(path: Path) -> Path:
    resolved = path.resolve()
    if any(resolved.is_relative_to(root) for root in legacy_roots()):
        raise AppError("旧程序目录永久只读，请将工作资料复制到 ERP 数据目录", code="legacy_source_read_only", status_code=403)
    return resolved


def require_asset_path(path: Path) -> Path:
    s = get_settings()
    resolved = path.resolve()
    roots = [s.data_dir.resolve(), *(p.resolve() for p in s.asset_roots)]
    if s.runtime_mode == "local":
        roots += legacy_roots() + [(PROJECT_ROOT / "outputs").resolve(), (PROJECT_ROOT / "reports").resolve()]
    if resolved.suffix.lower() not in ASSET_EXTENSIONS or not any(resolved.is_relative_to(root) for root in roots):
        raise AppError("该文件不在允许的资产目录或文件类型内", code="asset_path_forbidden", status_code=403)
    return resolved
