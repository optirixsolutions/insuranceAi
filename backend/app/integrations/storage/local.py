"""DEVELOPMENT-ONLY filesystem storage. Refused in production by Settings validation."""

from __future__ import annotations

from pathlib import Path

import anyio

from app.integrations.storage.base import (
    InvalidObjectKeyError,
    ObjectNotFoundError,
    StorageError,
    StoredObject,
    sha256_hex,
    validate_key,
)


class LocalFilesystemStorage:
    provider_name = "local_filesystem"

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _path(self, bucket: str, key: str) -> Path:
        validate_key(key)
        validate_key(bucket)
        path = (self._root / bucket / key).resolve()
        if not path.is_relative_to(self._root / bucket):
            raise InvalidObjectKeyError("object key escapes the bucket directory")
        return path

    async def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str) -> StoredObject:
        path = anyio.Path(self._path(bucket, key))
        await path.parent.mkdir(parents=True, exist_ok=True)
        await path.write_bytes(data)
        return StoredObject(bucket, key, len(data), sha256_hex(data), content_type)

    async def get_bytes(self, bucket: str, key: str) -> bytes:
        path = anyio.Path(self._path(bucket, key))
        try:
            return await path.read_bytes()
        except FileNotFoundError as exc:
            raise ObjectNotFoundError("object not found") from exc

    async def delete(self, bucket: str, key: str) -> None:
        path = anyio.Path(self._path(bucket, key))
        try:
            await path.unlink()
        except FileNotFoundError as exc:
            raise ObjectNotFoundError("object not found") from exc

    async def exists(self, bucket: str, key: str) -> bool:
        return await anyio.Path(self._path(bucket, key)).is_file()

    async def ensure_bucket(self, bucket: str) -> None:
        validate_key(bucket)
        await anyio.Path(self._root / bucket).mkdir(parents=True, exist_ok=True)

    async def healthcheck(self, buckets: list[str]) -> None:
        for bucket in buckets:
            if not await anyio.Path(self._root / bucket).is_dir():
                raise StorageError(f"bucket directory missing: {bucket}")
