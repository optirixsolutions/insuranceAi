"""Object storage provider interface.

Keys are generated server-side only (never accepted from clients) and must match
``VALID_KEY``. Documents are stored here; PostgreSQL keeps references + metadata.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

VALID_KEY = re.compile(r"^(?!.*\.\.)(?!/)[A-Za-z0-9!_.*'()/\-]{1,1024}$")


class StorageError(Exception):
    """Raised for storage failures; message never contains credentials."""


class InvalidObjectKeyError(StorageError):
    pass


class ObjectNotFoundError(StorageError):
    pass


@dataclass(frozen=True, slots=True)
class StoredObject:
    bucket: str
    key: str
    size_bytes: int
    sha256: str
    content_type: str


def validate_key(key: str) -> str:
    if not VALID_KEY.match(key):
        raise InvalidObjectKeyError("object key contains forbidden characters or path traversal")
    return key


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@runtime_checkable
class ObjectStorage(Protocol):
    provider_name: str

    async def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str) -> StoredObject: ...

    async def get_bytes(self, bucket: str, key: str) -> bytes: ...

    async def delete(self, bucket: str, key: str) -> None: ...

    async def exists(self, bucket: str, key: str) -> bool: ...

    async def ensure_bucket(self, bucket: str) -> None: ...

    async def healthcheck(self, buckets: list[str]) -> None:
        """Raise StorageError if any bucket is unreachable."""
        ...
