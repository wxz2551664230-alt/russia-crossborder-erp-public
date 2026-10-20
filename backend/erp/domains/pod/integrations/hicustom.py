from __future__ import annotations

import time
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from erp.core.errors import AppError
from erp.domains.pod.integrations.common import SupplierConnectionConfig


class HicustomClient:
    provider = "hicustom"
    provider_name = "指纹科技"

    def __init__(self, connection: SupplierConnectionConfig, *, transport: httpx.BaseTransport | None = None) -> None:
        self.app_key = str(connection.secrets.get("app_key") or "").strip()
        self.app_secret = str(connection.secrets.get("app_secret") or "").strip()
        if not self.app_key or not self.app_secret:
            raise AppError("HICUSTOM App Key 或 App Secret 未配置", code="hicustom_incomplete", status_code=503)
        self.http = httpx.Client(
            base_url=connection.base_url or "https://api.hicustom.com",
            timeout=90,
            follow_redirects=True,
            transport=transport,
        )
        self._access_token = ""
        self._refresh_token = ""
        self._expires_at = 0.0

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "HicustomClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def payload(data: dict[str, Any]) -> dict[str, Any]:
        nested = data.get("data")
        return nested if isinstance(nested, dict) else data

    def _request(
        self, method: str, path: str, *, outcome_unknown_on_timeout: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.TimeoutException as error:
            message = "HICUSTOM 请求超时"
            if outcome_unknown_on_timeout:
                message += "；创建结果未知，已暂停且不会自动重发"
            raise AppError(
                message,
                code="pod_write_outcome_unknown" if outcome_unknown_on_timeout else "hicustom_timeout",
                status_code=504,
                details={"outcome_unknown": outcome_unknown_on_timeout, "path": path},
            ) from error
        except httpx.NetworkError as error:
            raise AppError("HICUSTOM 网络连接失败", code="hicustom_network_error", status_code=502) from error
        if not response.content and response.is_success:
            return {}
        try:
            data = response.json()
        except ValueError as error:
            if response.status_code >= 400:
                raise AppError(
                    f"HICUSTOM：HTTP {response.status_code}",
                    code="hicustom_api_error",
                    status_code=response.status_code,
                    details={"http_status": response.status_code, "path": path},
                ) from error
            raise AppError("HICUSTOM 返回了无效响应", code="hicustom_invalid_response", status_code=502) from error
        if not isinstance(data, dict):
            raise AppError("HICUSTOM 响应格式错误", code="hicustom_invalid_response", status_code=502)
        code = data.get("code")
        if response.status_code >= 400 or data.get("status") is False or (isinstance(code, int) and code >= 400):
            message = str(data.get("message") or data.get("msg") or f"HTTP {response.status_code}")
            raise AppError(
                f"HICUSTOM：{message}",
                code="hicustom_api_error",
                status_code=response.status_code if response.status_code >= 400 else 502,
                details={"http_status": response.status_code, "path": path},
            )
        return data

    def _request_token(self) -> None:
        payload = self.payload(self._request(
            "GET", "/oauth/token", params={"app_key": self.app_key, "app_secret": self.app_secret}
        ))
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise AppError("HICUSTOM Token 响应缺少 access_token", code="hicustom_token_invalid", status_code=502)
        self._access_token = token
        self._refresh_token = str(payload.get("refresh_token") or "").strip()
        self._expires_at = time.monotonic() + max(60, int(payload.get("expires_in") or 7200))

    def access_token(self) -> str:
        if not self._access_token or time.monotonic() >= self._expires_at - 300:
            self._request_token()
        return self._access_token

    def product_categories(self) -> list[dict[str, Any]]:
        data = self._request(
            "GET", "/api/v1/product-type-categories", params={"access_token": self.access_token()}
        ).get("data")
        if not isinstance(data, list):
            raise AppError("HICUSTOM 分类响应缺少 data 数组", code="hicustom_catalog_invalid", status_code=502)
        return [dict(item) for item in data if isinstance(item, dict)]

    def product_types(self, page: int, page_size: int = 100) -> dict[str, Any]:
        return self.payload(self._request(
            "GET", "/api/v1/product-types",
            params={"access_token": self.access_token(), "page": page, "page_size": page_size},
        ))

    def product_type(self, product_type_id: int) -> dict[str, Any]:
        return self.payload(self._request(
            "GET", f"/api/v1/product-type/{product_type_id}", params={"access_token": self.access_token()}
        ))

    @staticmethod
    def preview_url(product: dict[str, Any]) -> str | None:
        for value in product.get("detail_img") or []:
            if isinstance(value, str) and value.startswith(("https://", "http://")):
                return value
        for item in product.get("renderings_info") or []:
            if isinstance(item, dict):
                for value in item.get("renderings") or []:
                    if isinstance(value, str) and value.startswith(("https://", "http://")):
                        return value
        return None

    def gallery_exists(self, gallery_code: str) -> bool:
        try:
            data = self._request(
                "GET",
                f"/api/v1/gallery/{quote(str(gallery_code), safe='')}",
                params={"access_token": self.access_token()},
            )
        except AppError as error:
            message = str(error).casefold()
            if error.details and error.details.get("http_status") == 404:
                return False
            if any(marker in message for marker in ("不存在", "未找到", "not found")):
                return False
            raise
        return bool(str(self.payload(data).get("code") or gallery_code).strip())

    def delete_gallery(self, gallery_code: str) -> None:
        self._request(
            "DELETE",
            f"/api/v1/gallery/{quote(str(gallery_code), safe='')}",
            params={"access_token": self.access_token()},
        )

    def upload_gallery(
        self, *, filename: str, content: bytes, mime_type: str, external_id: str,
        external_customer_id: str = "",
    ) -> dict[str, Any]:
        suffix = {"image/png": ".png", "image/jpeg": ".jpg"}.get(mime_type)
        if not suffix:
            raise AppError("HICUSTOM 素材只支持 PNG 或 JPG", code="pod_image_format_invalid")
        upload_name = Path(filename or f"artwork{suffix}").name
        if Path(upload_name).suffix.casefold() not in ({".jpg", ".jpeg"} if suffix == ".jpg" else {suffix}):
            upload_name = f"{Path(upload_name).stem or 'artwork'}{suffix}"
        display_name = Path(upload_name).stem[:200]
        form = {
            "access_token": self.access_token(), "external_id": external_id,
            "external_customer_id": external_customer_id, "cn_name": display_name, "en_name": display_name,
        }
        payload = self.payload(self._request(
            "POST", "/api/v1/gallery",
            data={key: value for key, value in form.items() if value},
            files={"image": (upload_name, content, mime_type)},
            outcome_unknown_on_timeout=True,
        ))
        if not str(payload.get("code") or "").strip():
            raise AppError(
                "HICUSTOM 图库上传响应缺少编号；结果未知，已暂停且不会自动重发",
                code="pod_write_outcome_unknown", details={"outcome_unknown": True, "stage": "gallery"},
            )
        return payload

    def create_product(self, composition: dict[str, Any]) -> dict[str, Any]:
        cfgs = []
        for side in composition.get("sides") or []:
            if not side.get("gallery_code"):
                continue
            # Native ERP product versions keep layout under ``placement`` while
            # older composition jobs kept the same fields at the side root.
            placement = side.get("placement") if isinstance(side.get("placement"), dict) else {}
            def layout_value(name: str):
                return side.get(name) if side.get(name) is not None else placement.get(name)
            values = {name: layout_value(name) for name in ("width", "height", "top_x", "top_y")}
            if any(value is None for value in values.values()):
                raise AppError("印刷面缺少宽高或排版坐标", code="pod_composition_placement_missing", details={"view_id": side.get("view_id")})
            cfgs.append({
                "view_id": int(side["view_id"]), "gallery_code": str(side["gallery_code"]),
                "width": int(values["width"]), "height": int(values["height"]),
                "top_x": float(values["top_x"]), "top_y": float(values["top_y"]),
            })
        if not cfgs:
            raise AppError("任务没有可用的印刷面", code="pod_composition_sides_missing")
        form: dict[str, Any] = {
            "access_token": self.access_token(), "external_id": composition["external_id"],
            "product_type_id": int(composition["product_type_id"]),
            "default_color_id": int(composition["default_color_id"]),
            "default_view_id": int(composition["default_view_id"]),
            "cfgs": json.dumps(cfgs, ensure_ascii=False, separators=(",", ":")),
        }
        if composition.get("external_customer_id"):
            form["external_customer_id"] = composition["external_customer_id"]
        payload = self.payload(self._request(
            "POST", "/api/v1/product", data=form, outcome_unknown_on_timeout=True,
        ))
        if not str(payload.get("code") or "").strip():
            raise AppError(
                "HICUSTOM 建品响应缺少产品编号；结果未知，已暂停且不会自动重发",
                code="pod_write_outcome_unknown", details={"outcome_unknown": True, "stage": "create_product"},
            )
        return payload

    def custom_product(self, product_code: str) -> dict[str, Any]:
        return self.payload(self._request(
            "GET", f"/api/v1/product/{quote(str(product_code), safe='')}",
            params={"access_token": self.access_token()},
        ))

    def order_by_out_order_id(self, out_order_id: str) -> dict[str, Any] | None:
        try:
            data = self._request("GET", f"/api/v1/out-order-id/{quote(str(out_order_id), safe='')}",
                                 params={"access_token": self.access_token()})
        except AppError as error:
            message = str(error).replace(" ", "")
            if (error.details or {}).get("http_status") == 404 or any(value in message for value in ("不存在的订单", "订单不存在", "查无此订单")):
                return None
            raise
        nested = data.get("data")
        if isinstance(nested, list):
            return next((dict(item) for item in nested if isinstance(item, dict)), None)
        payload = self.payload(data)
        return dict(payload) if isinstance(payload, dict) and payload else None

    def create_order(self, order: dict[str, Any]) -> dict[str, Any]:
        data = self._request("POST", "/api/v1/order", data={"access_token": self.access_token(),
            "order": json.dumps(order, ensure_ascii=False, separators=(",", ":"))}, outcome_unknown_on_timeout=True)
        nested = data.get("data")
        payload = next((dict(item) for item in nested if isinstance(item, dict)), {}) if isinstance(nested, list) else self.payload(data)
        if not str(payload.get("order_id") or payload.get("id") or "").strip():
            message = str(payload.get("error_msg") or payload.get("message") or data.get("message") or "").strip()
            raise AppError(f"HICUSTOM 创建订单未成功{f'：{message}' if message else '，结果未知'}",
                code="hicustom_order_create_failed" if message else "pod_write_outcome_unknown",
                details={"outcome_unknown": not bool(message)})
        return dict(payload)

    @staticmethod
    def rendering_groups(product: dict[str, Any], default_color_id: int = 0) -> list[dict[str, Any]]:
        def urls(values: Any) -> list[str]:
            result: list[str] = []
            for item in values or []:
                value = item if isinstance(item, str) else item.get("big_img") or item.get("small_img") if isinstance(item, dict) else ""
                if str(value).startswith("https://"):
                    result.append(str(value))
            return list(dict.fromkeys(result))
        groups = []
        for color in product.get("colors") or []:
            if isinstance(color, dict) and (items := urls(color.get("renderings"))):
                groups.append({
                    "color_id": int(color.get("id") or color.get("color_id") or default_color_id or 0),
                    "color_name": str(color.get("cn_name") or color.get("color_name") or color.get("name") or ""),
                    "rendering_urls": items, "saved_files": [],
                })
        direct = urls(product.get("renderings"))
        if direct and not groups:
            groups.append({"color_id": int(default_color_id or 0), "color_name": "", "rendering_urls": direct, "saved_files": []})
        return groups

    def download_rendering(self, url: str) -> tuple[bytes, str]:
        try:
            response = self.http.get(url)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise AppError("HICUSTOM 效果图下载失败", code="pod_rendering_download_failed", status_code=502) from error
        content_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
        if content_type not in {"image/png", "image/jpeg", "image/webp"} or not response.content or len(response.content) > 50 * 1024 * 1024:
            raise AppError("HICUSTOM 返回的效果图文件无效", code="pod_rendering_invalid", status_code=502)
        return response.content, content_type
