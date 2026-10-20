from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from erp.core.errors import AppError
from erp.domains.pod.integrations.common import SupplierConnectionConfig


def _country_code(value: Any) -> str:
    country = str(value or "").strip()
    return "CN" if country.casefold() in {"cn", "china", "中国", "中华人民共和国"} else country


class S2BClient:
    provider = "s2b"
    provider_name = "S2B 货盘"

    def __init__(self, connection: SupplierConnectionConfig, *, transport: httpx.BaseTransport | None = None) -> None:
        self.app_key = str(connection.secrets.get("app_key") or "").strip()
        self.app_secret = str(connection.secrets.get("app_secret") or "").strip()
        if not self.app_key or not self.app_secret:
            raise AppError("S2B App Key 或 App Secret 未配置", code="s2b_incomplete", status_code=503)
        self.http = httpx.Client(
            base_url=connection.base_url or "https://openapi.s2bdiy.com",
            timeout=90,
            follow_redirects=True,
            transport=transport,
        )
        self._token = ""
        self._expires_at = 0.0

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "S2BClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    material_limit = 20 * 1024 * 1024

    def _request(self, method: str, path: str, *, authenticated: bool = True, retry_auth: bool = True, outcome_unknown_on_timeout: bool = False, **kwargs: Any) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        if authenticated:
            headers["Authorization"] = f"Bearer {self.access_token()}"
        try:
            response = self.http.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as error:
            message = "S2B 请求超时"
            if outcome_unknown_on_timeout:
                message += "；创建结果未知，已暂停且不会自动重发"
            raise AppError(message, code="pod_write_outcome_unknown" if outcome_unknown_on_timeout else "s2b_timeout", status_code=504, details={"outcome_unknown": outcome_unknown_on_timeout, "path": path}) from error
        except httpx.NetworkError as error:
            raise AppError("S2B 网络连接失败", code="s2b_network_error", status_code=502) from error
        if response.status_code == 401 and authenticated and retry_auth:
            self._token = ""
            return self._request(method, path, authenticated=True, retry_auth=False, outcome_unknown_on_timeout=outcome_unknown_on_timeout, **kwargs)
        try:
            data = response.json()
        except ValueError as error:
            raise AppError("S2B 返回了无效响应", code="s2b_invalid_response", status_code=502) from error
        if not isinstance(data, dict):
            raise AppError("S2B 响应格式错误", code="s2b_invalid_response", status_code=502)
        if response.status_code >= 400 or data.get("status") not in (None, "success", True):
            raise AppError(
                f"S2B：{data.get('msg') or data.get('message') or f'HTTP {response.status_code}'}",
                code="s2b_api_error",
                status_code=response.status_code if response.status_code >= 400 else 502,
            )
        return data

    def access_token(self) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        data = self._request(
            "POST", "/open/v1/accessToken", authenticated=False,
            json={"app_key": self.app_key, "app_secret": self.app_secret},
        )
        payload = data.get("data")
        token = str(payload.get("token") if isinstance(payload, dict) else "").strip()
        if not token:
            raise AppError("S2B Token 响应缺少 token", code="s2b_token_invalid", status_code=502)
        self._token = token
        self._expires_at = time.monotonic() + 71 * 60 * 60
        return token

    @staticmethod
    def original_rendering_url(url: str) -> str:
        value = str(url or "").strip()
        parsed = urlsplit(value)
        hostname = str(parsed.hostname or "").casefold()
        if parsed.scheme != "https" or not (hostname == "s2bdiy.com" or hostname.endswith(".s2bdiy.com")):
            return value
        query = [
            (key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not (key.casefold() == "x-oss-process" and "image/resize" in item.casefold())
        ]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))

    @classmethod
    def catalog_product(cls, raw: dict[str, Any]) -> dict[str, Any]:
        item = dict(raw)
        source_id = str(item.get("id") or item.get("code") or "")
        item.update({
            "supplier": cls.provider,
            "supplier_name": cls.provider_name,
            "source_id": source_id,
            "selection_id": source_id,
            "selection_code": str(item.get("code") or "").strip(),
            "catalog_key": f"{cls.provider}:{source_id}",
            "cn_name": str(item.get("name") or item.get("en_name") or ""),
            "preview_url": str(item.get("view_image_src") or item.get("design_product_image") or ""),
        })
        return item

    @classmethod
    def catalog_category(cls, raw: dict[str, Any]) -> dict[str, Any]:
        item = dict(raw)
        item["supplier"] = cls.provider
        item["children"] = [cls.catalog_category(value) for value in item.get("children") or [] if isinstance(value, dict)]
        return item

    def product_categories(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/open/v1/basicProduct/categorys").get("data")
        if not isinstance(data, list):
            raise AppError("S2B 分类响应缺少 data 数组", code="s2b_catalog_invalid", status_code=502)
        return [self.catalog_category(item) for item in data if isinstance(item, dict)]

    def product_types(self, page: int, page_size: int = 100, *, country: str = "CN") -> dict[str, Any]:
        country = country.upper()
        if country not in {"CN", "US"}:
            raise AppError("S2B 货盘国家仅支持 CN 或 US", code="s2b_catalog_country_invalid", status_code=422)
        params = {"page": page, "per_page": page_size, "produce_country": country}
        if country == "CN":
            params["produce_area"] = 1
        payload = self._request(
            "GET", "/open/v1/basicProduct",
            params=params,
        ).get("data")
        if not isinstance(payload, dict):
            raise AppError("S2B 商品列表响应缺少分页数据", code="s2b_catalog_invalid", status_code=502)
        result = dict(payload)
        raw_items = [item for item in payload.get("data") or [] if isinstance(item, dict)]
        result["data"] = [
            self.catalog_product(item) for item in raw_items
            if str(item.get("produce_country") or "").upper() == country
            or (country == "CN" and not item.get("produce_country") and str(item.get("produce_area") or "") == "1")
        ]
        result["_page_item_count"] = len(raw_items)
        return result

    @classmethod
    def normalize_detail(cls, raw: dict[str, Any]) -> dict[str, Any]:
        detail = dict(raw)
        source_id = str(detail.get("id") or detail.get("code") or "")
        colors = [dict(item) for item in detail.get("colors") or [] if isinstance(item, dict)]
        for color in colors:
            color["cn_name"] = str(color.get("name") or color.get("en_name") or "")
            color["tone1"] = str(color.get("tone") or "")
        sizes = [dict(item) for item in detail.get("sizes") or [] if isinstance(item, dict)]
        views = [dict(item) for item in detail.get("views") or [] if isinstance(item, dict)]
        view_names = {int(item.get("id") or 0): str(item.get("name") or "") for item in views}
        print_areas = []
        for area in detail.get("print_areas") or []:
            if isinstance(area, dict):
                view_id = int(area.get("view_id") or area.get("id") or 0)
                print_areas.append({**area, "id": view_id, "view_id": view_id, "name": view_names.get(view_id) or f"印刷面 {view_id}"})
        stock_info = []
        price_groups = []
        # S2B US prices are CNY as confirmed by the user and supplier UI.
        # The official items schema specifies weight in g and dimensions in cm.
        currency = str(detail.get("currency") or "CNY")
        for raw_item in detail.get("items") or []:
            if not isinstance(raw_item, dict):
                continue
            stock = dict(raw_item)
            stock_info.append(stock)
            price_groups.append({
                "price": stock.get("price"), "currency": currency,
                "retail_price": {"price": stock.get("price"), "currency": currency},
                "stock_info": [stock],
            })
        attr_definitions: dict[int, dict[str, Any]] = {}
        size_attr_info: list[dict[str, Any]] = []
        for size_group in detail.get("attr_values") or []:
            if not isinstance(size_group, dict):
                continue
            size_id = int(size_group.get("size_id") or 0)
            for attribute in size_group.get("attr_value") or []:
                if not isinstance(attribute, dict):
                    continue
                attr_id = int(attribute.get("attr_id") or 0)
                if attr_id:
                    attr_definitions[attr_id] = {
                        "id": attr_id, "name": str(attribute.get("attr_name") or ""),
                        "cn_name": str(attribute.get("attr_name") or ""), "field_unit_name": "",
                    }
                    size_attr_info.append({"size_id": size_id, "attr_id": attr_id,
                                           "attr_value": attribute.get("attr_value")})
        image_urls: list[str] = []
        rendering_info: list[dict[str, Any]] = []
        for group in detail.get("product_show_images") or detail.get("show_images") or []:
            if not isinstance(group, dict):
                continue
            urls = []
            for image in group.get("images") or []:
                if isinstance(image, dict):
                    url = cls.original_rendering_url(str(image.get("big_src") or image.get("src") or ""))
                    if url.startswith("https://"):
                        urls.append(url); image_urls.append(url)
            rendering_info.append({"color_id": group.get("color_id"), "color_name": group.get("color_name"), "renderings": urls})
        default_color = int((colors[0].get("id") if colors else 0) or 0)
        default_size = int((sizes[0].get("id") if sizes else 0) or 0)
        default_view = int((print_areas[0].get("id") if print_areas else 0) or 0)
        detail.update({
            "currency": currency, "supplier_dimension_unit": "cm", "supplier_weight_unit": "g",
            "supplier": cls.provider, "supplier_name": cls.provider_name,
            "source_id": source_id, "selection_id": source_id,
            "selection_code": str(detail.get("code") or "").strip(),
            "catalog_key": f"{cls.provider}:{source_id}",
            "cn_name": str(detail.get("name") or detail.get("en_name") or ""),
            "cn_desc": str(detail.get("desc") or ""),
            "colors": colors, "sizes": sizes, "stock_info": stock_info,
            "size_attr_map": list(attr_definitions.values()), "size_attr_info": size_attr_info,
            "prices": {"price": detail.get("purchase_price"), "currency": currency, "default_color_id": default_color,
                       "default_size_id": default_size, "wholesale_price": price_groups},
            "default_values": {"color": default_color, "size": default_size, "view": default_view},
            "product_description": {
                "print_areas": print_areas, "detail_info_desc": str(detail.get("desc") or ""),
                "product_material": {"cn_name": str(detail.get("product_material_text") or ""),
                                     "en_name": str(detail.get("en_product_material_text") or "")},
                "product_technology": str(detail.get("product_technology_text") or ""),
            },
            "detail_img": list(dict.fromkeys(image_urls)), "renderings_info": rendering_info,
            "categories": list(detail.get("categorys") or []),
        })
        return detail

    def product_type(self, product_type_id: int, *, country: str = "CN") -> dict[str, Any]:
        country = country.upper()
        if country not in {"CN", "US"}:
            raise AppError("S2B 货盘国家仅支持 CN 或 US", code="s2b_catalog_country_invalid", status_code=422)
        payload = self._request("GET", f"/open/v1/basicProduct/{product_type_id}").get("data")
        if not isinstance(payload, dict) or not payload:
            raise AppError("S2B 商品详情为空", code="s2b_product_missing", status_code=404)
        actual = str(payload.get("produce_country") or "").upper()
        if not (actual == country or (country == "CN" and not actual and str(payload.get("produce_area") or "") == "1")):
            raise AppError(f"S2B 商品生产国家不符合 {country} 货盘", code="s2b_product_country_mismatch", status_code=422)
        return self.normalize_detail(payload)

    def calculate_products(self, *, products: list[dict[str, Any]], address: dict[str, Any], platform: int = 99) -> list[dict[str, Any]]:
        params: list[tuple[str, Any]] = [("platform", platform), ("country", _country_code(address.get("country", ""))),
            ("province", address.get("region") or address.get("province", "")), ("city", address.get("city", "")),
            ("postcode", address.get("postcode", ""))]
        for index, item in enumerate(products):
            params.extend([(f"products[{index}][product_id]", int(item.get("product_id") or 0)),
                (f"products[{index}][stock_sku_item_id]", int(item.get("stock_sku_item_id") or 0)),
                (f"products[{index}][num]", int(item.get("num") or 1))])
        payload = self._request("GET", "/open/v1/calculateProducts", params=params).get("data")
        if isinstance(payload, list): return [dict(item) for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict): return [dict(item) for item in payload.get("data") or payload.get("list") or payload.get("logistics") or [] if isinstance(item, dict)]
        return []

    def logistics_calculation(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Read country-level range for a US base product; never create an order."""
        if params.get('country') != 'US' or int(params.get('num') or 0) != 1:
            raise AppError('美国上架运费必须按单件试算', code='us_freight_invalid', status_code=422)
        payload = self._request('GET', '/open/v1/logisticsCalculation', params=params).get('data')
        if not isinstance(payload, list):
            raise AppError('S2B 运费响应缺少渠道数组', code='s2b_freight_invalid', status_code=502)
        return payload

    def order_by_out_order_id(self, out_order_id: str) -> dict[str, Any] | None:
        payload = self._request("GET", "/open/v1/order", params={"third_order_id": str(out_order_id), "per_page": 20}).get("data")
        values = payload.get("data") if isinstance(payload, dict) else []
        for item in values or []:
            if isinstance(item, dict) and str(item.get("third_order_id") or "") == str(out_order_id):
                order = dict(item); order_id = int(order.get("id") or 0)
                if order_id > 0:
                    detail = self._request("GET", f"/open/v1/order/{order_id}").get("data")
                    if isinstance(detail, dict): order.update(detail)
                return order
        return None

    def create_order(self, order: dict[str, Any]) -> dict[str, Any]:
        payload = self._request("POST", "/open/v1/order", json=order, outcome_unknown_on_timeout=True).get("data")
        if not isinstance(payload, dict) or not payload.get("id"):
            raise AppError("S2B 创建订单响应缺少订单编号；结果未知", code="pod_write_outcome_unknown", details={"outcome_unknown": True})
        return {**payload, "order_id": str(payload["id"])}

    @staticmethod
    def preview_url(product: dict[str, Any]) -> str | None:
        value = str(product.get("preview_url") or product.get("view_image_src") or product.get("product_show_master_image") or "")
        return value if value.startswith(("https://", "http://")) else None

    def upload_gallery(self, *, filename: str, content: bytes, mime_type: str, external_id: str, external_customer_id: str = "") -> dict[str, Any]:
        del external_id, external_customer_id
        if len(content) > self.material_limit:
            raise AppError("S2B 素材图片不能超过 20MB", code="pod_image_too_large", status_code=413)
        suffix = {"image/png": ".png", "image/jpeg": ".jpg"}.get(mime_type)
        if not suffix:
            raise AppError("S2B 素材只支持 PNG 或 JPG", code="pod_image_format_invalid")
        upload_name = Path(filename or f"material{suffix}").name
        payload = self._request(
            "POST", "/open/v1/material/uploadMaterial", data={"name": Path(upload_name).stem[:200]},
            files={"image": (upload_name, content, mime_type)}, outcome_unknown_on_timeout=True,
        ).get("data")
        material_id = int(payload.get("id") or 0) if isinstance(payload, dict) else 0
        if material_id <= 0:
            raise AppError("S2B 素材上传结果未知，已暂停且不会自动重发", code="pod_write_outcome_unknown", details={"outcome_unknown": True, "stage": "gallery"})
        return {**payload, "code": str(material_id)}

    def create_product(self, composition: dict[str, Any]) -> dict[str, Any]:
        views = [{
            "view_id": int(side["view_id"]),
            "objects": [{"type": "image", "material_id": int(side["gallery_code"]), "design_type": 1}],
        } for side in composition.get("sides") or [] if str(side.get("gallery_code") or "").isdigit()]
        sizes = [int(value) for value in composition.get("selected_size_ids") or [] if int(value) > 0]
        if not views or not sizes:
            raise AppError("S2B 产品缺少设计面或销售尺寸", code="pod_variant_selection_missing")
        response = self._request("POST", "/open/v1/product/quickCreate", json={
            "size_id": sizes[0], "color_id": int(composition["default_color_id"]),
            "product_design": {"basic_product_id": int(composition["product_type_id"]), "name": str(composition["product_name"])[:200], "views": views},
        }, outcome_unknown_on_timeout=True)
        payload = response.get("data")
        if not isinstance(payload, dict) or not payload.get("product_id") or not payload.get("product_code"):
            raise AppError("S2B 建品结果未知，已暂停且不会自动重发", code="pod_write_outcome_unknown", details={"outcome_unknown": True, "stage": "create_product"})
        return {**payload, "id": payload["product_id"], "code": str(payload["product_code"])}

    def custom_product(self, product_id: str | int) -> dict[str, Any]:
        payload = self._request("GET", f"/open/v1/product/{product_id}").get("data")
        return dict(payload) if isinstance(payload, dict) else {}

    @staticmethod
    def rendering_groups(product: dict[str, Any], default_color_id: int = 0) -> list[dict[str, Any]]:
        groups = []
        for group in product.get("show_images") or product.get("product_show_images") or []:
            if not isinstance(group, dict):
                continue
            urls = []
            for image in group.get("images") or []:
                value = S2BClient.original_rendering_url(str(image.get("big_src") or image.get("src") or "")) if isinstance(image, dict) else str(image or "")
                if value.startswith("https://"):
                    urls.append(value)
            if urls:
                groups.append({"color_id": int(group.get("color_id") or default_color_id or 0), "color_name": str(group.get("color_name") or ""), "rendering_urls": list(dict.fromkeys(urls)), "saved_files": []})
        return groups

    def download_rendering(self, url: str) -> tuple[bytes, str]:
        try:
            response = self.http.get(url)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise AppError("S2B 效果图下载失败", code="pod_rendering_download_failed", status_code=502) from error
        content_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
        if content_type not in {"image/png", "image/jpeg", "image/webp"} or not response.content or len(response.content) > 50 * 1024 * 1024:
            raise AppError("S2B 返回的效果图文件无效", code="pod_rendering_invalid", status_code=502)
        return response.content, content_type
