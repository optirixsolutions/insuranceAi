"""Prometheus metrics (Phase 13).

The API exposes ``/metrics`` on its own port (not under ``/api``, so Nginx never serves it publicly). Request
metrics are labelled by the route *template* (``/api/v1/claims/{claim_id}``), never the raw path, so ids
do not explode label cardinality. If ``METRICS_TOKEN`` is set, scrapers must send it as a bearer token.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Scope

from app.core.config import Settings

HTTP_REQUESTS = Counter(
    "claimos_http_requests_total", "HTTP requests handled by the API", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "claimos_http_request_duration_seconds",
    "Time to handle an HTTP request",
    ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)
APP_INFO = Gauge("claimos_app_info", "Build information", ["version", "environment"])

_UNMATCHED = "unmatched"
_SKIP = frozenset({"/metrics", "/api/v1/health/live"})


def route_template(scope: Scope) -> str:
    """The matched route's template with its router prefix: ``/api/v1/claims/{claim_id}``.

    Nested routers only know their own relative template, so the static prefix is taken from the front of
    the request path (router prefixes here never contain parameters).
    """
    template = getattr(scope.get("route"), "path", None)
    if not isinstance(template, str):
        return _UNMATCHED
    if ":path}" in template:
        return template
    actual = [p for p in str(scope.get("path", "")).split("/") if p]
    relative = [p for p in template.split("/") if p]
    extra = len(actual) - len(relative)
    prefix = "/" + "/".join(actual[:extra]) if extra > 0 else ""
    return f"{prefix}{template}"


def observe_request(scope: Scope, status: int, seconds: float) -> None:
    if scope.get("path") in _SKIP:
        return
    method, route = str(scope.get("method", "")), route_template(scope)
    HTTP_REQUESTS.labels(method, route, str(status)).inc()
    HTTP_LATENCY.labels(method, route).observe(seconds)


def set_app_info(settings: Settings) -> None:
    APP_INFO.labels(settings.app_version, settings.app_env.value).set(1)


def metrics_endpoint(settings: Settings) -> Callable[[Request], Awaitable[Response]]:
    token = settings.metrics_token.get_secret_value() if settings.metrics_token else None

    async def metrics(request: Request) -> Response:
        if token is not None:
            given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(given.encode(), token.encode()):
                return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return metrics
