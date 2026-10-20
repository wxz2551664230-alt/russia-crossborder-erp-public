from __future__ import annotations

import hashlib
import ipaddress
import socket
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image, ImageOps


MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
SYNTHETIC_DNS_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def validate_public_url(url: str, *, allow_synthetic_dns: bool = False) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("图片地址必须是公开的 HTTP 或 HTTPS URL")
    try:
        literal_host = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal_host = None
    if literal_host is not None and not literal_host.is_global:
        raise ValueError("不允许访问本机、内网或保留地址")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    except socket.gaierror as error:
        raise ValueError("图片域名无法解析") from error
    def allowed(value: str) -> bool:
        address = ipaddress.ip_address(value)
        return address.is_global or (allow_synthetic_dns and literal_host is None and address in SYNTHETIC_DNS_NETWORK)
    if not addresses or any(not allowed(value) for value in addresses):
        raise ValueError("不允许访问本机、内网或保留地址")
    return parsed.geturl()


def download_image(url: str, *, allow_synthetic_dns: bool = False) -> tuple[bytes, str]:
    current = validate_public_url(url, allow_synthetic_dns=allow_synthetic_dns)
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for _ in range(4):
            with client.stream("GET", current, headers={"User-Agent": "RussiaERP-Worker/1.0"}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("图片重定向缺少目标地址")
                    current = validate_public_url(urljoin(current, location), allow_synthetic_dns=allow_synthetic_dns)
                    continue
                response.raise_for_status()
                declared = int(response.headers.get("content-length") or 0)
                if declared > MAX_DOWNLOAD_BYTES:
                    raise ValueError("图片文件超过 20 MB 限制")
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_DOWNLOAD_BYTES:
                        raise ValueError("图片文件超过 20 MB 限制")
                return bytes(data), current
    raise ValueError("图片重定向次数过多")


def optimize_image(data: bytes, *, max_dimension: int = 2000, quality: int = 85, output_format: str = "JPEG") -> tuple[bytes, dict]:
    max_dimension = max(320, min(int(max_dimension), 4096))
    quality = max(60, min(int(quality), 95))
    output_format = str(output_format).upper()
    if output_format not in {"JPEG", "WEBP"}:
        raise ValueError("输出格式只支持 JPEG 或 WEBP")
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    with Image.open(BytesIO(data)) as source:
        source.verify()
    with Image.open(BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source)
        original_format = source.format or "unknown"
        original_size = image.size
        image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        if output_format == "JPEG":
            if image.mode in {"RGBA", "LA"}:
                background = Image.new("RGB", image.size, "white")
                background.paste(image, mask=image.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
        output = BytesIO()
        image.save(output, format=output_format, quality=quality, optimize=True)
        content = output.getvalue()
        details = {
            "original_format": original_format,
            "original_width": original_size[0],
            "original_height": original_size[1],
            "width": image.width,
            "height": image.height,
            "output_format": output_format.lower(),
        }
        return content, details


def execute_image_prepare(job_id: str, payload: dict, data_dir: Path, *, allow_synthetic_dns: bool = False) -> dict:
    source, resolved_url = download_image(
        str(payload.get("source_url") or ""), allow_synthetic_dns=allow_synthetic_dns,
    )
    output, details = optimize_image(
        source,
        max_dimension=int(payload.get("max_dimension") or 2000),
        quality=int(payload.get("quality") or 85),
        output_format=str(payload.get("output_format") or "JPEG"),
    )
    suffix = ".webp" if details["output_format"] == "webp" else ".jpg"
    target_dir = data_dir / "processed" / job_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"optimized{suffix}"
    target.write_bytes(output)
    return {
        "ok": True,
        **details,
        "source_url": resolved_url,
        "source_bytes": len(source),
        "output_bytes": len(output),
        "sha256": hashlib.sha256(output).hexdigest(),
        "local_relative_path": target.relative_to(data_dir).as_posix(),
    }
