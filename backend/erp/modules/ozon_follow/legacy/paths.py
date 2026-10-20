from __future__ import annotations

import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[5]
ERP_DATA_ROOT = Path(os.environ.get("ERP_DATA_DIR") or (ROOT_DIR / "data"))
DATA_DIR = ERP_DATA_ROOT / "ozon_follow"
LOG_DIR = ERP_DATA_ROOT / "logs"
DATABASE_PATH = DATA_DIR / "products.db"
CONFIG_PATH = DATA_DIR / "config.local.json"


def ensure_runtime_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
