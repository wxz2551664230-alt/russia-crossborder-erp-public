"""Durable request ledger, independent of long-running ERP write transactions.

Never stores prompts, responses, authorization headers or URL query strings.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

from erp.core.config import get_settings

scope = contextvars.ContextVar("ai_usage_scope", default={})


@contextmanager
def task_scope(job_id, job_type, attempt):
    token = scope.set(dict(job_id=job_id, job_type=job_type, attempt=attempt))
    try:
        yield
    finally:
        scope.reset(token)


@contextmanager
def connect():
    path = get_settings().data_dir / "ai-usage.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(20):
        db = sqlite3.connect(path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
        id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
        job_id TEXT NOT NULL, job_type TEXT NOT NULL, attempt INTEGER NOT NULL,
        kind TEXT NOT NULL, host TEXT NOT NULL, model TEXT NOT NULL,
        status TEXT NOT NULL, http_status INTEGER, input_tokens INTEGER,
        output_tokens INTEGER, cached_tokens INTEGER, images INTEGER,
        price_json TEXT NOT NULL, estimated_cost TEXT, currency TEXT,
        request_id TEXT, error_type TEXT)""")
            db.execute("CREATE INDEX IF NOT EXISTS usage_job ON requests(job_id, started_at)")
            break
        except sqlite3.OperationalError as error:
            db.close()
            if 'locked' not in str(error).lower() or attempt == 19:
                raise
            time.sleep(.1)
        except BaseException:
            db.close()
            raise
    try:
        with db:
            yield db
    finally:
        db.close()


def validate_pricing(value):
    if not isinstance(value, list):
        raise ValueError("AI 单价必须是配置列表")
    seen = set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("单价配置必须是对象")
        key = (row.get("host"), row.get("kind"), row.get("model"))
        if not all(isinstance(v, str) and v for v in key) or key[1] not in ("text", "image") or key in seen:
            raise ValueError("请填写唯一的服务域名、文字/图片类型及模型")
        seen.add(key)
        if row.get("currency") not in ("CNY", "USD"):
            raise ValueError("币种应为 CNY 或 USD")
        mode = row.get("mode")
        fields = {"per_request": ["rate"], "per_image": ["rate"], "per_million_tokens": ["input", "output"]}.get(mode)
        if fields is None:
            raise ValueError("计价方式应为按请求、按图片或按百万 token")
        for field in fields + (["cached_input"] if "cached_input" in row else []):
            try:
                number = Decimal(str(row[field]))
                if not number.is_finite() or number < 0:
                    raise ValueError()
            except Exception as error:
                raise ValueError("单价必须是非负有限数值") from error
    return value


def begin(kind, url, kwargs, model_hint=""):
    from erp.core.database import SessionLocal
    from erp.core.jsonutil import loads
    from erp.models import SystemSetting
    body = kwargs.get("json") or kwargs.get("data") or {}
    model = str(body.get("model", "")) if isinstance(body, dict) else model_hint
    host = (urlsplit(str(url)).hostname or "").lower()
    with SessionLocal() as session:
        setting = session.get(SystemSetting, "ai_pricing")
        prices = loads(setting.value_json, []) if setting else []
    price = next((p for p in prices if (p["host"], p["kind"], p["model"]) == (host, kind, model)), {})
    identity = uuid.uuid4().hex
    task = scope.get()
    with connect() as db:
        db.execute("""INSERT INTO requests
            (id,started_at,job_id,job_type,attempt,kind,host,model,status,price_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (identity, datetime.now(timezone.utc).isoformat(),
            task.get("job_id", ""), task.get("job_type", "unassigned"), task.get("attempt", 0),
            kind, host, model, "in_flight", json.dumps(price)))
    return identity


def integer(value):
    return value if type(value) is int and value >= 0 else None


def finish(identity, response=None, body=None, error=None):
    # Accounting failures must never turn a successful paid request into a retry.
    try:
        status = getattr(response, "status_code", None) or getattr(response, "status", None)
        valid_body = isinstance(body, dict)
        body = body if valid_body else {}
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        incoming = integer(usage.get("input_tokens", usage.get("prompt_tokens")))
        outgoing = integer(usage.get("output_tokens", usage.get("completion_tokens")))
        details = usage.get("input_tokens_details", usage.get("prompt_tokens_details", {})) or {}
        cached = integer(details.get("cached_tokens", 0)) if isinstance(details, dict) else None
        images = sum(isinstance(item, dict) and bool(item.get("url") or item.get("b64_json")) for item in body["data"]) if isinstance(body.get("data"), list) else None
        result = "http_error" if status and not 200 <= status < 300 else "unknown" if error or not status or not valid_body else "response_error" if "error" in body else "success"
        with connect() as db:
            row = db.execute("SELECT price_json FROM requests WHERE id=?", (identity,)).fetchone()
            price = json.loads(row[0])
            cost = None
            if result == "success" and price:
                mode = price["mode"]
                if mode == "per_request":
                    cost = Decimal(str(price["rate"]))
                elif mode == "per_image" and images:
                    cost = Decimal(str(price["rate"])) * images
                elif mode == "per_million_tokens" and incoming is not None and outgoing is not None and cached is not None and cached <= incoming:
                    if not cached or "cached_input" in price:
                        cost = (Decimal(str(price["input"])) * (incoming - cached) + Decimal(str(price["output"])) * outgoing + Decimal(str(price.get("cached_input", 0))) * cached) / 1000000
            headers = getattr(response, "headers", {}) or {}
            db.execute("""UPDATE requests SET finished_at=?,status=?,http_status=?,input_tokens=?,
                output_tokens=?,cached_tokens=?,images=?,estimated_cost=?,currency=?,request_id=?,error_type=? WHERE id=?""",
                (datetime.now(timezone.utc).isoformat(), result, status, incoming, outgoing, cached, images,
                 str(cost) if cost is not None else None, price.get("currency"),
                 str(headers.get("x-request-id", ""))[:200], type(error).__name__ if error else "", identity))
    except Exception:
        logging.getLogger(__name__).exception("AI usage finalization failed; request remains unconfirmed: %s", identity)


def report(job_ids):
    with connect() as db:
        rows = db.execute("SELECT * FROM requests WHERE job_id IN (%s) ORDER BY started_at" % ",".join("?" for _ in job_ids), job_ids).fetchall() if job_ids else []
    groups = {}
    for row in rows:
        item = groups.setdefault(row["job_id"], dict(job_id=row["job_id"], text=0, image=0, success=0, errors=0, pending=0, unknown_cost=0, input_tokens=0, output_tokens=0, images=0, missing_usage=0, costs={}, models=[], by_kind={kind: {"costs": {}, "unknown_cost": 0, "calls": 0} for kind in ("text", "image")}))
        item[row["kind"]] += 1
        item["by_kind"][row["kind"]]["calls"] += 1
        item["success" if row["status"] == "success" else "pending" if row["status"] in ("in_flight", "unknown") else "errors"] += 1
        for field in ("input_tokens", "output_tokens"):
            item[field] += row[field] or 0
        item["missing_usage"] += int(row["input_tokens"] is None or row["output_tokens"] is None)
        item["images"] += row["images"] or 0
        model = f'{row["host"]} / {row["model"] or "模型未返回"}'
        if model not in item["models"]:
            item["models"].append(model)
        if row["estimated_cost"] is None:
            item["unknown_cost"] += 1
            item["by_kind"][row["kind"]]["unknown_cost"] += 1
        else:
            currency = row["currency"]
            item["costs"][currency] = str(Decimal(item["costs"].get(currency, "0")) + Decimal(row["estimated_cost"]))
            costs = item["by_kind"][row["kind"]]["costs"]
            costs[currency] = str(Decimal(costs.get(currency, "0")) + Decimal(row["estimated_cost"]))
    return {"items": list(groups.values()), "coverage": "仅包含启用账本后的实际请求；历史未记录不等于零调用。费用为请求时单价估算，不是平台实际账单。"}
