from __future__ import annotations

from datetime import datetime, timezone

import pytest

from erp.worker_tasks.oss_upload import OssUploadConfig, build_object_key


def test_build_object_key_is_scoped_and_deterministic() -> None:
    value = build_object_key(
        "/erp-worker/staging/", "job:../../unsafe", "abcdef0123456789ffff", ".webp",
        now=datetime(2026, 9, 14, tzinfo=timezone.utc),
    )
    assert value == "erp-worker/staging/2026/09/14/jobunsafe/abcdef0123456789.webp"


def test_oss_config_requires_local_credentials() -> None:
    config = OssUploadConfig(
        region="cn-chengdu", endpoint="oss-cn-chengdu.aliyuncs.com", bucket="bucket",
        public_base_url="", prefix="erp-worker", access_key_id="", access_key_secret="",
    )
    with pytest.raises(ValueError, match="access_key_id"):
        config.validate()
