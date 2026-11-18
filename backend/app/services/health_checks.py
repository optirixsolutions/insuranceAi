"""Concrete readiness checks wired to real infrastructure clients."""

from __future__ import annotations

from typing import Any

import anyio
from redis.asyncio import Redis

from app.core.db import Database
from app.integrations.storage.base import ObjectStorage
from app.services.health_service import CheckFailedError, CheckFn, DegradedError


class MigrationsPendingError(CheckFailedError):
    pass


def database_check(db: Database) -> CheckFn:
    async def check() -> str | None:
        await db.ping()
        return None

    return check


def migrations_check(db: Database, expected_heads: frozenset[str]) -> CheckFn:
    async def check() -> str | None:
        current = await db.current_revision()
        if current is None:
            raise MigrationsPendingError("database has no alembic_version; run migrations")
        if current not in expected_heads:
            raise MigrationsPendingError(f"revision {current} is not head")
        return f"revision {current} (head)"

    return check


def redis_check(redis: Redis) -> CheckFn:
    async def check() -> str | None:
        await redis.ping()
        return None

    return check


def storage_check(storage: ObjectStorage, buckets: list[str]) -> CheckFn:
    async def check() -> str | None:
        await storage.healthcheck(buckets)
        return f"{storage.provider_name}, {len(buckets)} buckets"

    return check


def antivirus_check(scanner: Any) -> CheckFn:
    async def check() -> str | None:
        if not await anyio.to_thread.run_sync(scanner.ping):
            raise DegradedError("clamd did not answer PING")
        return "clamd responding"

    return check


def model_gateway_check(settings: Any) -> CheckFn:
    """Configuration only: no paid or rate-limited calls on every readiness probe."""

    async def check() -> str | None:
        if settings.ai_provider == "mock":
            return "mock provider (development)"
        if settings.openrouter_api_key is None:
            raise DegradedError("OPENROUTER_API_KEY is not configured")
        return "openrouter key configured"

    return check


def workers_check(celery_app: Any, timeout_seconds: float) -> CheckFn:
    """Broadcast ping; no responding worker is 'degraded' (API still serves reads)."""

    async def check() -> str | None:
        def _ping() -> list[dict[str, Any]]:
            replies: list[dict[str, Any]] = celery_app.control.ping(timeout=timeout_seconds) or []
            return replies

        replies = await anyio.to_thread.run_sync(_ping)
        if not replies:
            raise DegradedError("no workers responded")
        return f"{len(replies)} worker(s) responding"

    return check
