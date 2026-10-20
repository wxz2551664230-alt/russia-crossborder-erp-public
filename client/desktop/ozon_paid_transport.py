"""Durable paid-call boundary for one Ozon listing and one store on a PC.

The original ai_listing.ai_post function can be wrapped with paid_transport.
Completed calls replay only within this job/store; failed or uncertain calls retry under the user-authorized bounded policy. No API keys or prompts are written to the journal.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

import requests


class PaidCallUnknown(ValueError):
    code = 'ai_write_outcome_unknown'


def _file_digest(value: object) -> dict:
    stream = value[1] if isinstance(value, tuple) and len(value) > 1 else value
    if not hasattr(stream, 'read') or not hasattr(stream, 'tell') or not hasattr(stream, 'seek'):
        raise ValueError('AI 图片请求包含不可检查的文件流')
    offset = stream.tell()
    digest = hashlib.sha256()
    size = 0
    try:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    finally:
        stream.seek(offset)
    return {'sha256': digest.hexdigest(), 'bytes': size,
            'name': str(value[0]) if isinstance(value, tuple) else ''}


def _fingerprint(scope: str, kind: str, url: str, kwargs: dict) -> str:
    files = kwargs.get('files') or {}
    if not isinstance(files, dict):
        raise ValueError('AI 图片文件格式无效')
    payload = {'scope': scope, 'kind': kind, 'url': url,
               'json': kwargs.get('json'), 'data': kwargs.get('data'),
               'files': {name: _file_digest(value) for name, value in sorted(files.items())}}
    # Authorization headers and access tokens are intentionally excluded.
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _atomic(path: Path, value: dict) -> None:
    temporary = path.with_name(uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def paid_transport(original, directory: Path, *, scope: str, lease):
    """Return an ai_post-compatible function with durable per-call receipts."""
    if not scope or any(char in scope for char in '/\\:'):
        raise ValueError('Ozon 店铺检查点范围无效')
    directory.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    sequence: dict[str, int] = {}

    def unlocked_request(kind: str, send, url: str, **kwargs):
        signature = _fingerprint(scope, kind, url, kwargs)
        with lock:
            index = sequence.get(signature, 0)
            sequence[signature] = index + 1
        path = directory / f'{signature}-{index}.json'
        lease()
        try:
            # Exclusive creation is the durable boundary before the paid call.
            # Two local worker processes must never both decide to send it.
            with path.open('x', encoding='utf-8') as stream:
                stream.write('{"state":"pending"}')
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            try:
                receipt = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as error:
                raise PaidCallUnknown('AI 请求检查点尚未确认，禁止自动重发') from error
            if receipt.get('state') == 'done' and receipt.get('status', 500) < 400:
                response = requests.Response()
                response.status_code = receipt['status']
                response._content = base64.b64decode(receipt['body'])
                response.url = url
                response.request = requests.Request('POST', url).prepare()
                return response
            # User-authorized retry policy. Preserve the previous uncertain
            # checkpoint before a new attempt; the per-call OS lock prevents
            # concurrent workers from sending the same request.
            _atomic(directory / ('previous-' + uuid.uuid4().hex + '.json'), receipt)
            _atomic(path, {'state': 'pending'})
        sender_entered = False
        def guarded_sender(*args, **parameters):
            nonlocal sender_entered
            sender_entered = True
            response = send(*args, **parameters)
            # Persist the platform response before usage accounting or rate-limit
            # bookkeeping can fail after an otherwise successful paid call.
            if response.status_code < 400:
                _atomic(path, {'state': 'done', 'status': response.status_code,
                              'body': base64.b64encode(response.content).decode('ascii')})
            return response
        for attempt in range(3):
            sender_entered = False
            try:
                response = original(kind, guarded_sender if callable(send) else send, url, **kwargs)
                if response.status_code >= 400:
                    raise PaidCallUnknown('AI 服务响应结果需核对')
                _atomic(path, {'state': 'done', 'status': response.status_code,
                              'body': base64.b64encode(response.content).decode('ascii')})
                return response
            except Exception as error:
                receipt = json.loads(path.read_text(encoding='utf-8'))
                if receipt.get('state') == 'done' and receipt.get('status', 500) < 400:
                    response = requests.Response()
                    response.status_code = receipt['status']
                    response._content = base64.b64decode(receipt['body'])
                    response.url = url
                    return response
                if callable(send) and not sender_entered:
                    # The real network sender provably did not run. Retry only
                    # within this process while it still owns the reservation.
                    if attempt < 2:
                        lease(); time.sleep(2 ** attempt); continue
                    _atomic(path, {'state': 'not_sent'})
                    raise ValueError('AI 请求尚未发送，自动恢复3次仍未成功，请检查本机配置') from error
                if attempt < 2:
                    lease(); time.sleep(2 ** attempt); continue
                raise PaidCallUnknown('AI 调用自动重试3次仍未成功，请检查服务商状态') from error

    def request(kind, send, url, **kwargs):
        key = _fingerprint(scope, kind, url, kwargs)
        lock_path = directory / (key + '.lock')
        with lock_path.open('a+b') as handle:
            handle.seek(0); handle.write(b'0'); handle.flush(); handle.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise PaidCallUnknown('同一 AI 请求正在执行，等待原进程完成') from error
            try:
                return unlocked_request(kind, send, url, **kwargs)
            finally:
                handle.seek(0)
                if os.name == 'nt': msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return request
