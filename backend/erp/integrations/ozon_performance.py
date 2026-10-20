from __future__ import annotations

import io
import csv
import hashlib
import json
import os
import re
import time
import zipfile
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import requests

from erp.core.errors import AppError


class OzonPerformanceClient:
    base_url = "https://api-performance.ozon.ru"

    def __init__(self, client_id: str, client_secret: str, *, timeout: int = 60) -> None:
        if not client_id or not client_secret:
            raise AppError("店铺缺少 Ozon 广告 API 凭据", code="performance_credentials_missing")
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "RussiaCrossborderERP/0.1"})
        self._access_token = ""

    def _token(self) -> str:
        if self._access_token:
            return self._access_token
        response = self.session.post(
            f"{self.base_url}/api/client/token",
            json={"client_id": self.client_id, "client_secret": self.client_secret, "grant_type": "client_credentials"},
            timeout=self.timeout,
        )
        data = self._json(response, "Ozon 广告 API 鉴权失败")
        token = str(data.get("access_token") or "")
        if not token:
            raise AppError("Ozon 广告 API 未返回访问令牌", code="performance_auth_failed", status_code=502)
        self._access_token = token
        return token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}

    @staticmethod
    def _json(response: requests.Response, fallback: str) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            if response.status_code == 204:
                return {}
            raise AppError(fallback, code="performance_response_invalid", status_code=502) from None
        if not isinstance(data, dict):
            raise AppError(fallback, code="performance_response_invalid", status_code=502)
        if not response.ok:
            message = str(data.get("error") or data.get("message") or fallback)
            raise AppError(message, code="performance_api_error", status_code=502, details={"upstream_status": response.status_code})
        return data

    def campaigns(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self.session.get(
                f"{self.base_url}/api/client/campaign",
                headers=self._headers(),
                params={"page": page, "pageSize": 100},
                timeout=self.timeout,
            )
            data = self._json(response, "无法读取 Ozon 广告活动")
            items = data.get("list") or []
            if not isinstance(items, list):
                items = []
            result.extend(item for item in items if isinstance(item, dict))
            if len(result) >= int(data.get("total") or len(result)) or len(items) < 100:
                return result
            page += 1

    def campaign_products(self, campaign_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self.session.get(
                f"{self.base_url}/api/client/campaign/{campaign_id}/v2/products",
                headers=self._headers(),
                params={"page": page, "pageSize": 1000},
                timeout=self.timeout,
            )
            data = self._json(response, "无法读取 Ozon 广告活动商品")
            items = data.get("products")
            if not isinstance(items, list) or any(not isinstance(item, dict) or not item.get("sku") for item in items):
                raise AppError("广告商品响应格式不完整，无法确认执行结果", code="performance_response_invalid", status_code=502)
            result.extend(item for item in items if isinstance(item, dict))
            if len(items) < 1000:
                return result
            page += 1

    def delete_campaign_products(self, campaign_id: str, sku_ids: list[str]) -> dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}/api/client/campaign/{campaign_id}/products/delete",
            headers=self._headers(),
            json={"sku": [str(sku_id) for sku_id in sku_ids]},
            timeout=self.timeout,
        )
        return self._json(response, "无法从 Ozon 广告计划移除商品")

    def update_campaign_products(self, campaign_id: str, products: list[dict[str, Any]]) -> dict[str, Any]:
        response = self.session.put(
            f"{self.base_url}/api/client/campaign/{campaign_id}/products",
            headers=self._headers(),
            json={"bids": products},
            timeout=self.timeout,
        )
        return self._json(response, "无法更新 Ozon 广告商品目标费用")

    def update_campaign(self, campaign_id: str, values: dict[str, Any]) -> dict[str, Any]:
        response = self.session.patch(
            f"{self.base_url}/api/client/campaign/{campaign_id}",
            headers=self._headers(),
            json=values,
            timeout=self.timeout,
        )
        return self._json(response, "无法更新 Ozon 广告计划")

    def submit_report(self, campaign_ids: list[str], date_from: date, date_to: date, *, wait_seconds: int = 180) -> str:
        deadline = time.monotonic() + wait_seconds
        while True:
            response = self.session.post(
                f"{self.base_url}/api/client/statistics",
                headers=self._headers(),
                json={
                    "campaigns": campaign_ids,
                    "dateFrom": date_from.isoformat(),
                    "dateTo": date_to.isoformat(),
                    "groupBy": "DATE",
                },
                timeout=self.timeout,
            )
            try:
                data = response.json()
            except ValueError:
                data = {}
            message = str(data.get("error") or data.get("message") or "")
            if not response.ok and "лимит активных запросов" in message.lower() and time.monotonic() < deadline:
                time.sleep(3)
                continue
            data = self._json(response, "无法创建 Ozon 广告统计报表")
            report_id = str(data.get("UUID") or "")
            if not report_id:
                raise AppError("Ozon 未返回广告报表编号", code="performance_report_missing", status_code=502)
            return report_id

    def wait_report(self, report_id: str, *, timeout_seconds: int = 180) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            response = self.session.get(
                f"{self.base_url}/api/client/statistics/{report_id}",
                headers=self._headers(),
                timeout=self.timeout,
            )
            data = self._json(response, "无法查询 Ozon 广告报表状态")
            state = str(data.get("state") or "").upper()
            if state == "OK":
                return data
            if state in {"ERROR", "FAILED"}:
                raise AppError(str(data.get("error") or "Ozon 广告报表生成失败"), code="performance_report_failed", status_code=502)
            time.sleep(2)
        raise AppError("等待 Ozon 广告报表超时", code="performance_report_timeout", status_code=504)

    def download_report(self, report_id: str) -> tuple[bytes, str]:
        response = self.session.get(
            f"{self.base_url}/api/client/statistics/report",
            headers=self._headers(),
            params={"UUID": report_id},
            timeout=self.timeout,
        )
        if not response.ok:
            self._json(response, "无法下载 Ozon 广告报表")
        return response.content, response.headers.get("Content-Type", "application/octet-stream")

    def report_files(self, campaign_ids: list[str], date_from: date, date_to: date, *, checkpoint_dir: Path | None = None) -> Iterator[tuple[str, bytes]]:
        if checkpoint_dir is not None:
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
        for start in range(0, len(campaign_ids), 10):
            batch = campaign_ids[start:start + 10]
            signature = hashlib.sha256(json.dumps([batch, date_from.isoformat(), date_to.isoformat()], separators=(",", ":")).encode()).hexdigest()
            checkpoint = checkpoint_dir / f"{start:05d}.json" if checkpoint_dir is not None else None
            data_path = checkpoint_dir / f"{start:05d}.bin" if checkpoint_dir is not None else None
            saved = {}
            if checkpoint and checkpoint.is_file():
                try:
                    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    saved = {}
            valid_checkpoint = saved.get("signature") == signature
            if not valid_checkpoint:
                saved = {}
            report_id = str(saved.get("report_id") or "")
            if not report_id:
                report_id = self.submit_report(batch, date_from, date_to, wait_seconds=600)
                saved = {"signature": signature, "report_id": report_id}
                if checkpoint:
                    temporary = checkpoint.with_suffix(".tmp")
                    temporary.write_text(json.dumps(saved), encoding="utf-8")
                    os.replace(temporary, checkpoint)
            if valid_checkpoint and data_path and data_path.is_file():
                content = data_path.read_bytes()
                content_type = str(saved.get("content_type") or "")
            else:
                self.wait_report(report_id, timeout_seconds=600)
                content, content_type = self.download_report(report_id)
                if data_path and checkpoint:
                    temporary_data = data_path.with_suffix(".tmp")
                    temporary_data.write_bytes(content)
                    os.replace(temporary_data, data_path)
                    saved["content_type"] = content_type
                    temporary = checkpoint.with_suffix(".tmp")
                    temporary.write_text(json.dumps(saved), encoding="utf-8")
                    os.replace(temporary, checkpoint)
            if content.startswith(b"PK") or "zip" in content_type.lower():
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    for name in archive.namelist():
                        if not name.endswith("/"):
                            yield name, archive.read(name)
            else:
                yield f"{batch[0]}.csv", content

    @staticmethod
    def parse_report(content: bytes) -> list[dict[str, Any]]:
        text = content.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text), delimiter=";"))
        if len(rows) < 2:
            return []
        campaign_match = re.search(r"№\s*(\d+)", " ".join(rows[0]))
        campaign_id = campaign_match.group(1) if campaign_match else ""
        header = {name.strip(): index for index, name in enumerate(rows[1])}

        def value(row: list[str], name: str) -> str:
            index = header.get(name)
            return row[index].strip() if index is not None and index < len(row) else ""

        def number(raw: str, *, integer: bool = False) -> int | float:
            parsed = float(raw.replace(" ", "").replace("\xa0", "").replace(",", ".") or 0)
            return int(parsed) if integer else parsed

        result: list[dict[str, Any]] = []
        for row in rows[2:]:
            sku = value(row, "sku")
            raw_date = value(row, "День")
            if not sku or not raw_date:
                continue
            metric_date = date.fromisoformat("-".join(reversed(raw_date.split("."))))
            direct_sold_qty = number(value(row, "Продано товаров"), integer=True)
            model_sold_qty = number(value(row, "Продано товаров модели"), integer=True)
            direct_ad_sales = number(value(row, "Продажи в продвижении, ₽"))
            model_ad_sales = number(value(row, "Продажи в продвижении с заказов модели, ₽"))
            result.append({
                "campaign_id": campaign_id,
                "metric_date": metric_date,
                "sku_id": sku,
                "name": value(row, "Название товара"),
                "price": number(value(row, "Предельная цена, ₽")),
                "impressions": number(value(row, "Показы"), integer=True),
                "clicks": number(value(row, "Клики"), integer=True),
                "add_to_cart": number(value(row, "Добавления в корзину"), integer=True),
                "avg_cpc": number(value(row, "Средняя стоимость клика, ₽")),
                "ad_spend": number(value(row, "Расход, ₽")),
                "direct_sold_qty": direct_sold_qty,
                "model_sold_qty": model_sold_qty,
                "sold_qty": direct_sold_qty + model_sold_qty,
                "direct_ad_sales": direct_ad_sales,
                "model_ad_sales": model_ad_sales,
                "ad_sales": direct_ad_sales + model_ad_sales,
                "ad_start_date": value(row, "Дата добавления"),
            })
        return result
