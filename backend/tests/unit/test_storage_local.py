from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.integrations.storage.base import InvalidObjectKeyError, ObjectNotFoundError, StorageError
from app.integrations.storage.local import LocalFilesystemStorage

BUCKET = "claimos-documents"


@pytest.fixture
async def storage(tmp_path: Path) -> LocalFilesystemStorage:
    store = LocalFilesystemStorage(tmp_path)
    await store.ensure_bucket(BUCKET)
    return store


async def test_round_trip(storage: LocalFilesystemStorage) -> None:
    data = b"%PDF-1.7 synthetic"
    stored = await storage.put_bytes(BUCKET, "org/1/claims/2/doc.pdf", data, "application/pdf")
    assert stored.size_bytes == len(data)
    assert stored.sha256 == hashlib.sha256(data).hexdigest()
    assert await storage.exists(BUCKET, "org/1/claims/2/doc.pdf")
    assert await storage.get_bytes(BUCKET, "org/1/claims/2/doc.pdf") == data
    await storage.delete(BUCKET, "org/1/claims/2/doc.pdf")
    assert not await storage.exists(BUCKET, "org/1/claims/2/doc.pdf")


async def test_missing_object(storage: LocalFilesystemStorage) -> None:
    with pytest.raises(ObjectNotFoundError):
        await storage.get_bytes(BUCKET, "nope")


@pytest.mark.parametrize("key", ["../escape", "a/../../b", "/absolute", "with space", "a\\b", ""])
async def test_rejects_unsafe_keys(storage: LocalFilesystemStorage, key: str) -> None:
    with pytest.raises(InvalidObjectKeyError):
        await storage.put_bytes(BUCKET, key, b"x", "text/plain")


async def test_healthcheck_detects_missing_bucket(storage: LocalFilesystemStorage) -> None:
    await storage.healthcheck([BUCKET])
    with pytest.raises(StorageError):
        await storage.healthcheck(["claimos-reports"])
