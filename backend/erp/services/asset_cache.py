"""Bounded, immutable read-through copies; never deletes business originals.

No eviction is performed: callers may retain a returned Path across a response or
job. At capacity downloads fail closed instead of deleting an in-use file. Only
this module's own incomplete download is removed. The inter-process lock covers
quota accounting and publication, including Windows queue processes.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import os
import re
import shutil
import socket
import ssl
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from PIL import Image

from erp.core.config import get_settings
from erp.core.errors import AppError
from erp.core.paths import ASSET_EXTENSIONS, require_writable_path


def _error(message: str, code: str, status: int = 502) -> AppError:
    return AppError(message, code=code, status_code=status)


def _root() -> Path:
    base = get_settings().data_dir.resolve()
    root = base / "asset-cache" / "v1"
    for path in (base / "asset-cache", root):
        if path.is_symlink() or path.resolve() != path:
            raise _error("资产缓存目录不能使用链接或重定向", "asset_cache_path_invalid", 403)
    require_writable_path(root)
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def _cache_lock(root: Path):
    path = root / ".lock"
    if path.is_symlink():
        raise _error("资产缓存锁路径无效", "asset_cache_path_invalid", 403)
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + 35
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise _error("资产缓存正在下载，请稍后重试", "asset_cache_busy", 503)
                time.sleep(.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _validate_url(url: str) -> tuple[str, str]:
    """Exact admin-owned host allowlist plus public IP validation on every hop."""
    try:
        value = urlsplit(url)
        host = (value.hostname or "").lower()
        allowed = {h.strip().lower() for h in get_settings().asset_remote_allowed_hosts}
        if (value.scheme != "https" or not host or value.username or value.password
                or value.port not in {None, 443} or host not in allowed
                or "\\" in url or any(ord(c) < 32 or ord(c) == 127 for c in url)):
            raise ValueError()
        addresses = list(dict.fromkeys(r[4][0] for r in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
        synthetic = (get_settings().asset_allow_synthetic_dns and get_settings().runtime_mode != "server")
        try:
            ipaddress.ip_address(host)
            literal_host = True
        except ValueError:
            literal_host = False
        def permitted(address: str) -> bool:
            value = ipaddress.ip_address(address)
            return value.is_global or (synthetic and not literal_host and value in ipaddress.ip_network("198.18.0.0/15"))
        if not addresses or any(not permitted(address) for address in addresses):
            raise ValueError()
        return host, addresses[0]
    except (ValueError, OSError):
        raise _error("OSS 地址未获允许或解析到非公网地址，请检查资产读取域名配置", "asset_remote_forbidden", 403) from None


@contextmanager
def _response(url: str):
    host, address = _validate_url(url)
    # Pin the checked address for the TCP connection; keep the original hostname
    # for TLS certificate verification and SNI. No second hostname resolution,
    # proxy environment, credentials, or automatic redirects are used.
    connection = http.client.HTTPSConnection(host, timeout=30, context=ssl.create_default_context())
    connection._create_connection = lambda _addr, timeout, source_address=None: socket.create_connection(
        (address, 443), timeout, source_address,
    )
    parsed = urlsplit(url)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    try:
        connection.request("GET", target, headers={"User-Agent": "RussiaERP-AssetReader/1.0", "Accept-Encoding": "identity"})
        response = connection.getresponse()
        try:
            yield response
        finally:
            response.close()
    finally:
        connection.close()


def _download(url: str, target: Path, budget: int, min_free: int) -> None:
    deadline = time.monotonic() + 180
    for _ in range(4):
        with _response(url) as response:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise _error("OSS 重定向缺少地址", "asset_download_failed")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise _error("OSS 文件读取失败，请核对对象和读取权限", "asset_download_failed")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise _error("OSS 文件编码不受支持", "asset_download_failed")
            length = response.getheader("Content-Length")
            try:
                declared = int(length) if length is not None else None
            except ValueError:
                raise _error("OSS 文件长度无效", "asset_download_failed") from None
            if declared is not None and (declared < 0 or declared > budget):
                raise _error("文件超过下载限制或可用缓存容量", "asset_cache_capacity", 507)
            total = 0
            with target.open("xb") as stream:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > budget or shutil.disk_usage(target.parent).free < min_free + len(chunk):
                        raise _error("资产缓存容量或磁盘余量不足", "asset_cache_capacity", 507)
                    if time.monotonic() > deadline:
                        raise _error("OSS 文件下载超时", "asset_download_failed")
                    stream.write(chunk)
            if total == 0 or (declared is not None and declared != total):
                raise _error("OSS 文件下载不完整", "asset_download_incomplete")
            return
    raise _error("OSS 重定向次数过多", "asset_download_failed")


def _verify(path: Path, asset) -> None:
    if asset.size_bytes and path.stat().st_size != asset.size_bytes:
        raise _error("OSS 文件大小与资产记录不一致", "asset_size_mismatch", 409)
    if asset.sha256:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != asset.sha256.lower():
            raise _error("OSS 文件内容校验不一致", "asset_hash_mismatch", 409)
    if str(asset.mime_type or "").startswith("image/"):
        try:
            with Image.open(path) as image:
                image.verify()
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            raise _error("OSS 文件不是有效图片", "asset_image_invalid", 409) from None


def cached_asset_path(asset) -> Path:
    settings = get_settings()
    url = str(asset.oss_url or "")
    suffix = Path(asset.name or "").suffix.lower()
    if suffix not in ASSET_EXTENSIONS:
        suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix not in ASSET_EXTENSIONS:
        raise _error("资产文件类型不受支持", "asset_path_forbidden", 403)
    if asset.sha256 and not re.fullmatch(r"[a-fA-F0-9]{64}", asset.sha256):
        raise _error("资产校验值格式无效", "asset_hash_invalid", 409)
    # Query strings (including signed URLs) never appear in filenames or logs.
    identity = "\0".join((str(asset.id), url, asset.sha256 or "", str(asset.size_bytes)))
    root = _root()
    target = root / (hashlib.sha256(identity.encode()).hexdigest() + suffix)
    with _cache_lock(root):
        if target.is_symlink():
            raise _error("资产缓存文件路径无效", "asset_cache_path_invalid", 403)
        if target.is_file():
            _verify(target, asset)
            return target
        used = sum(p.stat().st_size for p in root.iterdir() if p.is_file() and p.name != ".lock")
        budget = min(settings.asset_download_max_bytes, settings.asset_cache_max_bytes - used,
                     shutil.disk_usage(root).free - settings.asset_cache_min_free_bytes)
        if budget <= 0 or (asset.size_bytes and asset.size_bytes > budget):
            raise _error("资产缓存已满或磁盘余量不足，请调整缓存配置后重试", "asset_cache_capacity", 507)
        temporary = root / (uuid.uuid4().hex + ".part")
        try:
            _download(url, temporary, budget, settings.asset_cache_min_free_bytes)
            _verify(temporary, asset)
            os.replace(temporary, target)
        except AppError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise _error("OSS 文件读取失败，请检查网络与缓存目录", "asset_download_failed") from None
        finally:
            temporary.unlink(missing_ok=True)
        return target
