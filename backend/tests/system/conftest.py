"""System tests: drive the running docker-compose stack over HTTP (real API, workers, OCR, AV).

Run with the stack up:  CLAIMOS_SYSTEM_URL=http://127.0.0.1:8080 uv run pytest -m system tests/system
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

PASSWORD = "system test passphrase"


class SystemClient:
    """Browser-like client. Cookies are managed by hand because the stack issues Secure
    ``__Host-`` cookies, which cookie jars refuse to send over plain-HTTP localhost."""

    def __init__(self, base_url: str) -> None:
        self.http = httpx.Client(base_url=base_url.rstrip("/") + "/api/v1", timeout=60)
        self.cookies: dict[str, str] = {}

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        csrf = self.cookies.get("__Host-claimos_csrf")
        if method != "GET" and csrf:
            headers["X-CSRF-Token"] = csrf
        for attempt in range(8):  # respect the edge rate limiter
            response = self.http.request(method, path, headers=headers, **kwargs)
            if response.status_code != 429:
                break
            time.sleep(0.5 * (attempt + 1))
        for raw in response.headers.get_list("set-cookie"):
            name, _, rest = raw.partition("=")
            self.cookies[name.strip()] = rest.split(";", 1)[0]
        return response

    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("POST", path, **kw)

    def patch(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("PATCH", path, **kw)


@pytest.fixture(scope="module")
def base_url() -> str:
    url = os.environ.get("CLAIMOS_SYSTEM_URL")
    if not url:
        pytest.fail("Set CLAIMOS_SYSTEM_URL (e.g. http://127.0.0.1:8080) with the compose stack running")
    return url


@pytest.fixture(scope="module")
def admin(base_url: str) -> Iterator[SystemClient]:
    client = SystemClient(base_url)
    r = client.post(
        "/auth/register",
        json={
            "organization_name": "System Test Insurance (synthetic)",
            "display_name": "System Admin",
            "email": f"system-{uuid.uuid4().hex[:10]}@example.test",
            "password": PASSWORD,
        },
    )
    assert r.status_code == 201, r.text
    yield client
    client.http.close()


@pytest.fixture(scope="module")
def claim(admin: SystemClient) -> dict[str, Any]:
    from datetime import date, timedelta

    product = admin.post(
        "/products",
        json={
            "code": f"MOTOR-{uuid.uuid4().hex[:5].upper()}",
            "name": "Motor",
            "line": "motor",
            "currency": "GBP",
        },
    ).json()
    customer = admin.post("/customers", json={"full_name": "Rohan Tanaka", "is_synthetic": True}).json()
    start = date.today() - timedelta(days=100)
    policy = admin.post(
        "/policies",
        json={
            "policy_number": f"NWM-M-{uuid.uuid4().int % 100000:05d}",
            "customer_id": customer["id"],
            "product_id": product["id"],
            "is_synthetic": True,
            "publish_initial_version": True,
            "initial_version": {
                "effective_from": start.isoformat(),
                "effective_to": (start + timedelta(days=365)).isoformat(),
                "sum_insured": "20000.00",
                "deductible_amount": "250.00",
                "clauses": [
                    {
                        "clause_ref": "1",
                        "section": "coverage",
                        "title": "Cover",
                        "text": "Accidental damage cover.",
                    }
                ],
                "coverages": [
                    {
                        "coverage_code": "own_damage",
                        "perils": ["collision"],
                        "limit_amount": "20000.00",
                        "clause_ref": "1",
                    }
                ],
            },
        },
    ).json()
    r = admin.post(
        "/claims",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json={
            "customer_id": customer["id"],
            "policy_id": policy["id"],
            "loss_type": "collision",
            "incident_date": (date.today() - timedelta(days=5)).isoformat(),
            "is_synthetic": True,
            "description": "Rear bumper damaged in a low-speed collision.",
            "claimed_amount": {"amount": "1475.00", "currency": "GBP"},
        },
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def wait_for_documents(
    client: SystemClient, claim_id: str, expected: int, timeout: float = 300
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while True:
        docs: list[dict[str, Any]] = client.get(f"/claims/{claim_id}/documents").json()
        done = [
            d for d in docs if d["processing_status"] in ("processed", "needs_review", "rejected", "failed")
        ]
        if len(docs) >= expected and len(done) == len(docs):
            return docs
        if time.monotonic() > deadline:
            pytest.fail(
                f"Documents still processing after {timeout}s: "
                f"{[(d['original_filename'], d['processing_status']) for d in docs]}"
            )
        time.sleep(2)
