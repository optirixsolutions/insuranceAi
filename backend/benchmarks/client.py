"""A browser-like HTTP client for benchmarks (one per worker thread).

Same behaviour as ``tests.system.conftest.SystemClient`` (that module is not in the image and depends on
pytest): cookies are kept by hand because the stack issues ``Secure`` ``__Host-`` cookies that cookie jars
refuse over plain HTTP inside the compose network, and unsafe requests send the CSRF token. ``429`` responses
from the edge limiter are retried and counted, never hidden.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

PASSWORD = "benchmark synthetic passphrase"


class BenchClient:
    def __init__(self, base_url: str, timeout: float = 120) -> None:
        self.http = httpx.Client(base_url=base_url.rstrip("/") + "/api/v1", timeout=timeout)
        self.cookies: dict[str, str] = {}
        self.throttled = 0

    def close(self) -> None:
        self.http.close()

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.cookies:
            headers["Cookie"] = self.cookie_header()
        csrf = self.cookies.get("__Host-claimos_csrf")
        if method != "GET" and csrf:
            headers["X-CSRF-Token"] = csrf
        response = self.http.request(method, path, headers=headers, **kwargs)
        for attempt in range(10):
            if response.status_code != 429:
                break
            self.throttled += 1
            time.sleep(0.25 * (attempt + 1))
            response = self.http.request(method, path, headers=headers, **kwargs)
        for raw in response.headers.get_list("set-cookie"):
            name, _, rest = raw.partition("=")
            self.cookies[name.strip()] = rest.split(";", 1)[0]
        return response

    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("POST", path, **kw)

    def ok(self, method: str, path: str, expected: int = 200, **kw: Any) -> Any:
        response = self.request(method, path, **kw)
        if response.status_code != expected:
            raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else None

    # Session helpers -----------------------------------------------------------------------------

    def register(self, organisation: str) -> dict[str, Any]:
        email = f"bench-{uuid.uuid4().hex[:12]}@example.test"
        body: dict[str, Any] = self.ok(
            "POST",
            "/auth/register",
            201,
            json={
                "organization_name": organisation,
                "display_name": "Benchmark Admin",
                "email": email,
                "password": PASSWORD,
            },
        )
        body["email"] = email
        return body

    def login(self, email: str) -> None:
        self.ok("POST", "/auth/login", json={"email": email, "password": PASSWORD})
