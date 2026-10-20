from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import alibabacloud_oss_v2 as oss


@dataclass(frozen=True)
class OssUploadConfig:
    region: str
    endpoint: str
    bucket: str
    public_base_url: str
    prefix: str
    access_key_id: str
    access_key_secret: str

    def validate(self) -> None:
        required = {
            "region": self.region,
            "endpoint": self.endpoint,
            "bucket": self.bucket,
            "access_key_id": self.access_key_id,
            "access_key_secret": self.access_key_secret,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError("本地 Worker OSS 配置缺少：" + "、".join(missing))


def build_object_key(prefix: str, job_id: str, sha256: str, suffix: str, *, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    safe_prefix = str(prefix or "erp-worker").strip("/")
    safe_job_id = "".join(char for char in str(job_id) if char.isalnum() or char in "-_")[:100]
    if not safe_job_id:
        raise ValueError("任务 ID 无效")
    safe_suffix = ".webp" if str(suffix).casefold() == ".webp" else ".jpg"
    return f"{safe_prefix}/{current:%Y/%m/%d}/{safe_job_id}/{sha256[:16]}{safe_suffix}"


def upload_prepared_image(path: Path, *, job_id: str, sha256: str, config: OssUploadConfig) -> dict:
    config.validate()
    object_key = build_object_key(config.prefix, job_id, sha256, path.suffix)
    client_config = oss.config.load_default()
    client_config.credentials_provider = oss.credentials.StaticCredentialsProvider(
        config.access_key_id, config.access_key_secret,
    )
    client_config.region = config.region
    client_config.endpoint = config.endpoint
    client = oss.Client(client_config)
    content_type = "image/webp" if path.suffix.casefold() == ".webp" else "image/jpeg"
    with path.open("rb") as body:
        response = client.put_object(oss.PutObjectRequest(
            bucket=config.bucket,
            key=object_key,
            body=body,
            content_type=content_type,
        ))
    base = str(config.public_base_url or f"https://{config.bucket}.{config.endpoint}").rstrip("/")
    return {
        "oss_bucket": config.bucket,
        "oss_key": object_key,
        "oss_url": f"{base}/{quote(object_key, safe='/')}",
        "oss_etag": str(getattr(response, "etag", "") or "").strip('"'),
    }
