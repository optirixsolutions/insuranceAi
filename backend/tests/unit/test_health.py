from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.migrations import expected_heads
from app.services.health_service import CheckFailedError, DegradedError, HealthCheck, HealthService


async def ok() -> str | None:
    return "fine"


async def degraded() -> str | None:
    raise DegradedError("no workers responded")


async def failing() -> str | None:
    raise ConnectionRefusedError("connect to 10.0.0.5:5432 failed, password=hunter2")


async def safe_failure() -> str | None:
    raise CheckFailedError("revision 0000 is not head")


async def slow() -> str | None:
    await asyncio.sleep(5)
    return None


async def test_all_ok() -> None:
    report = await HealthService([HealthCheck("a", ok), HealthCheck("b", ok)], timeout_seconds=1).readiness()
    assert report.status == "ok"
    assert [c.detail for c in report.components] == ["fine", "fine"]


async def test_optional_degraded_component_degrades_overall() -> None:
    service = HealthService(
        [HealthCheck("db", ok), HealthCheck("workers", degraded, required=False)], timeout_seconds=1
    )
    report = await service.readiness()
    assert report.status == "degraded"
    assert report.components[1].detail == "no workers responded"


async def test_required_failure_is_down_and_hides_message() -> None:
    report = await HealthService([HealthCheck("db", failing)], timeout_seconds=1).readiness()
    assert report.status == "down"
    assert report.components[0].detail == "ConnectionRefusedError"


async def test_optional_failure_does_not_take_service_down() -> None:
    service = HealthService(
        [HealthCheck("db", ok), HealthCheck("workers", failing, required=False)], timeout_seconds=1
    )
    assert (await service.readiness()).status == "degraded"


async def test_safe_failure_message_is_exposed() -> None:
    report = await HealthService([HealthCheck("migrations", safe_failure)], timeout_seconds=1).readiness()
    assert report.components[0].detail == "revision 0000 is not head"


async def test_timeout_is_reported() -> None:
    report = await HealthService([HealthCheck("db", slow)], timeout_seconds=0.05).readiness()
    assert report.status == "down"
    assert report.components[0].detail == "timed out after 0.05s"


def test_live_endpoint(client: TestClient) -> None:
    response = client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


def test_ready_endpoint_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["environment"] == "test"
    assert body["development_adapters"] == [
        "local_filesystem_storage",
        "antivirus_disabled",
        "derived_encryption_key",
    ]
    assert body["components"][0]["name"] == "database"


def test_ready_endpoint_returns_503_when_down(make_app: Callable[..., FastAPI]) -> None:
    app = make_app(health_service=HealthService([HealthCheck("database", failing)], timeout_seconds=1))
    with TestClient(app) as client:
        response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "down"
    assert "hunter2" not in response.text


def test_single_alembic_head() -> None:
    assert expected_heads() == frozenset({"0012"})
