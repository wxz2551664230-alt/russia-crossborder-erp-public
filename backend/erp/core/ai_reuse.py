"""Exact-input reuse of validated AI results; never cache errors."""
import hashlib
import asyncio
import json
import os
import time
import uuid
from contextlib import contextmanager, asynccontextmanager

from erp.core.config import get_settings


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Task/store directories are deep on Windows. Appending to the full
    # fingerprint filename can exceed MAX_PATH after a paid response arrives.
    temporary = path.with_name(uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def result_cache(namespace, key, timeout=240):
    from erp.core.ai_concurrency import _try_lock, _check_cancel
    root = get_settings().data_dir / 'ai-results' / namespace
    root.mkdir(parents=True, exist_ok=True)
    handle = None
    deadline = time.monotonic() + timeout
    while handle is None:
        _check_cancel()
        handle = _try_lock(root / (key + '.lock'))
        if handle is None:
            if time.monotonic() >= deadline:
                raise TimeoutError('相同内容正在生成，稍后重试以复用结果')
            time.sleep(.1)
    try:
        yield root / (key + '.json')
    finally:
        handle.close()


@asynccontextmanager
async def async_result_cache(namespace, key):
    from erp.core.ai_concurrency import _try_lock, _check_cancel
    root = get_settings().data_dir / 'ai-results' / namespace
    root.mkdir(parents=True, exist_ok=True)
    handle = None
    deadline = time.monotonic() + 660
    while handle is None:
        _check_cancel()
        handle = _try_lock(root / (key + '.lock'))
        if handle is None:
            if time.monotonic() >= deadline:
                raise TimeoutError('相同图片正在生成，请稍后恢复任务')
            await asyncio.sleep(.1)
    try:
        yield root / (key + '.json')
    finally:
        handle.close()
