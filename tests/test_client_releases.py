import hashlib
from zipfile import ZipFile

import pytest

from erp.api.routes import client_releases
from erp.core.errors import NotFoundError


def test_release_metadata_and_download(tmp_path, monkeypatch):
    package = tmp_path / 'erp-desktop-0.6.77.zip'
    with ZipFile(package, 'w') as archive:
        archive.writestr('install.cmd', '@echo off')
    monkeypatch.setattr(client_releases, 'RELEASE_PATH', package)
    client_releases.package_info.cache_clear()
    try:
        info = client_releases.latest()
        assert info['size_bytes'] == package.stat().st_size
        assert info['sha256'] == hashlib.sha256(package.read_bytes()).hexdigest()
        assert info['download_path'].endswith('/latest/download')
        response = client_releases.download()
        assert response.path == package
        assert response.headers['x-content-sha256'] == info['sha256']
    finally:
        client_releases.package_info.cache_clear()


def test_missing_release_is_not_advertised(tmp_path, monkeypatch):
    monkeypatch.setattr(client_releases, 'RELEASE_PATH', tmp_path / 'missing.zip')
    client_releases.package_info.cache_clear()
    with pytest.raises(NotFoundError):
        client_releases.latest()
