"""Builders that create catalog data and claims through the real API."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from tests.integration.conftest import ApiClient

TODAY = date.today()


def make_product(client: ApiClient, line: str = "motor", currency: str = "USD") -> dict[str, Any]:
    code = f"{line.upper()}-{uuid.uuid4().hex[:6].upper()}"
    r = client.post(
        "/products",
        json={"code": code, "name": f"{line.title()} cover {code}", "line": line, "currency": currency},
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def make_customer(client: ApiClient, name: str = "Amelia Hartley") -> dict[str, Any]:
    r = client.post(
        "/customers",
        json={
            "full_name": name,
            "external_ref": f"CUST-{uuid.uuid4().hex[:8]}",
            "email": "amelia.hartley@example.test",
            "is_synthetic": True,
            "address": {"city": "Leeds", "country": "GB"},
        },
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def version_payload(
    start: date, end: date, sum_insured: str = "20000.00", waiting: int = 0
) -> dict[str, Any]:
    return {
        "effective_from": start.isoformat(),
        "effective_to": end.isoformat(),
        "sum_insured": sum_insured,
        "deductible_amount": "500.00",
        "waiting_period_days": waiting,
        "clauses": [
            {
                "clause_ref": "2.1",
                "section": "coverage",
                "title": "Own damage",
                "text": "We cover accidental damage to the insured vehicle, up to the scheduled limit.",
            },
            {
                "clause_ref": "4.3",
                "section": "exclusion",
                "title": "Racing",
                "text": "We do not cover damage while the vehicle is used for racing or speed testing.",
            },
        ],
        "coverages": [
            {
                "coverage_code": "own_damage",
                "perils": ["collision", "fire", "weather", "vandalism"],
                "limit_amount": sum_insured,
                "clause_ref": "2.1",
            }
        ],
        "exclusions": [
            {
                "exclusion_code": "racing",
                "applies_to": ["own_damage"],
                "description": "Use for racing or speed testing",
                "clause_ref": "4.3",
            }
        ],
    }


def make_policy(
    client: ApiClient,
    customer: dict[str, Any],
    product: dict[str, Any],
    start: date | None = None,
    end: date | None = None,
    publish: bool = True,
) -> dict[str, Any]:
    start = start or TODAY - timedelta(days=200)
    end = end or start + timedelta(days=365)
    r = client.post(
        "/policies",
        json={
            "policy_number": f"POL-{uuid.uuid4().hex[:8].upper()}",
            "customer_id": customer["id"],
            "product_id": product["id"],
            "is_synthetic": True,
            "initial_version": version_payload(start, end),
            "publish_initial_version": publish,
        },
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def claim_payload(customer: dict[str, Any], policy: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "customer_id": customer["id"],
        "policy_id": policy["id"],
        "loss_type": "collision",
        "incident_date": (TODAY - timedelta(days=5)).isoformat(),
        "incident_location": {
            "city": "Leeds",
            "country": "GB",
            "description": "Junction of A61 and Park Lane",
        },
        "description": "Rear-ended at a red light; rear bumper and tailgate damaged.",
        "claimed_amount": {"amount": "5200.00", "currency": "USD"},
        "is_synthetic": True,
    }
    payload.update(overrides)
    return payload


def create_claim(client: ApiClient, payload: dict[str, Any], key: str | None = None) -> Any:
    return client.post("/claims", json=payload, headers={"Idempotency-Key": key or uuid.uuid4().hex})


def setup_book(client: ApiClient) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    product = make_product(client)
    customer = make_customer(client)
    policy = make_policy(client, customer, product)
    return product, customer, policy
