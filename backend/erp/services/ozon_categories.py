from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from erp.core.config import get_settings
from erp.modules.ozon_follow.legacy.services.ozon_listing import flatten_ozon_categories


def _snapshot_path(store_id: str, language: str = "ZH_HANS") -> Path:
    safe_language = "".join(ch for ch in language.upper() if ch.isalnum() or ch == "_") or "ZH_HANS"
    return get_settings().data_dir / "ozon_categories" / f"{store_id}-{safe_language}.json"


def save_category_snapshot(store_id: str, store_name: str, language: str, tree: list[dict[str, Any]]) -> dict[str, Any]:
    options = sorted(flatten_ozon_categories(tree), key=lambda row: row["display"])
    payload = {
        "store_id": store_id,
        "store_name": store_name,
        "language": language,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "root_count": len(tree),
        "option_count": len(options),
        "tree": tree,
        "options": options,
    }
    path = _snapshot_path(store_id, language)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {key: value for key, value in payload.items() if key != "tree"}


def load_category_snapshot(store_id: str, language: str = "ZH_HANS") -> dict[str, Any] | None:
    path = _snapshot_path(store_id, language)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def find_category_option(store_id: str, category_id: int, type_id: int, language: str = "ZH_HANS") -> dict[str, Any] | None:
    snapshot = load_category_snapshot(store_id, language)
    if not snapshot:
        return None
    return next((
        item for item in snapshot.get("options", [])
        if int(item.get("description_category_id") or 0) == int(category_id)
        and int(item.get("type_id") or 0) == int(type_id)
    ), None)


def _options_path(store_id, kind, params):
    import hashlib
    key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    return get_settings().data_dir / 'ozon_categories' / f'{store_id}-{kind}-{key}.json'


def load_options_cache(store_id, kind, params):
    if kind not in {'category_attributes', 'warehouses', 'promotions'}: return None
    try:
        data = json.loads(_options_path(store_id, kind, params).read_text(encoding='utf-8'))
        if kind in {'warehouses', 'promotions'}:
            import time
            if not isinstance(data, dict) or time.time()-data.get('saved_at', 0)>300:
                return None
            data=data.get('items')
        return data if isinstance(data, list) else None
    except (OSError, ValueError): return None


def save_options_cache(store_id, kind, params, items):
    if kind not in {'category_attributes', 'warehouses', 'promotions'}: return
    path = _options_path(store_id, kind, params)
    path.parent.mkdir(parents=True, exist_ok=True)
    from uuid import uuid4
    temporary = path.with_suffix('.' + uuid4().hex + '.tmp')
    try:
        import time
        value={'saved_at':time.time(),'items':items} if kind in {'warehouses','promotions'} else items
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, path)
    finally: temporary.unlink(missing_ok=True)
