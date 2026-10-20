"""Machine-wide AI request limits, shared by threads and local worker processes."""
from __future__ import annotations

import asyncio
import contextvars
import os
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from erp.core.config import get_settings

DEFAULT_LIMITS = {"text": 5, "image": 3}
_cancel = contextvars.ContextVar("ai_cancel", default=None)


def validate_limits(value):
    if not isinstance(value, dict) or set(value) != {"text", "image"}:
        raise ValueError("请同时设置文字和生图并发数")
    for key, maximum in (("text", 16), ("image", 8)):
        if type(value[key]) is not int or not 1 <= value[key] <= maximum:
            raise ValueError(f"{key} 并发数必须是 1–{maximum} 的整数")
    return dict(value)


def limits():
    from erp.core.database import SessionLocal
    from erp.core.jsonutil import loads
    from erp.models import SystemSetting
    with SessionLocal() as session:
        row = session.get(SystemSetting, "ai_concurrency")
        return validate_limits(loads(row.value_json, {})) if row else dict(DEFAULT_LIMITS)


def _root():
    root = get_settings().data_dir / "runtime" / "ai-slots"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _try_lock(path: Path):
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"0"); handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _check_cancel():
    event = _cancel.get()
    if event is not None and event.is_set():
        raise RuntimeError("AI 批次已停止，不再发起新请求")


@contextmanager
def cancellation(event):
    token = _cancel.set(event)
    try:
        yield
    finally:
        _cancel.reset(token)


def _try_slot(kind, limit):
    _check_cancel()
    root = _root()
    cooldown = root / f"{kind}.cooldown"
    try:
        if float(cooldown.read_text()) > time.time():
            return None
    except (OSError, ValueError):
        pass
    for index in range(limit):
        handle = _try_lock(root / f"{kind}-{index}.lock")
        if handle is not None:
            return handle
    return None


def _rate_limited(kind, response):
    status = getattr(response, "status_code", None) or getattr(response, "status", None)
    if status != 429:
        return
    try:
        delay = max(10, min(300, float(response.headers.get("Retry-After", 10))))
    except (TypeError, ValueError):
        delay = 10
    # Atomic replace keeps async readers from observing a partial timestamp.
    root = _root()
    import uuid
    temporary = root / f"{kind}-{uuid.uuid4().hex}.tmp"
    temporary.write_text(str(time.time() + delay))
    os.replace(temporary, root / f"{kind}.cooldown")


@contextmanager
def slot(kind):
    handle = None
    last_read = 0.0
    while handle is None:
        if time.monotonic() - last_read > 1:
            limit = limits()[kind]; last_read = time.monotonic()
        handle = _try_slot(kind, limit)
        if handle is None:
            time.sleep(.1)
    try:
        _check_cancel()
        yield
    finally:
        handle.close()  # OS releases the lock, including after a process crash.


def post(kind, sender, *args, **kwargs):
    with slot(kind):
        from erp.core import ai_usage
        identity = ai_usage.begin(kind, args[0] if args else kwargs.get("url", ""), kwargs)
        try:
            response = sender(*args, **kwargs)
        except BaseException as error:
            ai_usage.finish(identity, error=error)
            raise
        try:
            body = response.json()
        except Exception:
            body = None
        ai_usage.finish(identity, response, body)
        _rate_limited(kind, response)
        return response


@asynccontextmanager
async def async_post(kind, session, *args, **kwargs):
    from erp.core import ai_usage
    model_hint = kwargs.pop("usage_model", "")
    handle = None
    last_read = 0.0
    while handle is None:
        if time.monotonic() - last_read > 1:
            limit = limits()[kind]; last_read = time.monotonic()
        handle = _try_slot(kind, limit)
        if handle is None:
            await asyncio.sleep(.1)
    try:
        identity = ai_usage.begin(kind, args[0] if args else kwargs.get("url", ""), kwargs, model_hint)
        response = None
        recorded = False
        try:
            async with session.post(*args, **kwargs) as response:
                try:
                    body = await response.json(content_type=None)
                except (ValueError, TypeError, AttributeError):
                    body = None
                ai_usage.finish(identity, response, body)
                recorded = True
                _rate_limited(kind, response)
                yield response  # aiohttp caches body for the caller.
        except BaseException as error:
            if not recorded:
                ai_usage.finish(identity, response=response, error=error)
            raise
    finally:
        handle.close()
