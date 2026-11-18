from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

# Async psycopg (the LangGraph checkpointer) cannot run on Windows' default Proactor loop; the
# containers run Linux, where this does not apply. Only affects test runs on a Windows host.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Unit tests must never reach real infrastructure: provide inert defaults before app imports.
# Integration tests (marker `integration`) read real values from the environment instead.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://unit:unit@127.0.0.1:1/unit")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:1/0")
os.environ.setdefault("STORAGE_PROVIDER", "local_filesystem")
os.environ.setdefault("LOG_JSON", "false")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.services.health_service import HealthCheck, HealthService

BASE_SETTINGS: dict[str, Any] = {
    "app_env": "test",
    "database_url": "postgresql+asyncpg://unit:unit@127.0.0.1:1/unit",
    "redis_url": "redis://127.0.0.1:1/0",
    "storage_provider": "local_filesystem",
    "log_json": False,
}


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        values = {**BASE_SETTINGS, "local_storage_path": tmp_path / "storage", **overrides}
        return Settings(**values)

    return factory


async def _ok() -> str | None:
    return None


@pytest.fixture
def healthy_service() -> HealthService:
    return HealthService([HealthCheck("database", _ok)], timeout_seconds=1)


@pytest.fixture
def make_app(
    make_settings: Callable[..., Settings], healthy_service: HealthService
) -> Callable[..., FastAPI]:
    def factory(health_service: HealthService | None = None, **overrides: Any) -> FastAPI:
        app = create_app(make_settings(**overrides))
        app.state.health_service = health_service or healthy_service
        return app

    return factory


@pytest.fixture
def client(make_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    with TestClient(make_app()) as test_client:
        yield test_client
