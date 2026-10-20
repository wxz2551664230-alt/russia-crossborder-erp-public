from __future__ import annotations

import re
import time
from typing import Any, Callable
from decimal import Decimal

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class OzonApiError(RuntimeError):
    pass


class OzonImportUncertain(OzonApiError):
    """The create request may have reached Ozon; callers must not resend it."""


class OzonSellerClient:
    CATALOG_LANGUAGES = {"DEFAULT", "RU", "EN", "TR", "ZH_HANS"}

    def __init__(
        self,
        client_id: str,
        api_key: str,
        api_base: str = "https://api-seller.ozon.ru",
        timeout: int = 60,
        *,
        session: requests.Session | None = None,
        repository: Any = None,
    ) -> None:
        self.client_id = client_id.strip()
        self.api_key = api_key.strip()
        self.api_base = api_base.rstrip("/")
        self.timeout = (20, max(45, int(timeout)))
        self.session = session or requests.Session()
        self.repository = repository
        if session is None:
            retry = Retry(
                total=3,
                connect=3,
                read=0,
                redirect=0,
                status=0,
                backoff_factor=1,
                allowed_methods=frozenset({"POST"}),
            )
            self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        product_create: bool = False,
    ) -> dict[str, Any]:
        if not self.client_id or not self.api_key:
            raise OzonApiError("请先填写 Ozon Seller Client ID 和 API Key")
        normalized_path = "/" + path.lstrip("/")
        try:
            response = self.session.post(
                f"{self.api_base}{normalized_path}",
                headers={
                    "Client-Id": self.client_id,
                    "Api-Key": self.api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
        except requests.exceptions.ReadTimeout as exc:
            if product_create:
                raise OzonImportUncertain(
                    "Ozon创建商品请求等待响应超时；请按Offer ID到卖家后台核对，"
                    "程序不会自动重发，以免重复建品。"
                ) from exc
            raise OzonApiError("Ozon接口等待响应超时") from exc
        except requests.RequestException as exc:
            raise OzonApiError(f"Ozon接口请求失败：{exc}") from exc
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = response.text[:2000]
            raise OzonApiError(
                f"Ozon API {normalized_path} 返回 HTTP {response.status_code}：{detail}"
            ) from exc
        try:
            value = response.json()
        except ValueError as exc:
            raise OzonApiError("Ozon接口没有返回有效JSON") from exc
        if not isinstance(value, dict):
            raise OzonApiError("Ozon接口返回格式不正确")
        return value

    def get(self, path: str) -> dict[str, Any]:
        if not self.client_id or not self.api_key:
            raise OzonApiError("请先填写 Ozon Seller Client ID 和 API Key")
        normalized_path = "/" + path.lstrip("/")
        try:
            response = self.session.get(
                f"{self.api_base}{normalized_path}",
                headers={
                    "Client-Id": self.client_id,
                    "Api-Key": self.api_key,
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            value = response.json()
        except requests.RequestException as exc:
            raise OzonApiError(f"Ozon接口请求失败：{exc}") from exc
        except ValueError as exc:
            raise OzonApiError("Ozon接口没有返回有效JSON") from exc
        if not isinstance(value, dict):
            raise OzonApiError("Ozon接口返回格式不正确")
        return value

    def category_tree(self, language: str = "ZH_HANS") -> list[dict[str, Any]]:
        selected = language.strip().upper()
        if selected not in self.CATALOG_LANGUAGES:
            raise OzonApiError("Ozon类目语言不受支持")
        data = self.post("/v1/description-category/tree", {"language": selected})
        result = data.get("result", data.get("items", []))
        return [item for item in (result or []) if isinstance(item, dict)]

    def category_attributes(
        self,
        description_category_id: int,
        type_id: int,
        language: str = "ZH_HANS",
    ) -> list[dict[str, Any]]:
        selected_language = language.strip().upper()
        if self.repository:
            cached = self.repository.get_category_attributes_cache(description_category_id, type_id, selected_language)
            if cached is not None:
                return cached
        data = self.post(
            "/v1/description-category/attribute",
            {
                "description_category_id": int(description_category_id),
                "type_id": int(type_id),
                "language": selected_language,
            },
        )
        result = data.get("result", [])
        if isinstance(result, dict):
            result = result.get("attributes") or result.get("items") or []
        items = [item for item in (result or []) if isinstance(item, dict)]
        if self.repository:
            self.repository.save_category_attributes_cache(description_category_id, type_id, selected_language, items)
        return items

    def category_attribute_values(
        self,
        description_category_id: int,
        type_id: int,
        attribute_id: int,
        *,
        language: str = "ZH_HANS",
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        selected_language = language.strip().upper()
        if self.repository:
            cached = self.repository.get_attribute_values_cache(description_category_id, type_id, attribute_id, selected_language)
            if cached is not None:
                return cached
        values: list[dict[str, Any]] = []
        seen: set[int] = set()
        last_value_id = 0
        for _ in range(1000):
            data = self.post(
                "/v1/description-category/attribute/values",
                {
                    "description_category_id": int(description_category_id),
                    "type_id": int(type_id),
                    "attribute_id": int(attribute_id),
                    "language": selected_language,
                    "last_value_id": last_value_id,
                    "limit": int(limit),
                },
            )
            result = data.get("result", [])
            page = (
                result.get("values") or result.get("items") or []
                if isinstance(result, dict)
                else result
            )
            page = [item for item in (page or []) if isinstance(item, dict) and item.get("id")]
            for item in page:
                value_id = int(item["id"])
                if value_id not in seen:
                    seen.add(value_id)
                    values.append(item)
            if len(page) < limit:
                if self.repository:
                    self.repository.save_attribute_values_cache(description_category_id, type_id, attribute_id, selected_language, values)
                return values
            next_id = int(page[-1]["id"])
            if next_id == last_value_id:
                raise OzonApiError(f"Ozon字典属性 {attribute_id} 返回重复分页游标")
            last_value_id = next_id
        raise OzonApiError(f"Ozon字典属性 {attribute_id} 分页超过安全上限")

    def search_category_attribute_values(
        self,
        description_category_id: int,
        type_id: int,
        attribute_id: int,
        query: str,
        *,
        language: str = "ZH_HANS",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if self.repository:
            values = self.category_attribute_values(description_category_id, type_id, attribute_id, language=language)
            normalized = str(query or "").strip().casefold().replace("ё", "е")
            matched = [item for item in values if normalized in str(item.get("value") or item.get("name") or "").casefold().replace("ё", "е")]
            return matched[:max(1, min(int(limit), 100))]
        data = self.post(
            "/v1/description-category/attribute/values/search",
            {
                "description_category_id": int(description_category_id),
                "type_id": int(type_id),
                "attribute_id": int(attribute_id),
                "language": language.strip().upper(),
                "value": str(query or "").strip(),
                "limit": max(1, min(int(limit), 100)),
            },
        )
        result = data.get("result", [])
        if isinstance(result, dict):
            result = result.get("values") or result.get("items") or []
        return [item for item in (result or []) if isinstance(item, dict)]

    def import_product(self, item: dict[str, Any]) -> int:
        data = self.post("/v3/product/import", {"items": [item]}, product_create=True)
        try:
            return int(data["result"]["task_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OzonApiError("Ozon创建商品响应中缺少task_id") from exc

    def import_info(self, task_id: int) -> dict[str, Any]:
        return self.post("/v1/product/import/info", {"task_id": int(task_id)})

    def wait_import(
        self,
        task_id: int,
        *,
        attempts: int = 30,
        interval: float = 3,
        on_status: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        last: dict[str, Any] = {}
        for attempt in range(max(1, attempts)):
            last = self.import_info(task_id)
            if on_status:
                on_status(last)
            items = _import_items(last)
            statuses = {str(item.get("status") or "").casefold() for item in items}
            if any(item.get("errors") for item in items):
                return last
            if statuses & {"imported", "failed", "error"}:
                return last
            if attempt + 1 < attempts and interval > 0:
                time.sleep(interval)
        return last

    def warehouses(self) -> list[dict[str, Any]]:
        warehouses: list[dict[str, Any]] = []
        cursor = ""
        seen: set[str] = set()
        while True:
            body: dict[str, Any] = {"limit": 100}
            if cursor:
                body["cursor"] = cursor
            data = self.post("/v2/warehouse/list", body)
            envelope = data.get("result") if isinstance(data.get("result"), dict) else data
            page = envelope.get("warehouses")
            if page is None:
                page = data.get("result") if isinstance(data.get("result"), list) else data.get("items", [])
            warehouses.extend(item for item in (page or []) if isinstance(item, dict))
            next_cursor = str(envelope.get("cursor") or data.get("cursor") or "")
            has_next = bool(envelope.get("has_next", data.get("has_next", False)))
            if not has_next or not next_cursor or next_cursor in seen:
                return warehouses
            seen.add(next_cursor)
            cursor = next_cursor

    def update_stock(self, offer_id: str, warehouse_id: int, stock: int) -> dict[str, Any]:
        if stock < 0:
            raise OzonApiError("库存不能小于0")
        return self.post(
            "/v2/products/stocks",
            {
                "stocks": [
                    {
                        "offer_id": str(offer_id).strip(),
                        "warehouse_id": int(warehouse_id),
                        "stock": int(stock),
                    }
                ]
            },
        )

    def promotions(self) -> list[dict[str, Any]]:
        data = self.get("/v1/actions")
        result = data.get("result", data.get("items", []))
        if isinstance(result, dict):
            result = result.get("actions") or result.get("items") or []
        return [item for item in (result or []) if isinstance(item, dict) and item.get("id")]

    def promotion_candidates(self, action_id: int, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        return self.post("/v1/actions/candidates", {
            "action_id": int(action_id), "limit": int(limit), "offset": int(offset),
        })

    @staticmethod
    def _promotion_candidate_page(data: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        result = data.get("result", {})
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)], len(result)
        products = result.get("products") or result.get("items") or []
        total = int(result.get("total") or result.get("total_count") or len(products))
        return [item for item in products if isinstance(item, dict)], total

    def find_promotion_candidate(self, action_id: int, product_id: int, *, page_limit: int = 100) -> dict[str, Any] | None:
        offset = 0
        for _page in range(100):
            products, total = self._promotion_candidate_page(
                self.promotion_candidates(action_id, limit=page_limit, offset=offset)
            )
            for product in products:
                candidate_id = product.get("id") or product.get("product_id")
                if candidate_id is not None and int(candidate_id) == int(product_id):
                    return product
            offset += len(products)
            if not products or offset >= total:
                return None
        raise OzonApiError("Ozon促销候选商品超过100页，已停止查询")

    def activate_promotion(self, action_id: int, product_id: int, action_price, stock: int) -> dict[str, Any]:
        return self.post("/v1/actions/products/activate", {
            "action_id": int(action_id),
            "products": [{
                "product_id": int(product_id),
                "action_price": float(Decimal(str(action_price))),
                "stock": int(stock),
            }],
        })

    def product_info_by_offer(self, offer_id: str) -> dict[str, Any] | None:
        normalized = str(offer_id or "").strip()
        if not normalized:
            raise OzonApiError("Offer ID不能为空")
        data = self.post("/v3/product/info/list", {"offer_id": [normalized]})
        result = data.get("result")
        items = result.get("items", []) if isinstance(result, dict) else result
        if not isinstance(items, list):
            items = data.get("items", [])
        for item in items or []:
            if isinstance(item, dict) and str(item.get("offer_id") or "").strip() == normalized:
                return item
        return None

    def wait_product_available(
        self,
        offer_id: str,
        *,
        attempts: int = 12,
        interval: float = 5,
        on_status: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        for attempt in range(max(1, attempts)):
            product = self.product_info_by_offer(offer_id)
            if product:
                return product
            if on_status:
                on_status(f"等待商品在Ozon目录生效（{attempt + 1}/{attempts}）")
            if attempt + 1 < attempts and interval > 0:
                time.sleep(interval)
        raise OzonApiError(
            f"商品 {offer_id} 尚未出现在Ozon商品列表；可稍后再次执行库存发布。"
        )


def import_succeeded(value: dict[str, Any]) -> bool:
    items = _import_items(value)
    return bool(items) and all(
        str(item.get("status") or "").casefold() == "imported"
        and not _blocking_import_errors(item)
        for item in items
    )


def import_errors(value: dict[str, Any]) -> list[dict[str, Any]]:
    failed: list[dict[str, Any]] = []
    for item in _import_items(value):
        status_failed = bool(re.fullmatch(
            r"failed|error", str(item.get("status") or ""), flags=re.I
        ))
        blocking = _blocking_import_errors(item)
        if status_failed or blocking:
            normalized = dict(item)
            normalized["errors"] = blocking or list(item.get("errors") or [])
            failed.append(normalized)
    return failed


def _blocking_import_errors(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Ozon includes non-fatal validation warnings in the errors array."""
    return [
        error
        for error in (item.get("errors") or [])
        if isinstance(error, dict)
        and str(error.get("level") or "").strip().casefold() != "warning"
    ]


def _import_items(value: dict[str, Any]) -> list[dict[str, Any]]:
    result = value.get("result", {})
    items = result.get("items", []) if isinstance(result, dict) else []
    return [item for item in (items or []) if isinstance(item, dict)]
