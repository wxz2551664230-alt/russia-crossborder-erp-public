from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ERP_",
        env_file=PROJECT_ROOT / ".env",
        extra="ignore",
    )

    app_name: str = "俄罗斯跨境 ERP"
    environment: str = "local"
    runtime_mode: Literal["local", "server", "worker"] = "local"
    api_prefix: str = "/api/v1"
    host: str = "127.0.0.1"
    port: int = 8788
    data_dir: Path = PROJECT_ROOT / "data"
    database_url: str = ""
    frontend_dist: Path = PROJECT_ROOT / "frontend" / "dist"
    secret_key: str = Field(default="", repr=False)
    access_token: str = Field(default="", repr=False)
    multiuser_enabled: bool = False
    web_ui_profile: Literal["full", "shared-products-pilot"] = "full"
    session_cookie_secure: bool = False
    asset_roots: list[Path] = Field(default_factory=list)
    asset_remote_allowed_hosts: list[str] = Field(default_factory=list)
    asset_allow_synthetic_dns: bool = False
    asset_cache_max_bytes: int = Field(default=1024 * 1024 * 1024, ge=1)
    asset_download_max_bytes: int = Field(default=128 * 1024 * 1024, ge=1)
    asset_cache_min_free_bytes: int = Field(default=512 * 1024 * 1024, ge=0)
    worker_poll_seconds: float = 0.75
    worker_lease_seconds: int = 45
    control_plane_url: str = ""
    worker_id: str = ""
    worker_token: str = Field(default="", repr=False)
    # Server-side SHA-256 verifiers keyed by device ID, not reusable clear tokens.
    worker_token_hashes: dict[str, str] = Field(default_factory=dict, repr=False)
    worker_require_device_identity: bool = False
    worker_allow_synthetic_dns: bool = False
    worker_oss_region: str = ""
    worker_oss_endpoint: str = ""
    worker_oss_bucket: str = ""
    worker_oss_public_base_url: str = ""
    worker_oss_prefix: str = "erp-worker"
    worker_oss_access_key_id: str = Field(default="", repr=False)
    worker_oss_access_key_secret: str = Field(default="", repr=False)
    legacy_ozon_listing_root: Path = PROJECT_ROOT.parent / "ozon自动选品上架"
    legacy_automation_root: Path = PROJECT_ROOT.parent / "OZON自动化工作文件夹 - 2"
    legacy_composer_root: Path = PROJECT_ROOT.parent / "指纹科技图片合成"
    legacy_ai_root: Path = PROJECT_ROOT.parent / "AI自动生图"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        path = (self.data_dir / "erp.db").resolve()
        return f"sqlite:///{path.as_posix()}"

    @property
    def runs_api(self) -> bool:
        return self.runtime_mode in {"local", "server"}

    @property
    def runs_local_workers(self) -> bool:
        return self.runtime_mode in {"local", "worker"}

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "imports").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "backups").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "logs").mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
