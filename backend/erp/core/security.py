from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from erp.core.config import get_settings
from erp.core.errors import AppError


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi_protect(data: bytes) -> bytes:
    source, source_buffer = _blob(data)
    target = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), "RussiaCrossborderERP", None, None, None, 0, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)
        del source_buffer


def _dpapi_unprotect(data: bytes) -> bytes:
    source, source_buffer = _blob(data)
    target = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)
        del source_buffer


class SecretVault:
    """Encrypts bundles with a data key protected by DPAPI on Windows.

    Cloud deployments provide ERP_SECRET_KEY as a Fernet key and never depend on
    a machine-local key file.
    """

    def __init__(self, key_path: Path | None = None, explicit_key: str | bytes | None = None) -> None:
        self.key_path = key_path or (get_settings().data_dir / "master.key.dpapi")
        self.explicit_key = explicit_key

    def _key(self) -> bytes:
        configured = self.explicit_key or get_settings().secret_key
        if configured:
            return configured.encode("ascii") if isinstance(configured, str) else configured
        if os.name != "nt":
            raise AppError(
                "非 Windows 环境必须设置 ERP_SECRET_KEY",
                code="secret_key_missing",
                status_code=503,
            )
        if self.key_path.is_file():
            try:
                return _dpapi_unprotect(base64.b64decode(self.key_path.read_bytes(), validate=True))
            except (OSError, ValueError) as error:
                raise AppError("无法读取本机加密主密钥", code="secret_key_invalid", status_code=503) from error
        key = Fernet.generate_key()
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.key_path.with_suffix(".tmp")
        temporary.write_bytes(base64.b64encode(_dpapi_protect(key)))
        os.replace(temporary, self.key_path)
        return key

    def encrypt(self, values: dict[str, Any] | None) -> str:
        payload = json.dumps(values or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return "fernet:" + Fernet(self._key()).encrypt(payload).decode("ascii")

    def decrypt(self, value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            raw = value.removeprefix("fernet:").encode("ascii")
            payload = json.loads(Fernet(self._key()).decrypt(raw).decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise AppError("加密配置无法读取", code="secret_decryption_failed", status_code=503) from error
        if not isinstance(payload, dict):
            raise AppError("加密配置格式无效", code="secret_payload_invalid", status_code=503)
        return payload

    @staticmethod
    def merge(existing: dict[str, Any], updates: dict[str, Any] | None) -> dict[str, Any]:
        merged = dict(existing)
        for key, value in (updates or {}).items():
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            merged[str(key)] = value
        return merged

    @staticmethod
    def public_status(values: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            key: {
                "configured": bool(value),
                "masked": f"••••{str(value)[-4:]}" if value else "",
            }
            for key, value in values.items()
        }


vault = SecretVault()

