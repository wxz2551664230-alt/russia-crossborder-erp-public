from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Literal

import requests


class AlibabaApiError(RuntimeError):
    pass


class AlibabaNewtonClient:
    """1688 AOP client for cross-border image search and product details."""

    API_BASE = "https://gw.open.1688.com/openapi"

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        access_token: str = "",
        timeout: int = 30,
        attempts: int = 4,
    ) -> None:
        self.app_key = app_key.strip()
        self.app_secret = app_secret.strip()
        self.access_token = access_token.strip()
        self.timeout = timeout
        self.attempts = max(1, min(6, int(attempts or 1)))

    def get_cross_product_info(self, product_id: str | int) -> dict[str, Any]:
        """Calls com.alibaba.product:alibaba.cross.productInfo-1."""
        return self._call(
            namespace="com.alibaba.product",
            api_name="alibaba.cross.productInfo",
            version=1,
            params={"productId": str(product_id)},
        )

    def search_cross_products_by_image(
        self,
        image_url: str,
        page: int = 1,
        price_min: float | str | None = None,
        price_max: float | str | None = None,
        sort_fields: str | None = None,
        cps_first: bool | None = None,
        media_id: int | None = None,
        media_zone_id: int | None = None,
        category_id: str | None = None,
        classify: Literal[
            "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13"
        ]
        | None = None,
        tags: str | None = None,
    ) -> dict[str, Any]:
        """Calls com.alibaba.linkplus:alibaba.cross.similar.offer.search-1.

        The source image must be reachable by 1688, no larger than 2 MB, and
        preferably at least 450x450 pixels. Results use a fixed server page size.
        """
        if not image_url.strip().lower().startswith(("http://", "https://")):
            raise AlibabaApiError("图搜图片必须是1688能够访问的 HTTP/HTTPS 地址")
        if page < 1:
            raise AlibabaApiError("图搜页码必须从1开始")
        if price_min is not None and price_max is not None:
            try:
                if float(price_min) > float(price_max):
                    raise AlibabaApiError("最低价格不能高于最高价格")
            except (TypeError, ValueError) as exc:
                raise AlibabaApiError("价格筛选必须是有效数字") from exc

        params: dict[str, Any] = {"picUrl": image_url.strip(), "page": page}
        optional = {
            "priceMin": price_min,
            "priceMax": price_max,
            "sortFields": sort_fields,
            "cpsFirst": None if cps_first is None else str(cps_first).lower(),
            "mediaId": media_id,
            "mediaZoneId": media_zone_id,
            "categoryID": category_id,
            "classify": classify,
            "tags": tags,
        }
        params.update({key: value for key, value in optional.items() if value is not None})
        return self._call(
            namespace="com.alibaba.linkplus",
            api_name="alibaba.cross.similar.offer.search",
            version=1,
            params=params,
        )

    def _call(
        self,
        namespace: str,
        api_name: str,
        version: int,
        params: dict[str, Any],
        *,
        raise_api_errors: bool = True,
    ) -> dict[str, Any]:
        if not self.app_key or not self.app_secret:
            raise AlibabaApiError("请先填写 1688 AppKey 和 AppSecret")

        api_path = f"param2/{version}/{namespace}/{api_name}/{self.app_key}"
        request_params = {
            key: self._encode_param(value)
            for key, value in params.items()
            if value is not None
        }
        if self.access_token:
            request_params["access_token"] = self.access_token
        request_params["_aop_signature"] = self._sign(api_path, request_params)

        payload: dict[str, Any] | None = None
        last_error: requests.RequestException | None = None
        last_response = None
        attempts_used = 0
        is_task_create = api_name == "newtoncloud.task.create"
        for attempt in range(1, self.attempts + 1):
            attempts_used = attempt
            try:
                response = requests.post(
                    f"{self.API_BASE}/{api_path}",
                    data=request_params,
                    timeout=(10, self.timeout),
                )
                last_response = response
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise AlibabaApiError("1688接口没有返回有效JSON") from exc
                break
            except requests.RequestException as exc:
                last_error = exc
                status = int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
                if is_task_create:
                    # A connect timeout occurs before the request reaches 1688 and is safe
                    # to retry.  Read timeouts and HTTP failures may have created a task,
                    # so retrying those could create duplicates.
                    retryable = isinstance(exc, requests.ConnectTimeout)
                else:
                    retryable = isinstance(exc, (
                        requests.ConnectTimeout,
                        requests.ConnectionError,
                        requests.ReadTimeout,
                    )) or status == 429 or status >= 500
                if not retryable or attempt >= self.attempts:
                    break
                time.sleep(min(8, 2 ** attempt))
        if payload is None:
            detail = str(getattr(last_response, "text", "") or "")[:800]
            if "gw.APIACLDecline" in detail or "not allowed(acl)" in detail:
                raise AlibabaApiError(
                    "当前1688应用没有商品详情API权限，请在1688开放平台为此AppKey申请"
                    " alibaba.cross.productInfo 权限后重新授权账号"
                ) from last_error
            raise AlibabaApiError(
                f"1688接口连续{attempts_used}次请求失败：{last_error}"
                + (f"；{detail}" if detail else "")
            ) from last_error

        if (
            raise_api_errors
            and isinstance(payload, dict)
            and (payload.get("errorCode") or payload.get("error"))
        ):
            raise AlibabaApiError(str(payload))
        return payload

    def call_api(
        self,
        namespace: str,
        api_name: str,
        version: int,
        params: dict[str, Any],
        *,
        raise_api_errors: bool = True,
    ) -> dict[str, Any]:
        """Public generic AOP call used by specialized 1688 service clients."""
        return self._call(
            namespace,
            api_name,
            version,
            params,
            raise_api_errors=raise_api_errors,
        )

    @staticmethod
    def _encode_param(value: Any) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return str(value)

    def _sign(self, api_path: str, params: dict[str, str]) -> str:
        joined = api_path + "".join(
            f"{key}{params[key]}" for key in sorted(params)
        )
        digest = hmac.new(
            self.app_secret.encode("utf-8"),
            joined.encode("utf-8"),
            hashlib.sha1,
        ).hexdigest()
        return digest.upper()
