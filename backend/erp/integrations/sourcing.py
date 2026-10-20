from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

import requests

from erp.integrations.vendor.daji_client import DajiClient, normalize_daji_candidates
from erp.integrations.vendor.elim_client import ElimClient, normalize_elim_candidates


class SourcingAdapter:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.provider = str(config.get("provider") or "")
        self.secrets = config.get("secrets") or {}

    def search(self, image_path: Path, *, count: int = 10) -> list[dict[str, Any]]:
        if self.provider == "daji":
            client = DajiClient(
                str(self.secrets.get("app_key") or self.config.get("app_key") or ""),
                str(self.secrets.get("app_secret") or ""),
                str(self.config.get("base_url") or "https://openapi.dajisaas.com"),
            )
            image_id = client.upload_image(image_path)
            search_payload = client.search_by_image(image_id, size=count)
            details: dict[str, dict[str, Any]] = {}
            for item in client.image_search_items(search_payload)[:count]:
                offer_id = str(item.get("offerId") or "")
                if offer_id:
                    details[offer_id] = client.product_detail(offer_id)
            return normalize_daji_candidates(search_payload, details)[:count]
        if self.provider == "elim":
            client = ElimClient(
                str(self.secrets.get("api_key") or ""),
                str(self.config.get("base_url") or "https://openapi.elim.asia"),
            )
            image_id = client.upload_image(image_path)
            search_payload = client.search_by_image(image_id, size=count)
            details: dict[str, dict[str, Any]] = {}
            for item in client.items(search_payload)[:count]:
                offer_id = str(item.get("id") or item.get("offerId") or "")
                if offer_id:
                    try:
                        details[offer_id] = client.product_detail(offer_id)
                    except Exception:
                        continue
            return normalize_elim_candidates(search_payload, details)[:count]
        raise ValueError(f"暂不支持找源接口：{self.provider}")


def download_sourcing_image(url: str, target: Path) -> Path:
    if not url.startswith("https://"):
        raise ValueError("找源商品缺少 HTTPS 主图")
    response = requests.get(url, timeout=(15, 60))
    response.raise_for_status()
    if not str(response.headers.get("Content-Type") or "").lower().startswith("image/"):
        raise ValueError("找源主图响应不是图片")
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = mimetypes.guess_extension(str(response.headers.get("Content-Type")).split(";", 1)[0]) or ".jpg"
    output = target.with_suffix(suffix)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(response.content)
    temporary.replace(output)
    return output

