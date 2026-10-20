from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from erp.modules.ozon_follow.legacy.services.ozon_listing import OzonListingDraft
from erp.modules.ozon_follow.legacy.services.sourcing_selector import SourcingCandidate


class ProductManagerApi:
    """Token-authenticated client for the ECS product manager."""

    def __init__(self, api_url: str, api_token: str, timeout: int = 45, session=None) -> None:
        raw_url = str(api_url or "").strip()
        if not raw_url:
            raise ValueError("请填写ECS产品管理API地址")
        if "://" not in raw_url:
            raw_url = "https://" + raw_url
        parsed = urlsplit(raw_url)
        if not parsed.hostname or parsed.scheme not in {"http", "https"}:
            raise ValueError("ECS产品管理API地址格式错误")
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("远程ECS产品管理API必须使用HTTPS")
        path = parsed.path.rstrip("/")
        if not path.endswith("/api/v1"):
            path += "/api/v1"
        self.base_url = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
        self.api_token = str(api_token or "").strip()
        if not self.api_token:
            raise ValueError("ECS产品管理同步令牌为空")
        self.timeout = (15, max(20, int(timeout)))
        self.session = session or requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=2,
            status=2,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            respect_retry_after_header=True,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def request(self, method: str, path: str, *, payload=None) -> dict[str, Any]:
        url = self.base_url + "/" + str(path or "").lstrip("/")
        try:
            response = self.session.request(
                method.upper(),
                url,
                headers={
                    "Authorization": f"Bearer {self.api_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "OzonAutoListing/1.0",
                },
                json=payload,
                timeout=self.timeout,
            )
        except requests.exceptions.SSLError as exc:
            raise RuntimeError("ECS产品管理HTTPS证书校验失败") from exc
        except requests.exceptions.Timeout as exc:
            raise RuntimeError("连接ECS产品管理超时；同步可以安全重试") from exc
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeError("无法连接ECS产品管理，请检查网络和ECS安全组") from exc
        try:
            data = response.json()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            preview = re.sub(r"\s+", " ", str(response.text or "")).strip()[:500]
            raise RuntimeError(
                f"ECS产品管理API返回的不是JSON（HTTP {response.status_code}）：{preview or '空响应'}"
            ) from exc
        if response.status_code >= 400 or not isinstance(data, dict) or data.get("ok") is False:
            error = data.get("error") if isinstance(data, dict) else None
            if isinstance(error, dict):
                detail = error.get("message") or error.get("code")
            else:
                detail = error
            raise RuntimeError(
                f"ECS产品管理API返回HTTP {response.status_code}：{detail or '请求失败'}"
            )
        return data

    def shops(self) -> list[dict[str, Any]]:
        data = self.request("GET", "shops/")
        return [item for item in (data.get("shops") or []) if isinstance(item, dict)]

    def upsert_product(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self.request("POST", "products/upsert/", payload=payload)
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("ECS产品管理API未返回产品同步结果")
        return result


def build_product_manager_payload(
    draft: OzonListingDraft,
    source: SourcingCandidate,
    shop_id: int,
) -> dict[str, Any]:
    raw_spec_model = source.raw.get("skuName")
    if not isinstance(raw_spec_model, str) or not raw_spec_model.strip():
        raise ValueError("1688选中SKU缺少准确的原始规格名称skuName，不能同步产品库")
    if not source.item_id or not source.sku_id:
        raise ValueError("1688选中货源缺少商品ID或SKU ID")
    if source.unit_price is None:
        raise ValueError("1688选中货源缺少准确的SKU拿货价")
    try:
        # ECS uses landed sourcing cost as its purchase price: the selected SKU
        # price plus domestic freight entered during manual sourcing review.
        purchase_price = (
            Decimal(source.unit_price) + Decimal(source.delivery_price or 0)
        ).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("1688选中货源的SKU拿货价无效") from exc
    return {
        "shop_id": int(shop_id),
        # ECS keeps one source/product record, independent of the Ozon stores.
        # Store prefixes only exist in Ozon-facing publication copies.
        "sku": (draft.base_offer_id or draft.offer_id).strip(),
        "chinese_name": str(source.raw.get("title") or source.title or "").strip(),
        "source_url": draft.selected_1688_url.strip() or source.offer_url,
        "purchase_price": format(purchase_price, "f"),
        # This must remain byte-for-byte equal to Newton's selected 1688 skuName.
        "spec_model": raw_spec_model,
        "color": draft.product_manager_color.strip(),
        "size": draft.product_manager_size.strip(),
        "image_url": draft.primary_image.strip(),
        "remark": draft.product_manager_remark.strip(),
    }
