"""Request id propagation and structured access logging (pure ASGI, streaming-safe)."""

from __future__ import annotations

import time

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import problem_response
from app.core.logging import get_logger
from app.core.metrics import observe_request
from app.core.request_context import resolve_request_id, set_request_id

REQUEST_ID_HEADER = "X-Request-ID"
_QUIET_PATHS = frozenset({"/api/v1/health/live"})

logger = get_logger("claimos.access")


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = resolve_request_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        set_request_id(request_id)
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Unhandled errors are converted here (not in Starlette's outer ServerErrorMiddleware)
            # so the problem response still carries the request id.
            if response_started:
                raise
            logger.exception("unhandled_exception", path=scope.get("path"))
            response = problem_response(
                status=500,
                code="INTERNAL",
                title="Internal server error",
                detail="An unexpected error occurred. Quote the request id when contacting support.",
            )
            await response(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - started
            observe_request(scope, status_code, elapsed)
            path = scope.get("path", "")
            if path not in _QUIET_PATHS:
                logger.info(
                    "http_request",
                    method=scope.get("method"),
                    path=path,
                    status=status_code,
                    duration_ms=round(elapsed * 1000, 2),
                )
            structlog.contextvars.unbind_contextvars("request_id")
            set_request_id(None)
