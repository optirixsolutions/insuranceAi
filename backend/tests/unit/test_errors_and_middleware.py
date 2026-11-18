from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import NotFoundError

PROBLEM = "application/problem+json"


def _app_with_routes(make_app: Callable[..., FastAPI], **overrides: object) -> FastAPI:
    app = make_app(**overrides)

    @app.get("/api/v1/_test/not-found")
    async def not_found() -> None:
        raise NotFoundError("claim does not exist")

    @app.get("/api/v1/_test/boom")
    async def boom() -> None:
        raise RuntimeError("database password is hunter2")

    @app.get("/api/v1/_test/typed")
    async def typed(count: int) -> dict[str, int]:
        return {"count": count}

    return app


def test_app_error_is_problem_json(make_app: Callable[..., FastAPI]) -> None:
    with TestClient(_app_with_routes(make_app)) as client:
        response = client.get("/api/v1/_test/not-found")
    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM
    body = response.json()
    assert body["code"] == "NOT_FOUND"
    assert body["detail"] == "claim does not exist"
    assert body["type"] == "https://claimos.dev/problems/not-found"
    assert body["request_id"] == response.headers["x-request-id"]


def test_unhandled_error_hides_internals_and_keeps_request_id(make_app: Callable[..., FastAPI]) -> None:
    with TestClient(_app_with_routes(make_app), raise_server_exceptions=False) as client:
        response = client.get("/api/v1/_test/boom", headers={"X-Request-ID": "req-12345678"})
    assert response.status_code == 500
    assert response.headers["content-type"] == PROBLEM
    assert response.headers["x-request-id"] == "req-12345678"
    body = response.json()
    assert body["code"] == "INTERNAL"
    assert body["request_id"] == "req-12345678"
    assert "hunter2" not in response.text


def test_validation_error_does_not_echo_input(make_app: Callable[..., FastAPI]) -> None:
    with TestClient(_app_with_routes(make_app)) as client:
        response = client.get("/api/v1/_test/typed", params={"count": "secret-value"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["errors"][0]["loc"] == ["query", "count"]
    assert "secret-value" not in response.text


def test_unknown_route_is_problem_json(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "NOT_FOUND"


def test_method_not_allowed_is_problem_json(client: TestClient) -> None:
    response = client.post("/api/v1/health/live")
    assert response.status_code == 405
    assert response.json()["code"] == "METHOD_NOT_ALLOWED"


def test_valid_request_id_is_propagated(client: TestClient) -> None:
    response = client.get("/api/v1/health/live", headers={"X-Request-ID": "abc-123-def-456"})
    assert response.headers["x-request-id"] == "abc-123-def-456"


def test_malformed_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/api/v1/health/live", headers={"X-Request-ID": "bad id\r\ninjected"})
    request_id = response.headers["x-request-id"]
    assert request_id != "bad id\r\ninjected"
    assert len(request_id) == 32


def test_security_headers(client: TestClient) -> None:
    headers = client.get("/api/v1/health/live").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cache-control"] == "no-store"
    assert "default-src 'none'" in headers["content-security-policy"]
    assert "strict-transport-security" not in headers


def test_hsts_only_in_production(make_app: Callable[..., FastAPI]) -> None:
    app = make_app(
        app_env="production",
        app_base_url="https://claims.example.com",
        database_url="postgresql+asyncpg://claimos_app:Generated1@db/claimos",
        storage_provider="s3",
        s3_access_key_id="AKIA",
        s3_secret_access_key="Generated2",
        av_provider="clamav",
        data_encryption_key="ab" * 32,
    )
    with TestClient(app) as client:
        headers = client.get("/api/v1/health/live").headers
    assert headers["strict-transport-security"].startswith("max-age=")


def test_docs_disabled_in_production(make_app: Callable[..., FastAPI]) -> None:
    app = make_app(
        app_env="production",
        app_base_url="https://claims.example.com",
        database_url="postgresql+asyncpg://claimos_app:Generated1@db/claimos",
        storage_provider="s3",
        s3_access_key_id="AKIA",
        s3_secret_access_key="Generated2",
        av_provider="clamav",
        data_encryption_key="ab" * 32,
    )
    with TestClient(app) as client:
        assert client.get("/api/docs").status_code == 404
        assert client.get("/api/v1/openapi.json").status_code == 404
