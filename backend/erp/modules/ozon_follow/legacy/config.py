from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from ctypes import wintypes

from erp.modules.ozon_follow.legacy.paths import CONFIG_PATH, ensure_runtime_directories


@dataclass(slots=True)
class AppConfig:
    sourcing_provider: str = "daji"
    elim_api_key: str = ""
    elim_api_base: str = "https://openapi.elim.asia"
    daji_enabled: str = "0"
    daji_app_key: str = ""
    daji_app_secret: str = ""
    daji_api_base: str = "https://openapi.dajisaas.com"
    daji_country: str = "en"
    daji_freight_province_code: str = ""
    daji_freight_city_code: str = ""
    daji_freight_district_code: str = ""
    alibaba_app_key: str = ""
    alibaba_app_secret: str = ""
    alibaba_access_token: str = ""
    ozon_client_id: str = ""
    ozon_api_key: str = ""
    ozon_api_base: str = "https://api-seller.ozon.ru"
    multi_store_enabled: str = "0"
    ozon_store_name: str = ""
    ozon_store_prefix: str = ""
    ozon_secondary_client_id: str = ""
    ozon_secondary_api_key: str = ""
    ozon_secondary_api_base: str = "https://api-seller.ozon.ru"
    ozon_secondary_store_name: str = ""
    ozon_secondary_store_prefix: str = ""
    # JSON array of arbitrary Ozon stores. The whole value is DPAPI protected
    # because it contains per-store API keys. Legacy fields above are retained
    # for automatic migration and old desktop clients.
    ozon_stores_json: str = ""
    ai_api_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    ai_models: tuple[str, ...] = ()
    vision_api_url: str = ""
    vision_api_key: str = ""
    vision_model: str = ""
    vision_models: tuple[str, ...] = ()
    image_api_url: str = ""
    image_api_key: str = ""
    image_model: str = "gpt-image-1"
    oss_region: str = "cn-hangzhou"
    oss_endpoint: str = "oss-cn-hangzhou.aliyuncs.com"
    oss_bucket: str = ""
    oss_public_base_url: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_object_prefix: str = "ozon-auto-listing"
    target_cost_profit_margin: str = "30"
    pricing_sticker_fee: str = "2.5"
    pricing_operating_cost: str = "3"
    pricing_return_rate: str = "10"
    pricing_logistics_service_rate: str = "2"
    pricing_price_multiplier: str = "3"
    pricing_old_price_multiplier: str = "3.7"
    pricing_category_module: str = ""
    pricing_product_category: str = ""
    product_manager_api_url: str = "https://products.example.com/api/v1"
    product_manager_api_token: str = ""
    product_manager_enabled: str = "0"
    product_manager_shop_id: str = ""
    product_manager_shop_name: str = ""


SECRET_FIELDS = {
    "elim_api_key",
    "daji_app_secret",
    "alibaba_app_secret",
    "alibaba_access_token",
    "ozon_api_key",
    "ozon_secondary_api_key",
    "ozon_stores_json",
    "ai_api_key",
    "vision_api_key",
    "image_api_key",
    "oss_access_key_id",
    "oss_access_key_secret",
    "product_manager_api_token",
}

_DPAPI_USER_PREFIX = "dpapi:"
_DPAPI_MACHINE_PREFIX = "dpapi-machine:"
_CRYPTPROTECT_LOCAL_MACHINE = 0x4


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob_from_bytes(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def _protect_secret(value: str) -> str:
    if not value or value.startswith((_DPAPI_USER_PREFIX, _DPAPI_MACHINE_PREFIX)):
        return value
    if os.name != "nt":
        raise RuntimeError("当前系统不支持 Windows DPAPI 凭据加密")
    source, source_buffer = _blob_from_bytes(value.encode("utf-8"))
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "OzonAutoListing",
        None,
        None,
        None,
        _CRYPTPROTECT_LOCAL_MACHINE,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(output.pbData, output.cbData)
        return _DPAPI_MACHINE_PREFIX + base64.b64encode(encrypted).decode("ascii")
    finally:
        kernel32.LocalFree(output.pbData)
        del source_buffer


def _unprotect_secret(value: str) -> str:
    if not value:
        return value
    if value.startswith(_DPAPI_MACHINE_PREFIX):
        prefix = _DPAPI_MACHINE_PREFIX
    elif value.startswith(_DPAPI_USER_PREFIX):
        prefix = _DPAPI_USER_PREFIX
    else:
        return value
    if os.name != "nt":
        raise RuntimeError("当前系统不支持 Windows DPAPI 凭据解密")
    try:
        encrypted = base64.b64decode(value.removeprefix(prefix), validate=True)
    except ValueError:
        raise RuntimeError("本机配置中的加密凭据格式无效") from None
    source, source_buffer = _blob_from_bytes(encrypted)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(output),
    ):
        error = ctypes.WinError()
        raise RuntimeError(
            "无法解密本机凭据；配置可能来自其他电脑或旧的 Windows 账户"
        ) from error
    try:
        decrypted = ctypes.string_at(output.pbData, output.cbData)
        return decrypted.decode("utf-8")
    finally:
        kernel32.LocalFree(output.pbData)
        del source_buffer


class ConfigStore:
    """Stores settings locally and protects secrets with Windows DPAPI."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else CONFIG_PATH
        self.backup_path = self.path.with_name(self.path.stem + ".backup" + self.path.suffix)

    @staticmethod
    def _read_payload(path: Path) -> dict[str, object]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("配置文件内容不是JSON对象")
        return payload

    @staticmethod
    def _config_from_payload(payload: dict[str, object]) -> AppConfig:
        defaults = AppConfig()
        allowed = AppConfig.__dataclass_fields__.keys()
        values = {key: payload.get(key, getattr(defaults, key)) for key in allowed}
        for key in SECRET_FIELDS:
            values[key] = _unprotect_secret(str(values.get(key, "")))
        return AppConfig(**values)

    def load(self) -> AppConfig:
        ensure_runtime_directories()
        if not self.path.exists() and not self.backup_path.exists():
            return AppConfig()
        errors: list[str] = []
        for candidate in (self.path, self.backup_path):
            if not candidate.exists():
                continue
            try:
                return self._config_from_payload(self._read_payload(candidate))
            except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
                errors.append(f"{candidate.name}: {exc}")
        raise RuntimeError(
            "本机配置文件和自动备份都无法读取；程序没有清空任何令牌。"
            + (" ".join(errors) if errors else "")
        )

    def save(self, config: AppConfig) -> None:
        ensure_runtime_directories()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(config)
        for key in SECRET_FIELDS:
            payload[key] = _protect_secret(str(payload.get(key, "")))
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        # Validate the complete serialized payload before touching the live file.
        try:
            parsed = json.loads(encoded)
        except json.JSONDecodeError as exc:  # pragma: no cover - json.dumps is deterministic
            raise RuntimeError("新配置序列化失败，原配置未改动") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("新配置格式无效，原配置未改动")

        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if self.path.exists():
                # Never replace a known-good backup with a damaged live file.
                try:
                    self._read_payload(self.path)
                except (OSError, ValueError, RuntimeError):
                    pass
                else:
                    shutil.copy2(self.path, self.backup_path)
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()
