"""Integration tests against real PostgreSQL, Redis and S3-compatible storage.

Run with the compose stack up and the test environment loaded:
    uv run pytest -m integration
They use DATABASE_URL / MIGRATION_DATABASE_URL pointing at a dedicated *test* database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.core.db import Database
from app.core.migrations import expected_heads
from app.integrations.storage import build_storage, required_buckets
from app.main import create_app

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def settings() -> Settings:
    if os.environ.get("CLAIMOS_INTEGRATION") != "1":
        pytest.fail("Integration tests need CLAIMOS_INTEGRATION=1 and real service URLs (see runbook)")
    return Settings()


def _alembic(*args: str) -> None:
    subprocess.run(  # noqa: S603 - fixed argument list
        [sys.executable, "-m", "alembic", *args], cwd=BACKEND, check=True, capture_output=True
    )


def test_migrations_upgrade_downgrade_cycle(settings: Settings) -> None:
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")


async def test_baseline_objects_exist(settings: Settings) -> None:
    db = Database.from_settings(settings)
    try:
        assert await db.current_revision() in expected_heads()
        async with db.engine.connect() as conn:
            extensions = set((await conn.execute(text("SELECT extname FROM pg_extension"))).scalars())
            assert {"pgcrypto", "citext", "pg_trgm", "vector"} <= extensions
            uid = (await conn.execute(text("SELECT gen_random_uuid()"))).scalar_one()
            assert isinstance(uid, uuid.UUID)
    finally:
        await db.dispose()


async def test_append_only_trigger_blocks_updates(settings: Settings) -> None:
    db = Database.from_settings(settings)
    try:
        async with db.engine.connect() as conn:
            await conn.execute(text("CREATE TEMP TABLE t_append (id int)"))
            await conn.execute(
                text(
                    "CREATE TRIGGER t_append_guard BEFORE UPDATE OR DELETE ON t_append "
                    "FOR EACH ROW EXECUTE FUNCTION claimos_forbid_mutation()"
                )
            )
            await conn.execute(text("INSERT INTO t_append VALUES (1)"))
            with pytest.raises(DBAPIError, match="append-only"):
                await conn.execute(text("UPDATE t_append SET id = 2"))
    finally:
        await db.dispose()


async def test_redis_round_trip(settings: Settings) -> None:
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    key = f"claimos:test:{uuid.uuid4().hex}"
    try:
        await redis.set(key, "1", ex=10)
        assert await redis.get(key) == b"1"
    finally:
        await redis.delete(key)
        await redis.aclose()


async def test_object_storage_round_trip(settings: Settings) -> None:
    storage = build_storage(settings)
    for bucket in required_buckets(settings):
        await storage.ensure_bucket(bucket)
    key = f"integration/{uuid.uuid4().hex}/sample.txt"
    bucket = settings.s3_bucket_documents
    stored = await storage.put_bytes(bucket, key, b"synthetic content", "text/plain")
    try:
        assert await storage.get_bytes(bucket, key) == b"synthetic content"
        assert stored.size_bytes == 17
    finally:
        await storage.delete(bucket, key)
    assert not await storage.exists(bucket, key)


def test_readiness_against_real_dependencies(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health/ready")
    body = response.json()
    statuses = {c["name"]: c["status"] for c in body["components"]}
    assert response.status_code == 200, body
    for name in ("database", "migrations", "redis", "object_storage"):
        assert statuses[name] == "ok", body
