"""Reject state-changing API requests that come from another origin (CSRF defence in depth).

Browsers always send ``Origin`` on cross-origin POST/PUT/PATCH/DELETE and send
``Sec-Fetch-Site`` on modern engines. Requests without either header (CLI clients,
server-to-server) are allowed through; session-authenticated endpoints still require
the CSRF token.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.errors import problem_response

_UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class OriginCheckMiddleware:
    def __init__(self, app: ASGIApp, *, allowed_origin: str) -> None:
        self.app = app
        self.allowed_origin = allowed_origin.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") in _UNSAFE
            and scope.get("path", "").startswith("/api/")
        ):
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            fetch_site = headers.get("sec-fetch-site")
            if (
                origin is not None and origin.rstrip("/") != self.allowed_origin
            ) or fetch_site == "cross-site":
                response = problem_response(
                    status=403,
                    code="ORIGIN_REJECTED",
                    title="Cross-origin request rejected",
                    detail="State-changing requests must come from the ClaimOS application origin",
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
