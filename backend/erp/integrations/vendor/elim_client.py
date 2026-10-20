from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import requests


class ElimApiError(RuntimeError):
    pass


class ElimApiLimitError(ElimApiError):
    pass


class ElimClient:
    """Small client for Elim Product API v2."""

    def __init__(self, api_key: str, api_base: str = "https://openapi.elim.asia", *, timeout: int = 45) -> None:
        self.api_key = str(api_key or "").strip()
        self.api_base = str(api_base or "https://openapi.elim.asia").rstrip("/")
        self.timeout = timeout

    @property
    def headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "x-api-key": self.api_key}

    def upload_image(self, image_path: str | Path) -> str:
        path = Path(image_path)
        with path.open("rb") as handle:
            payload = self._request(
                "/v2/products/upload-image",
                data={"platform": "alibaba"},
                files={"file": (path.name, handle, "image/jpeg")},
            )
        data = self._unwrap(payload)
        image_id = data.get("imageId") or data.get("image_id")
        if not image_id:
            raise ElimApiError("Elim 上传成功，但没有返回 imageId")
        return str(image_id)

    def search_by_image(self, image_id: str, *, size: int = 10) -> dict[str, Any]:
        return self._request(
            "/v2/products/search-by-image",
            json={
                "platform": "alibaba",
                "imgId": str(image_id),
                "page": 1,
                "size": max(1, min(20, int(size))),
            },
        )

    def product_detail(self, offer_id: str) -> dict[str, Any]:
        return self._request(
            "/v2/products/detail",
            json={"platform": "alibaba", "id": str(offer_id)},
        )

    @staticmethod
    def items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = ElimClient._unwrap(payload)
        values = data.get("items", [])
        return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []

    @staticmethod
    def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
        value: Any = payload
        for _ in range(2):
            if not isinstance(value, dict):
                return {}
            nested = value.get("data")
            if not isinstance(nested, dict):
                break
            value = nested
        return value if isinstance(value, dict) else {}

    def _request(self, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self.api_key:
            raise ElimApiError("请先在系统设置中填写 Elim API Key")
        response = requests.post(
            self.api_base + path,
            headers=self.headers,
            timeout=(10, self.timeout),
            **kwargs,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ElimApiError(f"Elim 没有返回有效 JSON（HTTP {response.status_code}）") from exc
        if response.status_code >= 400:
            message = _error_message(payload) or str(response.text or "").strip()[:500]
            error_type = ElimApiLimitError if response.status_code in {400, 429} and any(
                word in message.lower() for word in ("quota", "credit", "rate limit", "limit", "配额", "积分", "次数")
            ) else ElimApiError
            raise error_type(f"Elim 接口请求失败（HTTP {response.status_code}）" + (f"：{message}" if message else ""))
        if not isinstance(payload, dict):
            raise ElimApiError("Elim 返回格式不是 JSON 对象")
        return payload


def _error_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    value = payload.get("detail") or payload.get("message") or payload.get("error")
    if isinstance(value, dict):
        value = value.get("message") or value.get("detail")
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _sku_name(sku: dict[str, Any]) -> str:
    direct = sku.get("name") or sku.get("sku_name") or sku.get("spec_name")
    if direct:
        return str(direct).strip()
    props = sku.get("properties") or sku.get("attributes") or sku.get("props") or []
    if isinstance(props, dict):
        props = [props]
    values: list[str] = []
    for prop in props if isinstance(props, list) else []:
        if isinstance(prop, dict):
            name = prop.get("name") or prop.get("property_name") or ""
            value = prop.get("value") or prop.get("value_name") or ""
            if value:
                values.append(f"{name}:{value}" if name else str(value))
    return ";".join(values)


def normalize_elim_candidates(
    search_payload: dict[str, Any], details: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for offer in ElimClient.items(search_payload):
        item_id = str(offer.get("id") or offer.get("offer_id") or offer.get("offerId") or "").strip()
        if not item_id:
            continue
        has_detail = item_id in details
        detail = ElimClient._unwrap(details.get(item_id, {})) or offer
        skus = detail.get("skus") or detail.get("sku_list") or []
        if not isinstance(skus, list):
            skus = []
        if not skus:
            skus = [{"id": item_id, "price": detail.get("dropship_price") or detail.get("retail_price") or detail.get("price")}]
        extra = detail.get("extra_info") if isinstance(detail.get("extra_info"), dict) else {}
        dropship_supported = (
            extra.get("isOnePsale") is True
            or detail.get("is_one_psale") is True
            or detail.get("dropship_price") not in (None, "")
            or detail.get("retail_price") not in (None, "")
        )
        if not dropship_supported:
            continue
        for sku in skus:
            if not isinstance(sku, dict):
                continue
            sku_id = str(sku.get("id") or sku.get("sku_id") or sku.get("spec_id") or "").strip()
            if not sku_id:
                continue
            price = _decimal(
                sku.get("dropship_price") or sku.get("promotion_price") or sku.get("price")
                or detail.get("dropship_price") or detail.get("retail_price")
                or detail.get("promotion_price") or detail.get("price")
            )
            images = detail.get("img_urls") or offer.get("img_urls") or []
            image = (
                sku.get("image") or sku.get("img_url") or sku.get("image_url")
                or detail.get("img_url") or detail.get("image_url")
                or offer.get("img_url") or offer.get("image_url")
                or (images[0] if isinstance(images, list) and images else "")
            )
            rows.append({
                "itemId": item_id,
                "skuId": sku_id,
                "title": str(detail.get("title") or offer.get("title") or "").strip(),
                "skuName": _sku_name(sku),
                "imageUrl": str(image or "").strip(),
                "company": str(detail.get("shop_name") or detail.get("seller_name") or offer.get("shop_name") or offer.get("seller_name") or "").strip(),
                "realtimeSinglePrice": format(price, "f") if price is not None else "",
                "realtimeDeliveryPrice": "",
                "salesCountNum": detail.get("sold") or detail.get("sales_volume") or offer.get("sold") or offer.get("sales_volume"),
                "newtonData": {"min_order_qty": 1, "elim_needs_detail": not has_detail},
                "sourceUrl": str(detail.get("original_product_url") or detail.get("link") or offer.get("link") or f"https://detail.1688.com/offer/{item_id}.html"),
                "elimData": {"search": offer, "sku": sku},
            })
    return rows
