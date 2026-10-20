from __future__ import annotations

import re
import time
from decimal import Decimal
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from erp.modules.ozon_follow.legacy.storage import ProductRepository


class OzonApiError(RuntimeError):
    def __init__(self,message,*,status_code=None,request_path=None):
        super().__init__(message);self.status_code=status_code;self.request_path=request_path


class OzonImportUncertain(OzonApiError):
    pass


class OzonSellerAdapter:
    def __init__(self, config: dict[str, Any], *, session: requests.Session | None = None, repository: Any = None) -> None:
        secrets = config.get("secrets") or {}
        self.client_id = str(secrets.get("client_id") or config.get("external_account_id") or "").strip()
        self.api_key = str(secrets.get("api_key") or "").strip()
        self.currency = str(config.get("currency") or config.get("currency_code") or "CNY").upper()
        self.api_base = str(config.get("api_base") or config.get("base_url") or "https://api-seller.ozon.ru").rstrip("/")
        timeout = int(config.get("request_timeout") or 60)
        self.timeout = (20, max(45, timeout))
        self.session = session or requests.Session()
        self.repository = repository or ProductRepository()
        self._last_analytics_request_at = 0.0
        if session is None:
            retry = Retry(total=3, connect=3, read=0, redirect=0, status=0, backoff_factor=1, allowed_methods=frozenset({"POST"}))
            self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def post(self, path: str, payload: dict[str, Any], *, product_create: bool = False) -> dict[str, Any]:
        if not self.client_id or not self.api_key:
            raise OzonApiError("店铺缺少 Ozon Client ID 或 API Key")
        normalized = "/" + path.lstrip("/")
        read_only_paths = {
            "/v1/description-category/tree",
            "/v1/description-category/attribute",
            "/v1/description-category/attribute/values",
            "/v2/warehouse/list",
            "/v3/product/list",
            "/v3/product/info/list",
            "/v1/product/rating-by-sku",
            "/v1/analytics/data",
            "/v1/product/import/info",
        }
        analytics_read = normalized == "/v1/analytics/data"
        max_attempts = 6 if analytics_read else 5
        for attempt in range(max_attempts):
            if analytics_read and self._last_analytics_request_at:
                pause = 2.0 - (time.monotonic() - self._last_analytics_request_at)
                if pause > 0:
                    time.sleep(pause)
            if analytics_read:
                self._last_analytics_request_at = time.monotonic()
            try:
                response = self.session.post(
                    self.api_base + normalized,
                    headers={"Client-Id": self.client_id, "Api-Key": self.api_key, "Content-Type": "application/json", "Accept": "application/json"},
                    json=payload,
                    timeout=self.timeout,
                )
            except requests.exceptions.ReadTimeout as error:
                if product_create:
                    raise OzonImportUncertain("Ozon 创建商品响应超时；为避免重复建品，系统不会自动重发") from error
                raise OzonApiError("Ozon 接口等待响应超时") from error
            except requests.RequestException as error:
                if product_create:
                    raise OzonImportUncertain("Ozon 创建商品连接中断，受理结果未知；请先核对原商品或 task_id") from error
                raise OzonApiError(f"Ozon 接口请求失败：{error}") from error
            if response.status_code != 429 or normalized not in read_only_paths or attempt == max_attempts - 1:
                break
            retry_after = response.headers.get("Retry-After")
            try:
                delay = max(float(retry_after or 0), float(5 * 2 ** attempt if analytics_read else 2 ** attempt))
            except ValueError:
                delay = float(5 * 2 ** attempt if analytics_read else 2 ** attempt)
            time.sleep(min(delay, 60.0 if analytics_read else 16.0))
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            if product_create and response.status_code >= 500:
                raise OzonImportUncertain("Ozon 创建商品返回服务端错误，受理结果需核对") from error
            raise OzonApiError(f"Ozon API {normalized} 返回 HTTP {response.status_code}：{response.text[:1500]}",status_code=response.status_code,request_path=normalized) from error
        try:
            value = response.json()
        except ValueError as error:
            if product_create:
                raise OzonImportUncertain("Ozon 创建商品回执无法解析，请先核对原 task_id") from error
            raise OzonApiError("Ozon 接口没有返回有效 JSON") from error
        if not isinstance(value, dict):
            if product_create:
                raise OzonImportUncertain("Ozon 创建商品回执格式异常，请先核对原 task_id")
            raise OzonApiError("Ozon 接口返回格式不正确")
        return value

    def test_connection(self) -> dict[str, Any]:
        tree = self.category_tree("ZH_HANS")
        return {"ok": True, "category_roots": len(tree)}

    def category_tree(self, language: str = "ZH_HANS") -> list[dict[str, Any]]:
        data = self.post("/v1/description-category/tree", {"language": language})
        result = data.get("result", data.get("items", []))
        return [item for item in (result or []) if isinstance(item, dict)]

    def category_attributes(self, category_id: int, type_id: int, language: str = "ZH_HANS") -> list[dict[str, Any]]:
        selected_language=language.strip().upper()
        repository=getattr(self,"repository",None)
        cached=repository.get_category_attributes_cache(category_id,type_id,selected_language) if repository else None
        if cached is not None: return cached
        data = self.post("/v1/description-category/attribute", {
            "description_category_id": int(category_id), "type_id": int(type_id), "language": selected_language,
        })
        result = data.get("result", [])
        if isinstance(result, dict):
            result = result.get("attributes") or result.get("items") or []
        items=[item for item in (result or []) if isinstance(item, dict)]
        if repository:repository.save_category_attributes_cache(category_id,type_id,selected_language,items)
        return items

    def category_attribute_values(self, category_id: int, type_id: int, attribute_id: int, language: str = "ZH_HANS") -> list[dict[str, Any]]:
        selected_language=language.strip().upper()
        repository=getattr(self,"repository",None)
        cached=repository.get_attribute_values_cache(category_id,type_id,attribute_id,selected_language) if repository else None
        if cached is not None:return cached
        values: list[dict[str, Any]] = []
        last_value_id = 0
        seen: set[int] = set()
        while True:
            data = self.post("/v1/description-category/attribute/values", {
                "attribute_id": int(attribute_id), "description_category_id": int(category_id),
                "type_id": int(type_id), "language": selected_language, "last_value_id": last_value_id,
                "limit": 5000,
            })
            raw_result = data.get("result")
            if isinstance(raw_result, list):
                page = [item for item in raw_result if isinstance(item, dict)]
                has_next = bool(data.get("has_next", False))
                next_id = int(data.get("last_value_id") or (page[-1].get("id") if page else 0) or 0)
            else:
                result = raw_result if isinstance(raw_result, dict) else data
                page = [item for item in (result.get("values") or result.get("items") or []) if isinstance(item, dict)]
                has_next = bool(result.get("has_next", False))
                next_id = int(result.get("last_value_id") or (page[-1].get("id") if page else 0) or 0)
            values.extend(page)
            if not has_next or not page or not next_id or next_id in seen:
                if repository:repository.save_attribute_values_cache(category_id,type_id,attribute_id,selected_language,values)
                return values
            seen.add(next_id)
            last_value_id = next_id

    def warehouses(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
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
            values.extend(item for item in (page or []) if isinstance(item, dict))
            next_cursor = str(envelope.get("cursor") or data.get("cursor") or "")
            if not envelope.get("has_next", data.get("has_next", False)) or not next_cursor or next_cursor in seen:
                return values
            seen.add(next_cursor)
            cursor = next_cursor

    def products(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        last_id = ""
        while True:
            body: dict[str, Any] = {"filter": {"visibility": "ALL"}, "limit": 1000}
            if last_id:
                body["last_id"] = last_id
            data = self.post("/v3/product/list", body)
            result = data.get("result") if isinstance(data.get("result"), dict) else {}
            page = [item for item in (result.get("items") or []) if isinstance(item, dict)]
            values.extend(page)
            next_id = str(result.get("last_id") or "")
            if not page or len(values) >= int(result.get("total") or len(values)) or not next_id or next_id == last_id:
                return values
            last_id = next_id

    def product_info(self, skus: list[str]) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for start in range(0, len(skus), 1000):
            data = self.post("/v3/product/info/list", {"offer_id": [], "product_id": [], "sku": skus[start:start + 1000]})
            values.extend(item for item in (data.get("items") or []) if isinstance(item, dict))
        return values

    def product_ratings(self, skus: list[str]) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for start in range(0, len(skus), 100):
            data = self.post("/v1/product/rating-by-sku", {"skus": skus[start:start + 100]})
            values.extend(item for item in (data.get("products") or []) if isinstance(item, dict))
        return values

    def analytics_by_sku(self, date_from: str, date_to: str) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        offset = 0
        metrics = ["ordered_units", "revenue", "cancellations", "returns", "delivered_units"]
        while True:
            data = self.post("/v1/analytics/data", {
                "date_from": date_from,
                "date_to": date_to,
                "metrics": metrics,
                "dimension": ["sku"],
                "filters": [],
                "sort": [],
                "limit": 1000,
                "offset": offset,
            })
            result = data.get("result") if isinstance(data.get("result"), dict) else {}
            page = [item for item in (result.get("data") or []) if isinstance(item, dict)]
            for item in page:
                item["metric_names"] = metrics
            values.extend(page)
            if len(page) < 1000:
                return values
            offset += len(page)

    def import_product(self, item: dict[str, Any]) -> int:
        data = self.post("/v3/product/import", {"items": [item]}, product_create=True)
        try:
            return int(data["result"]["task_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise OzonImportUncertain("Ozon 创建商品响应缺少 task_id，必须先核对原结果") from error

    def import_info(self, task_id: int) -> dict[str, Any]:
        return self.post("/v1/product/import/info", {"task_id": int(task_id)})

    def wait_import(self, task_id: int, *, attempts: int = 30, interval: float = 3) -> dict[str, Any]:
        last: dict[str, Any] = {}
        for attempt in range(max(1, attempts)):
            last = self.import_info(task_id)
            items = import_items(last)
            statuses = {str(item.get("status") or "").casefold() for item in items}
            if any(blocking_import_errors(item) for item in items) or statuses & {"imported", "failed", "error"}:
                return last
            if attempt + 1 < attempts:
                time.sleep(min(30, interval * (2 ** min(attempt, 4))))
        return last

    def update_stock(self, offer_id: str, warehouse_id: int, stock: int) -> dict[str, Any]:
        return self.post("/v2/products/stocks", {"stocks": [{
            "offer_id": offer_id, "warehouse_id": int(warehouse_id), "stock": int(stock),
        }]})

    def promotions(self) -> list[dict[str, Any]]:
        try:
            response = self.session.get(
                self.api_base + "/v1/actions",
                headers={"Client-Id": self.client_id, "Api-Key": self.api_key, "Accept": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as error:
            raise OzonApiError(f"读取 Ozon 促销失败：{error}") from error
        result = data.get("result", data.get("items", [])) if isinstance(data, dict) else []
        if isinstance(result, dict):
            result = result.get("actions") or result.get("items") or []
        return [item for item in (result or []) if isinstance(item, dict)]

    def promotion_candidates(self, action_id: int, limit: int = 100, last_id: str = "") -> dict[str, Any]:
        if not 1 <= int(limit) <= 100:
            raise OzonApiError("促销候选商品每页最多100件")
        return self.post("/v2/actions/candidates", {"action_id": int(action_id), "limit": int(limit), "last_id": str(last_id or "")})

    def find_promotion_candidate(self, action_id: int, product_id: int) -> dict[str, Any] | None:
        last_id = ""; seen = set()
        for _ in range(100):
            data = self.promotion_candidates(action_id, 100, last_id)
            result = data.get("result", data)
            items = result.get("products") or []
            for item in items:
                if str(item.get("id") or item.get("product_id")) == str(product_id): return item
            cursor = str(result.get("last_id") or "")
            if not items or not cursor: return None
            if cursor in seen: raise OzonApiError("活动候选商品游标重复，停止重复查询")
            seen.add(cursor); last_id = cursor
        raise OzonApiError("活动资格超过100页，停止查询")

    def update_promotions(self, action_id: int, products: list[dict[str, Any]]) -> dict[str, Any]:
        if not 1 <= len(products) <= 1000:
            raise OzonApiError("促销批次必须为1到1000件")
        return self.post("/v1/actions/products/update", {"action_id": int(action_id), "products": [
            {"product_id": int(p["product_id"]), "action_price": {"amount": str(Decimal(str(p["action_price"]))),
             "currency": str(p.get("currency") or self.currency)},
             **({"stock": str(int(p["stock"]))} if p.get("stock") is not None else {})} for p in products]})

    def activate_promotion(self, action_id: int, product_id: int, action_price: Any, stock: int | None = None) -> dict[str, Any]:
        response = self.update_promotions(action_id, [{"product_id": product_id, "action_price": action_price, "stock": stock}])
        result = response.get("result", response)
        accepted = result.get("active_product_ids", result.get("product_ids", []))
        removed = result.get("deactivated_product_ids", [])
        if str(product_id) in {str(x) for x in removed} or str(product_id) not in {str(x) for x in accepted}:
            raise OzonApiError("平台没有确认加入活动（可能已移除或拒绝），请核对价格和回执")
        return response



def import_items(value: dict[str, Any]) -> list[dict[str, Any]]:
    result = value.get("result", {})
    items = result.get("items", []) if isinstance(result, dict) else []
    return [item for item in (items or []) if isinstance(item, dict)]


def blocking_import_errors(item: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        error for error in (item.get("errors") or [])
        if isinstance(error, dict) and str(error.get("level") or "").casefold() != "warning"
    ]


def import_succeeded(value: dict[str, Any]) -> bool:
    items = import_items(value)
    return bool(items) and all(str(item.get("status") or "").casefold() == "imported" and not blocking_import_errors(item) for item in items)


def import_failed(value: dict[str, Any]) -> bool:
    return any(
        re.fullmatch(r"failed|error", str(item.get("status") or ""), re.I) or blocking_import_errors(item)
        for item in import_items(value)
    )
