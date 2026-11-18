"""S3-compatible object storage (AWS S3, SeaweedFS, Ceph RGW, ...)."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

import anyio
import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.integrations.storage.base import (
    ObjectNotFoundError,
    StorageError,
    StoredObject,
    sha256_hex,
    validate_key,
)

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound", "NoSuchBucket"})


class S3ObjectStorage:
    provider_name = "s3"

    def __init__(
        self,
        *,
        endpoint_url: str | None,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        force_path_style: bool,
        server_side_encryption: str,
        kms_key_id: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._client: S3Client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=BotoConfig(
                s3={"addressing_style": "path" if force_path_style else "auto"},
                connect_timeout=timeout_seconds,
                read_timeout=timeout_seconds * 6,
                retries={"max_attempts": 3, "mode": "standard"},
                signature_version="s3v4",
            ),
        )
        self._sse = server_side_encryption
        self._kms_key_id = kms_key_id

    async def _call(self, fn: Any, /, **kwargs: Any) -> Any:
        try:
            return await anyio.to_thread.run_sync(partial(fn, **kwargs))
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in _NOT_FOUND_CODES:
                raise ObjectNotFoundError(f"object or bucket not found ({code})") from exc
            raise StorageError(f"S3 request failed ({code or 'unknown'})") from exc
        except BotoCoreError as exc:
            raise StorageError(f"S3 transport error ({type(exc).__name__})") from exc

    def _encryption_args(self) -> dict[str, str]:
        if self._sse == "none":
            return {}
        args = {"ServerSideEncryption": self._sse}
        if self._sse == "aws:kms" and self._kms_key_id:
            args["SSEKMSKeyId"] = self._kms_key_id
        return args

    async def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str) -> StoredObject:
        validate_key(key)
        digest = sha256_hex(data)
        await self._call(
            self._client.put_object,
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            Metadata={"sha256": digest},
            **self._encryption_args(),
        )
        return StoredObject(bucket, key, len(data), digest, content_type)

    async def get_bytes(self, bucket: str, key: str) -> bytes:
        validate_key(key)
        response = await self._call(self._client.get_object, Bucket=bucket, Key=key)
        body = response["Body"]
        try:
            data: bytes = await anyio.to_thread.run_sync(body.read)
        finally:
            body.close()
        return data

    async def delete(self, bucket: str, key: str) -> None:
        validate_key(key)
        await self._call(self._client.delete_object, Bucket=bucket, Key=key)

    async def exists(self, bucket: str, key: str) -> bool:
        validate_key(key)
        try:
            await self._call(self._client.head_object, Bucket=bucket, Key=key)
        except ObjectNotFoundError:
            return False
        return True

    async def ensure_bucket(self, bucket: str) -> None:
        try:
            await self._call(self._client.head_bucket, Bucket=bucket)
        except ObjectNotFoundError:
            await self._call(self._client.create_bucket, Bucket=bucket)

    async def healthcheck(self, buckets: list[str]) -> None:
        for bucket in buckets:
            await self._call(self._client.head_bucket, Bucket=bucket)
