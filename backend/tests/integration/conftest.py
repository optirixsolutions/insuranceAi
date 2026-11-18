"""Fixtures for integration tests against real PostgreSQL, Redis and S3 storage."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import ExitStack
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings
from app.main import create_app

PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="session")
def settings() -> Settings:
    if os.environ.get("CLAIMOS_INTEGRATION") != "1":
        pytest.fail("Integration tests need CLAIMOS_INTEGRATION=1 and real service URLs (see runbook)")
    return Settings()


class ApiClient:
    """A browser-like client: own cookie jar, sends the CSRF token on unsafe requests."""

    def __init__(self, client: TestClient) -> None:
        self.http = client

    @property
    def csrf(self) -> str | None:
        value: str | None = self.http.cookies.get("__Host-claimos_csrf")
        return value

    def _headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        headers = {"X-CSRF-Token": self.csrf} if self.csrf else {}
        return {**headers, **(extra or {})}

    def get(self, path: str, **kw: Any) -> Any:
        return self.http.get(f"/api/v1{path}", **kw)

    def post(self, path: str, json: Any = None, headers: dict[str, str] | None = None) -> Any:
        return self.http.post(f"/api/v1{path}", json=json, headers=self._headers(headers))

    def patch(self, path: str, json: Any = None, headers: dict[str, str] | None = None) -> Any:
        return self.http.patch(f"/api/v1{path}", json=json, headers=self._headers(headers))

    def put(self, path: str, json: Any = None, headers: dict[str, str] | None = None) -> Any:
        return self.http.put(f"/api/v1{path}", json=json, headers=self._headers(headers))

    def delete(self, path: str, headers: dict[str, str] | None = None) -> Any:
        return self.http.delete(f"/api/v1{path}", headers=self._headers(headers))


@pytest.fixture
def new_client(settings: Settings) -> Iterator[Callable[[], ApiClient]]:
    with ExitStack() as stack:

        def factory() -> ApiClient:
            # One app per client so each has its own lifespan, engine and event loop.
            client = TestClient(create_app(settings), base_url="https://testserver")
            return ApiClient(stack.enter_context(client))

        yield factory


def unique_email(prefix: str = "user") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}@example.test"


def register(
    client: ApiClient, *, org: str = "Synthetic Insurance Ltd", email: str | None = None
) -> dict[str, Any]:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": org,
            "display_name": "Org Admin",
            "email": email or unique_email("admin"),
            "password": PASSWORD,
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def invite(admin: ApiClient, roles: list[str], email: str | None = None) -> dict[str, Any]:
    response = admin.post(
        "/users", json={"email": email or unique_email(), "display_name": "Invited User", "roles": roles}
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def accept(client: ApiClient, invitation: dict[str, Any], password: str = PASSWORD) -> dict[str, Any]:
    token = invitation["access_link"]["url"].split("#token=", 1)[1]
    response = client.post("/auth/invitations/accept", json={"token": token, "password": password})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.fixture
async def migrator_engine() -> AsyncIterator[AsyncEngine]:
    """Schema-owner connection (bypasses RLS) for arranging and tampering in tests."""
    engine = create_async_engine(os.environ["MIGRATION_DATABASE_URL"])
    yield engine
    await engine.dispose()


@pytest.fixture
async def app_engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    """Runtime-role connection (subject to RLS), as used by the API."""
    engine = create_async_engine(settings.database_url.get_secret_value())
    yield engine
    await engine.dispose()
