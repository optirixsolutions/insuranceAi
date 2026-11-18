"""Prometheus /metrics: route-template labels (no raw ids), optional bearer token, not under /api."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_metrics_use_route_templates(client: TestClient) -> None:
    client.get("/api/v1/health/ready")
    client.get("/api/v1/claims/0b6f9c1e-0000-4000-8000-000000000001")  # 401, but still counted
    body = client.get("/metrics").text
    assert 'claimos_http_requests_total{method="GET",route="/api/v1/health/ready",status="200"}' in body
    assert 'route="/api/v1/claims/{claim_id}"' in body
    assert "0b6f9c1e-0000-4000-8000-000000000001" not in body
    assert "claimos_http_request_duration_seconds_bucket" in body
    assert "claimos_app_info{" in body
    assert 'route="/metrics"' not in body  # scrapes are not counted


def test_metrics_token_is_enforced(make_app: Callable[..., FastAPI]) -> None:
    with TestClient(make_app(metrics_token="scrape-secret-value")) as client:
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = client.get("/metrics", headers={"Authorization": "Bearer scrape-secret-value"})
        assert ok.status_code == 200 and ok.headers["content-type"].startswith("text/plain")
