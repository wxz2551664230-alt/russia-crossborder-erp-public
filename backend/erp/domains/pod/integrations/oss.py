from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import alibabacloud_oss_v2 as oss
from sqlalchemy import select
from sqlalchemy.orm import Session

from erp.core.errors import AppError
from erp.core.jsonutil import loads
from erp.core.security import vault
from erp.models import IntegrationConnection


class PodOssService:
    def __init__(self, connection: IntegrationConnection) -> None:
        self.connection_id = connection.id
        config = loads(connection.config_json, {})
        secrets = vault.decrypt(connection.secrets_encrypted) if connection.secrets_encrypted else {}
        self.region = str(config.get("region") or "").strip().lower()
        self.endpoint = str(config.get("endpoint") or "").strip()
        self.bucket = str(config.get("bucket") or "").strip().lower()
        self.prefix = str(config.get("prefix") or "hicustom-products").strip().strip("/")
        self.access_key_id = str(secrets.get("access_key_id") or "").strip()
        self.access_key_secret = str(secrets.get("access_key_secret") or "").strip()
        if not all((self.region, self.bucket, self.access_key_id, self.access_key_secret)):
            raise AppError("POD 产品资产 OSS 配置不完整", code="pod_oss_incomplete")
        provider = oss.credentials.StaticCredentialsProvider(self.access_key_id, self.access_key_secret)
        endpoint = self.endpoint or None
        if endpoint and "://" not in endpoint:
            endpoint = f"https://{endpoint}"
        self.client = oss.Client(oss.Config(
            region=self.region,
            endpoint=endpoint,
            credentials_provider=provider,
            connect_timeout=8,
            readwrite_timeout=60,
            retry_max_attempts=3,
        ))

    @classmethod
    def from_session(cls, session: Session, connection_id: str = "") -> "PodOssService | None":
        if connection_id:
            connection = session.get(IntegrationConnection, connection_id)
            if connection and connection.enabled and connection.provider == "oss":
                return cls(connection)
            return None
        connections = session.scalars(select(IntegrationConnection).where(
            IntegrationConnection.provider == "oss",
            IntegrationConnection.enabled.is_(True),
        ).order_by(IntegrationConnection.created_at)).all()
        for connection in connections:
            config = loads(connection.config_json, {})
            if str(config.get("prefix") or "").strip("/") == "hicustom-products":
                return cls(connection)
        return None

    def download_asset(self, *, key: str, target: Path, expected_sha256: str = "") -> dict[str, str | int]:
        import hashlib
        import os

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".download")
        request = oss.GetObjectRequest(bucket=self.bucket, key=key)
        try:
            result = self.client.get_object(request)
            with result.body, temporary.open("wb") as stream:
                for chunk in result.body.iter_bytes(): stream.write(chunk)
        except (oss.exceptions.BaseError, OSError) as error:
            temporary.unlink(missing_ok=True)
            raise AppError("POD 产品资产从 OSS 恢复失败", code="pod_oss_download_failed", status_code=502) from error
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        if expected_sha256 and digest.casefold() != expected_sha256.casefold():
            temporary.unlink(missing_ok=True)
            raise AppError("POD 产品资产恢复后哈希不一致", code="pod_oss_hash_mismatch", status_code=409)
        os.replace(temporary, target)
        return {"path": str(target), "sha256": digest, "size": target.stat().st_size}

    def object_url(self, key: str) -> str:
        endpoint = self.endpoint or f"https://oss-{self.region}.aliyuncs.com"
        if "://" not in endpoint:
            endpoint = f"https://{endpoint}"
        hostname = str(urlparse(endpoint).hostname or "")
        return f"https://{self.bucket}.{hostname}/{quote(key, safe='/')}"

    def upload_artwork(self, path: Path, sha256: str) -> dict[str, str | int]:
        key = f"{self.prefix}/artwork-library/{sha256[:2]}/{sha256}{path.suffix.casefold()}"
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        request = oss.PutObjectRequest(
            bucket=self.bucket,
            key=key,
            content_type=content_type,
            content_length=path.stat().st_size,
            cache_control="public, max-age=31536000, immutable",
            metadata={"sha256": sha256, "asset-type": "artwork"},
        )
        try:
            result = self.client.put_object_from_file(request, str(path))
        except oss.exceptions.BaseError as error:
            raise AppError("POD 设计原图上传 OSS 失败", code="pod_oss_upload_failed", status_code=502) from error
        return {
            "oss_bucket": self.bucket,
            "oss_key": key,
            "oss_uri": f"oss://{self.bucket}/{key}",
            "oss_url": self.object_url(key),
            "content_type": content_type,
            "size": path.stat().st_size,
            "sha256": sha256,
            "etag": str(result.etag or "").strip('"'),
            "crc64": str(result.hash_crc64 or ""),
            "oss_version_id": str(result.version_id or ""),
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }

    def upload_self_selected_asset(self, path: Path, sha256: str) -> dict[str, str | int]:
        """Store a bundle image under a content-addressed, retry-safe key."""
        key = f"{self.prefix}/self-selected-import/{sha256[:2]}/{sha256}{path.suffix.casefold()}"
        request = oss.HeadObjectRequest(bucket=self.bucket, key=key)
        try:
            existing = self.client.head_object(request)
        except oss.exceptions.BaseError as error:
            cause = error.unwrap() if isinstance(error, oss.exceptions.OperationError) else error
            if getattr(cause, 'status_code', None) != 404 or getattr(cause, 'code', '') != 'NoSuchKey':
                raise AppError('自主上架图片 OSS 状态未知，请先核对', code='catalog_oss_unknown', status_code=502) from error
            existing = None
        if existing is not None:
            metadata = existing.metadata or {}
            if (str(metadata.get('sha256') or '').casefold() != sha256.casefold()
                    or int(existing.content_length or -1) != path.stat().st_size):
                raise AppError('OSS 图片键已存在但内容不符', code='catalog_oss_conflict', status_code=409)
        else:
            content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            upload = oss.PutObjectRequest(
                bucket=self.bucket, key=key, content_type=content_type,
                content_length=path.stat().st_size,
                cache_control='public, max-age=31536000, immutable',
                metadata={'sha256': sha256, 'asset-type': 'self-selected-import'},
            )
            try:
                self.client.put_object_from_file(upload, str(path))
            except oss.exceptions.BaseError as error:
                raise AppError('自主上架图片上传结果未知，请先核对同一对象', code='catalog_oss_unknown', status_code=502) from error
            confirmed = self.client.head_object(request)
            if (str((confirmed.metadata or {}).get('sha256') or '').casefold() != sha256.casefold()
                    or int(confirmed.content_length or -1) != path.stat().st_size):
                raise AppError('自主上架图片上传后校验失败', code='catalog_oss_hash_mismatch', status_code=409)
        return {'oss_bucket': self.bucket, 'oss_key': key, 'oss_url': self.object_url(key),
                'sha256': sha256, 'size_bytes': path.stat().st_size}

    def upload_product_asset(
        self, path: Path, *, sku: str, asset_type: str, color_id: int | None = None,
        view_id: int | None = None, position: int = 0,
    ) -> dict[str, str | int]:
        """Archive one immutable product asset under the POD-specific OSS prefix."""
        import hashlib

        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        safe_sku = "".join(char if char.isalnum() or char in "-_" else "_" for char in sku)[:120]
        section = "renderings" if asset_type == "rendering" else "sources"
        qualifier = f"color-{int(color_id or 0)}" if asset_type == "rendering" else f"view-{int(view_id or 0)}"
        key = f"{self.prefix}/library/products/{safe_sku}/{section}/{qualifier}/{position:02d}-{digest[:12]}{path.suffix.casefold()}"
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        request = oss.PutObjectRequest(
            bucket=self.bucket, key=key, content_type=content_type,
            content_length=path.stat().st_size, cache_control="public, max-age=31536000, immutable",
            metadata={"sha256": digest, "asset-type": asset_type, "sku": safe_sku},
        )
        try:
            result = self.client.put_object_from_file(request, str(path))
        except oss.exceptions.BaseError as error:
            raise AppError("POD 产品资产上传 OSS 失败", code="pod_oss_upload_failed", status_code=502) from error
        return {
            "oss_bucket": self.bucket, "oss_key": key, "oss_uri": f"oss://{self.bucket}/{key}",
            "oss_url": self.object_url(key), "content_type": content_type, "size": path.stat().st_size,
            "sha256": digest, "etag": str(result.etag or "").strip('"'),
            "crc64": str(result.hash_crc64 or ""), "oss_version_id": str(result.version_id or ""),
            "uploaded_at": datetime.now(timezone.utc).isoformat(), "asset_type": asset_type,
            "color_id": color_id, "view_id": view_id,
        }
