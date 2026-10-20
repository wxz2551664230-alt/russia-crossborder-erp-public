from __future__ import annotations

import base64
import hashlib
import json
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import requests


class DajiApiError(RuntimeError):
    pass


class DajiApiLimitError(DajiApiError):
    pass


class DajiClient:
    """Small client for Daji's 1688 sourcing adapter."""

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        api_base: str = "https://openapi.dajisaas.com",
        *,
        timeout: int = 30,
        attempts: int = 3,
    ) -> None:
        self.app_key = str(app_key or "").strip()
        self.app_secret = str(app_secret or "").strip()
        self.api_base = str(api_base or "https://openapi.dajisaas.com").rstrip("/")
        self.timeout = max(5, int(timeout or 30))
        self.attempts = max(1, min(5, int(attempts or 1)))

    def sign(self, params: dict[str, Any]) -> str:
        """Daji signature: sorted key=value pairs, then secret, MD5 uppercase."""
        if not self.app_secret:
            raise DajiApiError("请先填写大吉 API 秘钥")
        parts = []
        for key in sorted(k for k in params if k != "sign"):
            value = params[key]
            if value is None:
                continue
            parts.append(f"{key}={self._encode(value)}")
        parts.append(f"secret={self.app_secret}")
        return hashlib.md5("&".join(parts).encode("utf-8")).hexdigest().upper()

    def upload_image(self, image_path: str | Path) -> str:
        path = Path(image_path)
        if not path.is_file():
            raise DajiApiError(f"找货源图片不存在：{path}")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        fields: dict[str, Any] = {
            "appKey": self.app_key,
            "image_base64": encoded,
        }
        fields["sign"] = self.sign(fields)
        payload = self._request("POST", "/alibaba/upload/image", data=fields)
        image_id = self._data(payload).get("imageId")
        if not image_id:
            raise DajiApiError("大吉图片上传成功，但没有返回 imageId")
        return str(image_id)

    def search_by_image(
        self,
        image_id: str,
        *,
        page: int = 1,
        size: int = 20,
        country: str = "en",
        filters: Iterable[str] = ("isOnePsale",),
        keyword: str = "",
        auxiliary_text: str = "",
    ) -> dict[str, Any]:
        if not str(image_id or "").strip():
            raise DajiApiError("大吉图搜缺少 imageId")
        params: dict[str, Any] = {
            "appKey": self.app_key,
            "imageId": str(image_id).strip(),
            "beginPage": max(1, int(page or 1)),
            "pageSize": max(1, min(50, int(size or 20))),
            "country": str(country or "en").strip() or "en",
        }
        normalized_filters = [str(value).strip() for value in filters if str(value).strip()]
        if normalized_filters:
            params["filter"] = ",".join(normalized_filters)
        if keyword.strip():
            params["keyword"] = keyword.strip()
        if auxiliary_text.strip():
            params["auxiliaryText"] = auxiliary_text.strip()
        params["sign"] = self.sign(params)
        return self._request("GET", "/alibaba/product/imageQuery", params=params)

    def product_detail(self, offer_id: str | int, *, country: str = "en") -> dict[str, Any]:
        params: dict[str, Any] = {
            "appKey": self.app_key,
            "offerId": str(offer_id).strip(),
            "country": str(country or "en").strip() or "en",
        }
        params["sign"] = self.sign(params)
        return self._request("GET", "/alibaba/product/queryProductDetail", params=params)

    def freight_estimate(
        self,
        offer_id: str | int,
        sku_quantities: Iterable[tuple[str | int, int]],
        *,
        province_code: str,
        city_code: str,
        district_code: str,
    ) -> dict[str, Any]:
        items = [
            {"skuId": str(sku_id), "number": max(1, int(quantity))}
            for sku_id, quantity in sku_quantities
            if str(sku_id).strip()
        ]
        if not items:
            raise DajiApiError("查询运费时至少需要一个 SKU")
        if not all(str(value or "").strip() for value in (province_code, city_code, district_code)):
            raise DajiApiError("请先填写国内仓库的省、市、区编码")
        body: dict[str, Any] = {
            "appKey": self.app_key,
            # Daji's published freight schema currently requires appSecret in the
            # JSON body. It is sent only over HTTPS and is never logged.
            "appSecret": self.app_secret,
            "offerId": int(offer_id),
            "toProvinceCode": str(province_code).strip(),
            "toCityCode": str(city_code).strip(),
            "toCountryCode": str(district_code).strip(),
            "totalNum": sum(item["number"] for item in items),
            "logisticsSkuNumModels": items,
        }
        return self._request("POST", "/alibaba/product/freightEstimate", json=body)

    @staticmethod
    def image_search_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = DajiClient._data(payload)
        values = data.get("data", [])
        return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []

    @staticmethod
    def _data(payload: dict[str, Any]) -> dict[str, Any]:
        value = payload.get("data") if isinstance(payload, dict) else None
        return value if isinstance(value, dict) else {}

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self.app_key or not self.app_secret:
            raise DajiApiError("请先填写大吉 AppKey 和秘钥")
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                response = requests.request(
                    method,
                    self.api_base + path,
                    timeout=(10, self.timeout),
                    **kwargs,
                )
                if response.status_code >= 400:
                    detail = ""
                    try:
                        error_payload = self._response_json(response)
                    except (ValueError, UnicodeDecodeError):
                        error_payload = None
                    if isinstance(error_payload, dict):
                        detail = str(
                            error_payload.get("detail") or error_payload.get("message")
                            or error_payload.get("msg") or ""
                        ).strip()
                    if not detail:
                        detail = str(response.text or "").strip()[:500]
                    if response.status_code not in (429,) and response.status_code < 500:
                        error_type = DajiApiLimitError if (
                            "调用次数" in detail or "达到上限" in detail
                            or "call limit" in detail.lower()
                        ) else DajiApiError
                        raise error_type(
                            f"大吉接口请求被拒绝（HTTP {response.status_code}）"
                            + (f"：{detail}" if detail else "")
                        )
                    response.raise_for_status()
                try:
                    payload = self._response_json(response)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise DajiApiError("大吉接口没有返回有效 JSON") from exc
                if not isinstance(payload, dict):
                    raise DajiApiError("大吉接口返回格式不是 JSON 对象")
                code = payload.get("code")
                if code not in (None, 0, 200, "0", "200"):
                    message = str(
                        payload.get("detail") or payload.get("message")
                        or payload.get("msg") or "未知错误"
                    )
                    error_type = DajiApiLimitError if (
                        "调用次数" in message or "达到上限" in message
                        or "call limit" in message.lower()
                    ) else DajiApiError
                    raise error_type(f"大吉接口失败（{code}）：{message}")
                return payload
            except DajiApiError:
                raise
            except requests.RequestException as exc:
                last_error = exc
                status = int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
                retryable = isinstance(exc, (
                    requests.ConnectTimeout,
                    requests.ConnectionError,
                    requests.ReadTimeout,
                )) or status == 429 or status >= 500
                if not retryable or attempt >= self.attempts:
                    break
                time.sleep(min(4, 2 ** (attempt - 1)))
        raise DajiApiError(f"大吉接口请求失败：{last_error}") from last_error

    @staticmethod
    def _response_json(response: requests.Response) -> Any:
        content = getattr(response, "content", None)
        if isinstance(content, bytes):
            return json.loads(content.decode("utf-8-sig"))
        return response.json()

    @staticmethod
    def _encode(value: Any) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        return str(value)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _sku_name(sku: dict[str, Any]) -> str:
    direct = sku.get("skuName") or sku.get("specName") or sku.get("specification")
    if direct:
        return str(direct).strip()
    values: list[str] = []
    attributes = (
        sku.get("skuAttributes") or sku.get("attributes")
        or sku.get("saleAttributes") or sku.get("specAttrs") or []
    )
    if isinstance(attributes, dict):
        attributes = [attributes]
    if isinstance(attributes, list):
        for attr in attributes:
            if not isinstance(attr, dict):
                continue
            name = attr.get("attributeName") or attr.get("propName") or attr.get("name")
            value = attr.get("attributeValue") or attr.get("valueName") or attr.get("value")
            if value:
                values.append(f"{name}:{value}" if name else str(value))
    return ";".join(values)


def normalize_daji_candidates(
    search_payload: dict[str, Any],
    details: dict[str, dict[str, Any]],
    freight_by_sku: dict[tuple[str, str], Any] | None = None,
) -> list[dict[str, Any]]:
    """Convert Daji search/detail results to the existing Newton row contract."""
    freight_by_sku = freight_by_sku or {}
    rows: list[dict[str, Any]] = []
    for offer in DajiClient.image_search_items(search_payload):
        item_id = str(offer.get("offerId") or "").strip()
        if not item_id:
            continue
        detail = details.get(item_id, {})
        data = DajiClient._data(detail)
        skus = data.get("productSkuInfos") or data.get("skuInfos") or data.get("skus") or []
        if not isinstance(skus, list):
            skus = []
        price_info = offer.get("priceInfo") if isinstance(offer.get("priceInfo"), dict) else {}
        offer_price = price_info.get("consignPrice") or price_info.get("jxhyPrice") or price_info.get("price")
        if not skus:
            skus = [{"skuId": "", "price": offer_price}]
        for sku in skus:
            if not isinstance(sku, dict):
                continue
            sku_id = str(sku.get("skuId") or sku.get("id") or sku.get("specId") or "").strip()
            if not sku_id:
                continue
            sku_price = (
                sku.get("consignPrice") or sku.get("promotionPrice") or sku.get("discountPrice")
                or sku.get("price") or offer_price
            )
            price = _decimal(sku_price)
            freight = _decimal(freight_by_sku.get((item_id, sku_id)))
            sku_attributes = sku.get("skuAttributes")
            sku_image = ""
            if isinstance(sku_attributes, list):
                sku_image = next((
                    str(attr.get("skuImageUrl") or "").strip()
                    for attr in sku_attributes if isinstance(attr, dict) and attr.get("skuImageUrl")
                ), "")
            rows.append({
                "itemId": item_id,
                "skuId": sku_id,
                "title": str(data.get("subject") or offer.get("subject") or "").strip(),
                "skuName": _sku_name(sku),
                "imageUrl": str(sku.get("imageUrl") or sku.get("picUrl") or sku_image or offer.get("imageUrl") or "").strip(),
                "company": str(data.get("companyName") or data.get("shopName") or data.get("sellerName") or "").strip(),
                "realtimeSinglePrice": format(price, "f") if price is not None else "",
                "realtimeDeliveryPrice": format(freight, "f") if freight is not None else "",
                "salesCountNum": offer.get("monthSold"),
                "newtonData": {
                    "min_order_qty": 1 if offer.get("isOnePsale") is True else None,
                    "daji_amount_on_sale": sku.get("amountOnSale") or sku.get("quantity"),
                },
                "sourceUrl": f"https://detail.1688.com/offer/{item_id}.html",
                "dajiData": {"search": offer, "sku": sku},
            })
    return rows
