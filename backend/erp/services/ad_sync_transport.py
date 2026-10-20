"""Small resumable chunks for PC advertisement sync results."""
import base64
import binascii
import gzip
import hashlib
import io
import json
import os
import re
from pathlib import Path

from erp.core.config import get_settings
from erp.core.errors import ConflictError

CHUNK_BYTES = 40 * 1024
MAX_PACKED = 128 * 1024 * 1024
MAX_JSON = 512 * 1024 * 1024


def _directory(job_id: str) -> Path:
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise ConflictError('广告同步任务编号无效')
    return get_settings().data_dir / 'ad_sync_uploads' / job_id


def store_chunk(job_id: str, *, index: int, total: int,
                digest: str, encoded: str) -> dict:
    if (not 0 <= index < total <= 4000
            or not re.fullmatch(r'[a-f0-9]{64}', digest)):
        raise ConflictError('广告同步分片编号或校验值无效')
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ConflictError('广告同步分片编码无效') from error
    if not content or len(content) > CHUNK_BYTES:
        raise ConflictError('广告同步分片超过大小限制')
    directory = _directory(job_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / 'manifest.json'
    expected = {'total': total, 'sha256': digest}
    try:
        with manifest.open('x', encoding='ascii') as target:
            json.dump(expected, target)
    except FileExistsError:
        try:
            if json.loads(manifest.read_text(encoding='ascii')) != expected:
                raise ConflictError('广告同步分片与已有上传不一致')
        except (OSError, ValueError) as error:
            raise ConflictError('广告同步分片记录损坏') from error
    part = directory / f'{index:04d}.part'
    try:
        with part.open('xb') as target:
            target.write(content)
    except FileExistsError:
        if part.read_bytes() != content:
            raise ConflictError('广告同步同序号分片内容不一致')
    return {'index': index, 'received': True}


def read_result(job_id: str, *, digest: str) -> tuple[dict, int]:
    directory = _directory(job_id)
    try:
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='ascii'))
    except (OSError, ValueError) as error:
        raise ConflictError('广告同步尚未上传结果') from error
    if manifest.get('sha256') != digest or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ConflictError('广告同步结果哈希不一致')
    total = manifest.get('total')
    if type(total) is not int or not 1 <= total <= 4000:
        raise ConflictError('广告同步分片记录无效')
    packed = io.BytesIO()
    sha = hashlib.sha256()
    for index in range(total):
        try:
            content = (directory / f'{index:04d}.part').read_bytes()
        except OSError as error:
            raise ConflictError('广告同步结果尚未上传完整') from error
        if not content or len(content) > CHUNK_BYTES:
            raise ConflictError('广告同步分片大小无效')
        sha.update(content)
        packed.write(content)
        if packed.tell() > MAX_PACKED:
            raise ConflictError('广告同步结果超过大小限制')
    if sha.hexdigest() != digest:
        raise ConflictError('广告同步结果内容校验失败')
    try:
        packed.seek(0)
        with gzip.GzipFile(fileobj=packed) as source:
            raw = source.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise ConflictError('广告同步结果解压超过大小限制')
        result = json.loads(raw)
    except (OSError, UnicodeError, ValueError) as error:
        raise ConflictError('广告同步结果压缩或 JSON 无效') from error
    if not isinstance(result, dict):
        raise ConflictError('广告同步结果格式无效')
    return result, packed.getbuffer().nbytes


def cleanup(job_id: str) -> None:
    directory = _directory(job_id)
    if not directory.exists():
        return
    for path in directory.iterdir():
        if path.name == 'manifest.json' or re.fullmatch(r'[0-9]{4}\.part', path.name):
            path.unlink(missing_ok=True)
    directory.rmdir()
