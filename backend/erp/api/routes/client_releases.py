"""Authenticated download of the verified Windows desktop package."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

from erp.core.errors import NotFoundError


router = APIRouter(prefix='/client/releases', tags=['client-releases'])
VERSION = '0.6.91'
RELEASE_PATH = Path(__file__).resolve().parents[4] / 'outputs' / f'erp-desktop-{VERSION}.zip'


@lru_cache(maxsize=1)
def package_info() -> dict:
    path = RELEASE_PATH
    if not path.is_file():
        raise NotFoundError('当前客户端安装包尚未发布')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'version': VERSION, 'filename': path.name, 'size_bytes': path.stat().st_size,
            'sha256': digest.hexdigest(), 'requires': 'Windows 与 Python 3.12+（含 Tk）',
            'download_path': '/api/v1/client/releases/latest/download'}


@router.get('/latest')
def latest() -> dict:
    return package_info()


@router.get('/latest/download')
def download() -> FileResponse:
    info = package_info()
    return FileResponse(RELEASE_PATH, media_type='application/zip', filename=info['filename'],
                        headers={'X-Content-SHA256': info['sha256']})
