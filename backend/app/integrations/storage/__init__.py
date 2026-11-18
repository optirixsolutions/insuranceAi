"""Object storage adapters."""

from __future__ import annotations

from app.core.config import Settings
from app.integrations.storage.base import ObjectStorage
from app.integrations.storage.local import LocalFilesystemStorage
from app.integrations.storage.s3 import S3ObjectStorage


def build_storage(settings: Settings) -> ObjectStorage:
    if settings.storage_provider == "local_filesystem":
        return LocalFilesystemStorage(settings.local_storage_path)
    assert settings.s3_access_key_id is not None and settings.s3_secret_access_key is not None
    return S3ObjectStorage(
        endpoint_url=settings.s3_endpoint_url,
        region=settings.s3_region,
        access_key_id=settings.s3_access_key_id.get_secret_value(),
        secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        force_path_style=settings.s3_force_path_style,
        server_side_encryption=settings.s3_server_side_encryption,
        kms_key_id=settings.s3_kms_key_id,
        timeout_seconds=settings.health_check_timeout_seconds,
    )


def required_buckets(settings: Settings) -> list[str]:
    return [settings.s3_bucket_documents, settings.s3_bucket_reports]
