from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from erp.worker_tasks.image_prepare import optimize_image, validate_public_url


def _png(width: int = 1200, height: int = 800, mode: str = "RGBA") -> bytes:
    output = BytesIO()
    Image.new(mode, (width, height), (40, 80, 120, 180)).save(output, format="PNG")
    return output.getvalue()


def test_optimize_image_resizes_and_flattens_transparency() -> None:
    content, details = optimize_image(_png(), max_dimension=600, quality=80, output_format="JPEG")
    with Image.open(BytesIO(content)) as result:
        assert result.format == "JPEG"
        assert result.mode == "RGB"
        assert result.size == (600, 400)
    assert details["original_format"] == "PNG"
    assert details["width"] == 600
    assert len(content) > 0


def test_validate_public_url_rejects_private_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "erp.worker_tasks.image_prepare.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 80))],
    )
    with pytest.raises(ValueError, match="内网"):
        validate_public_url("http://example.test/image.png")


def test_validate_public_url_accepts_public_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "erp.worker_tasks.image_prepare.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("1.1.1.1", 443))],
    )
    assert validate_public_url("https://example.test/image.png") == "https://example.test/image.png"


def test_synthetic_dns_requires_opt_in_and_never_allows_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "erp.worker_tasks.image_prepare.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("198.18.0.10", 443))],
    )
    with pytest.raises(ValueError, match="保留地址"):
        validate_public_url("https://example.test/image.png")
    assert validate_public_url("https://example.test/image.png", allow_synthetic_dns=True)
    with pytest.raises(ValueError, match="保留地址"):
        validate_public_url("https://198.18.0.10/image.png", allow_synthetic_dns=True)
