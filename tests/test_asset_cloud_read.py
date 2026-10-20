from __future__ import annotations

import hashlib
import io
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.core.errors import AppError
from erp.core.jsonutil import loads
from erp.domains.pod.models import PodArtwork, PodComposition
from erp.models import Asset, Job
from erp.services import asset_cache
from erp.services.assets import resolve_asset_path
from erp.services.jobs import execute_job


@pytest.fixture
def cache_settings(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "asset_remote_allowed_hosts", ["images.example.test"])
    monkeypatch.setattr(settings, "asset_cache_max_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "asset_download_max_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "asset_cache_min_free_bytes", 0)
    return settings


def png():
    out = io.BytesIO()
    Image.new("RGB", (12, 8), "red").save(out, "PNG")
    return out.getvalue()


def asset(content, **kwargs):
    values = dict(id=uuid.uuid4().hex, name="picture.png", local_path="", oss_url="https://images.example.test/picture.png",
                  sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content), mime_type="image/png", mirror_status="mirrored")
    values.update(kwargs)
    return Asset(**values)


def mock_remote(monkeypatch, content, *, headers=None, status=200):
    calls = []
    @contextmanager
    def response(url):
        calls.append(url)
        result = io.BytesIO(content)
        result.status = status
        result.getheader = lambda name, default=None: (headers or {}).get(name, default)
        with result:
            yield result
    monkeypatch.setattr(asset_cache, "_response", response)
    return calls


def test_missing_local_file_uses_verified_immutable_cache(cache_settings, monkeypatch):
    data = png()
    calls = mock_remote(monkeypatch, data)
    record = asset(data, local_path=str(cache_settings.data_dir / "old" / "source.png"))
    path = resolve_asset_path(record)
    assert path.read_bytes() == data
    assert resolve_asset_path(record) == path
    assert len(calls) == 1
    assert record.local_path.endswith("source.png")  # Do not rewrite the business record with a cache path.


def test_local_original_is_preserved_and_never_downloaded(cache_settings, monkeypatch):
    data = png()
    local = cache_settings.data_dir / "original.png"
    local.write_bytes(data)
    calls = mock_remote(monkeypatch, b"wrong")
    assert resolve_asset_path(asset(data, local_path=str(local))) == local
    assert not calls and local.read_bytes() == data


def test_other_machine_path_does_not_block_cloud_copy(cache_settings, monkeypatch, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "source.png"
    outside.write_bytes(b"do not read")
    data = png()
    mock_remote(monkeypatch, data)
    assert resolve_asset_path(asset(data, local_path=str(outside))).read_bytes() == data
    assert outside.read_bytes() == b"do not read"


@pytest.mark.parametrize("change,expected", [
    ({"sha256": "0" * 64}, "asset_hash_mismatch"),
    ({"size_bytes": 123}, "asset_size_mismatch"),
    ({"mirror_status": "deleted"}, "asset_deleted"),
])
def test_unverified_or_deleted_assets_are_not_served(cache_settings, monkeypatch, change, expected):
    data = png()
    mock_remote(monkeypatch, data)
    with pytest.raises(AppError) as error:
        resolve_asset_path(asset(data, **change))
    assert error.value.code == expected
    assert not list(cache_settings.data_dir.rglob("*.part"))
    assert not list((cache_settings.data_dir / "asset-cache").rglob("*.png"))


def test_stream_without_length_cannot_exceed_quota(cache_settings, monkeypatch):
    monkeypatch.setattr(cache_settings, "asset_cache_max_bytes", 10)
    mock_remote(monkeypatch, b"x" * 100)
    with pytest.raises(AppError) as error:
        resolve_asset_path(asset(b"", size_bytes=0, sha256=""))
    assert error.value.code == "asset_cache_capacity"
    assert not list(cache_settings.data_dir.rglob("*.part"))


def test_full_cache_preserves_existing_copy(cache_settings, monkeypatch):
    data = png()
    calls = mock_remote(monkeypatch, data)
    monkeypatch.setattr(cache_settings, "asset_cache_max_bytes", len(data))
    first = asset(data)
    path = resolve_asset_path(first)
    with pytest.raises(AppError) as error:
        resolve_asset_path(asset(data))
    assert error.value.status_code == 507
    assert path.read_bytes() == data and resolve_asset_path(first) == path and len(calls) == 1


def test_low_disk_space_rejects_before_network(cache_settings, monkeypatch):
    calls = mock_remote(monkeypatch, png())
    monkeypatch.setattr(asset_cache.shutil, "disk_usage", lambda _: Mock(free=1))
    with pytest.raises(AppError) as error:
        resolve_asset_path(asset(png()))
    assert error.value.code == "asset_cache_capacity" and not calls


def test_partial_transfer_does_not_publish_cache(cache_settings, monkeypatch):
    mock_remote(monkeypatch, b"partial", headers={"Content-Length": "100"})
    with pytest.raises(AppError) as error:
        resolve_asset_path(asset(b"partial", size_bytes=0, sha256=""))
    assert error.value.code == "asset_download_incomplete"
    assert not list(cache_settings.data_dir.rglob("*.part"))


def test_concurrent_readers_publish_only_one_copy(cache_settings, monkeypatch):
    data = png()
    calls = mock_remote(monkeypatch, data)
    record = asset(data)
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _: resolve_asset_path(record), range(4)))
    assert len(set(paths)) == 1 and paths[0].read_bytes() == data and len(calls) == 1


@pytest.mark.parametrize("url,ip", [
    ("https://untrusted.test/source.png", "8.8.8.8"),
    ("https://images.example.test/source.png", "127.0.0.1"),
    ("https://images.example.test/source.png", "10.0.0.1"),
    ("https://images.example.test/source.png", "198.18.0.1"),
    ("https://images.example.test:444/source.png", "8.8.8.8"),
    ("https://user:secret@images.example.test/source.png", "8.8.8.8"),
    ("http://images.example.test/source.png", "8.8.8.8"),
])
def test_untrusted_network_destinations_rejected(cache_settings, monkeypatch, url, ip):
    monkeypatch.setattr(cache_settings, "asset_allow_synthetic_dns", False)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))])
    with pytest.raises(AppError) as error:
        asset_cache._validate_url(url)
    assert error.value.code == "asset_remote_forbidden" and "secret" not in str(error.value)


def test_https_connection_is_pinned_and_redirect_is_revalidated(cache_settings, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    tcp = Mock()
    monkeypatch.setattr(socket, "create_connection", tcp)
    made = []
    class Connection:
        def __init__(self, host, **kwargs):
            assert host == "images.example.test"
            made.append(self)
        def request(self, *args, **kwargs):
            self._create_connection(("images.example.test", 443), 30)
        def getresponse(self):
            return Mock(status=302, getheader=lambda _: "https://localhost/internal")
        def close(self): pass
    monkeypatch.setattr(asset_cache.http.client, "HTTPSConnection", Connection)
    with pytest.raises(AppError) as error:
        resolve_asset_path(asset(png()))
    assert error.value.code == "asset_remote_forbidden"
    assert len(made) == 1
    tcp.assert_called_once_with(("8.8.8.8", 443), 30, None)


def test_fake_dns_is_explicit_local_only_and_does_not_allow_private_ips(cache_settings, monkeypatch):
    monkeypatch.setattr(cache_settings, "asset_allow_synthetic_dns", True)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.1", 443))])
    assert asset_cache._validate_url("https://images.example.test/a.png")[1] == "198.18.0.1"
    monkeypatch.setattr(cache_settings, "runtime_mode", "server")
    with pytest.raises(AppError):
        asset_cache._validate_url("https://images.example.test/a.png")
    monkeypatch.setattr(cache_settings, "runtime_mode", "local")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))])
    with pytest.raises(AppError):
        asset_cache._validate_url("https://images.example.test/a.png")


def test_cloud_only_artwork_content_thumbnail_and_composition_resume(client, cache_settings, monkeypatch):
    data = png()
    artwork = client.post("/api/v1/pod/artworks", files={"image": ("art.png", data, "image/png")}).json()
    with SessionLocal() as session:
        record = session.get(PodArtwork, artwork["id"]).asset
        Path(record.local_path).unlink()  # Fixture image only; simulate another machine.
        record.oss_url = "https://images.example.test/art.png"
        record.oss_key = "art.png"
        record.oss_bucket = "images"
        record.mirror_status = "mirrored"
        asset_id = record.id
        session.commit()
    calls = mock_remote(monkeypatch, data)
    assert client.get(f"/api/v1/assets/{asset_id}/content").content == data
    assert client.get(artwork["file_url"]).content == data
    thumbnail = client.get(artwork["thumbnail_url"])
    assert thumbnail.status_code == 200
    with Image.open(io.BytesIO(thumbnail.content)) as image:
        assert image.size == (12, 8)
    from erp.domains.pod.composition import create_native_composition
    payload = dict(supplier="hicustom", product_type_id=1, product_name="Cloud read test", default_color_id=1,
                   default_view_id=1, selected_size_ids=[1], placements=[dict(view_id=1, width=100, height=100,
                   top_x=0, top_y=0, artwork_id=artwork["id"], rotation=0)])
    uploaded = []
    class Supplier:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def upload_gallery(self, **kwargs):
            uploaded.append(kwargs["content"])
            raise AppError("未知结果测试", code="pod_write_outcome_unknown", details={"outcome_unknown": True})
    monkeypatch.setattr("erp.domains.pod.composition.supplier_client", lambda *a: Supplier())
    with SessionLocal() as session:
        composition = create_native_composition(session, request=payload, defer_source_preparation=True)
        job = session.get(Job, composition.job_id)
        execute_job(session, job)
        assert job.status == "paused", job.error_message
        assert uploaded == [data]
        # A second attempt must preserve the unknown-outcome guard.
        execute_job(session, job)
        assert uploaded == [data]
        assert session.get(PodComposition, composition.id).outcome_unknown
    assert len(calls) == 1
