"""Phase 9 human review on the running stack (API, Celery workers, OCR, real agents).

Claims with real synthetic documents run through every agent; the product has no STP, so the
recommendation goes to a person. A reviewer within authority approves the calculated payout and the
worker finishes the run. A second claim's reviewer changes the amount (an override), which waits
for a different authorised person before the claim is approved.
"""

from __future__ import annotations

import time
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tests.system.conftest import PASSWORD, SystemClient
from tests.system.test_agents_live import _claim_with_documents, book  # noqa: F401 - shared fixture

pytestmark = pytest.mark.system


def _wait(admin: SystemClient, execution_id: str, done: tuple[str, ...]) -> dict[str, Any]:
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        ex: dict[str, Any] = admin.get(f"/workflow-executions/{execution_id}").json()
        if ex["status"] in done:
            return ex
        time.sleep(2)
    pytest.fail(f"execution {execution_id} did not reach {done}")


def _to_review(
    admin: SystemClient,
    book: dict[str, Any],  # noqa: F811
    description: str,
) -> tuple[str, str, dict[str, Any]]:
    claim_id = _claim_with_documents(admin, book, description)
    started = admin.post(f"/claims/{claim_id}/processing", headers={"Idempotency-Key": uuid.uuid4().hex})
    assert started.status_code == 202, started.text
    ex = _wait(admin, started.json()["id"], ("interrupted", "completed", "failed"))
    assert ex["status"] == "interrupted", (ex["status"], ex.get("error_detail"))
    assert ex["interrupt"]["kind"] == "human_review", ex["interrupt"]
    ws: dict[str, Any] = admin.get(f"/claims/{claim_id}/reviews/{ex['interrupt']['review_id']}").json()
    return claim_id, ex["id"], ws


def _reviewer(
    admin: SystemClient, base_url: str, limit: str, *, fraud_override: bool = False
) -> tuple[SystemClient, str]:
    invited = admin.post(
        "/users",
        json={
            "email": f"reviewer-{uuid.uuid4().hex[:10]}@example.test",
            "display_name": "Synthetic Reviewer",
            "roles": ["reviewer"],
        },
    )
    assert invited.status_code == 201, invited.text
    token = invited.json()["access_link"]["url"].split("#token=", 1)[1]
    client = SystemClient(base_url)
    accepted = client.post("/auth/invitations/accept", json={"token": token, "password": PASSWORD})
    assert accepted.status_code == 200, accepted.text
    user_id: str = invited.json()["user"]["id"]
    grant = admin.request(
        "PUT",
        f"/users/{user_id}/authorities",
        json=[
            {
                "max_approval_amount": limit,
                "currency": "GBP",
                "valid_from": (date.today() - timedelta(days=1)).isoformat(),
                "can_reject": True,
                "can_override_fraud": fraud_override,
            }
        ],
    )
    assert grant.status_code == 200, grant.text
    return client, user_id


def _key() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


def test_reviewer_approves_the_recommended_payout(
    admin: SystemClient,
    base_url: str,
    book: dict[str, Any],  # noqa: F811
) -> None:
    claim_id, execution_id, ws = _to_review(
        admin, book, "Rear-ended at a junction in Leeds by a delivery van while stationary."
    )
    review = ws["review"]
    assert review["recommendation"], review  # produced by the real decision agent
    assert review["payout"] is not None and Decimal(review["payout"]) > 0, review
    assert ws["waiting"] and "approve" in ws["actions"]
    reviewer, rid = _reviewer(admin, base_url, "2000.00")
    assert any(q["id"] == review["id"] for q in reviewer.get("/reviews").json())

    url = f"/claims/{claim_id}/reviews/{review['id']}"
    body = {"approved_amount": review["payout"], "reason": "estimate and photos consistent"}
    if review["recommendation"] != "APPROVE_RECOMMENDED":
        body["override_reason"] = "documents verified by reviewer"
    first = reviewer.post(f"{url}/approve", json=body, headers=_key())
    assert first.status_code == 200, first.text
    if first.json()["status"] == "awaiting_second_approval":  # override or high risk: four-eyes
        second, _ = _reviewer(admin, base_url, "2000.00")
        confirmed = second.post(f"{url}/second-approval", json={}, headers=_key())
        assert confirmed.status_code == 200, confirmed.text
    ex = _wait(admin, execution_id, ("completed", "failed"))
    assert ex["status"] == "completed", ex.get("error_detail")
    assert admin.get(f"/claims/{claim_id}").json()["status"] == "APPROVED"
    decision = admin.get(f"/claims/{claim_id}/decisions").json()[-1]
    assert (decision["decision"], decision["decision_mode"]) == ("approved", "human")
    assert decision["decided_by"] == rid
    assert Decimal(decision["approved_amount"]) == Decimal(review["payout"])
    completed = [e["node"] for e in ex["events"] if e["type"] == "node_completed"]
    assert completed[-1] == "HUMAN_REVIEW" and "FINALIZATION" not in completed


def test_changed_amount_needs_a_second_approver(
    admin: SystemClient,
    base_url: str,
    book: dict[str, Any],  # noqa: F811
) -> None:
    claim_id, execution_id, ws = _to_review(
        admin, book, "Reversing collision in a Leeds car park; rear bumper and sensor damaged."
    )
    review = ws["review"]
    assert review["payout"] is not None, review
    # The second claim on this policy within days is a likely duplicate, so the real fraud agent may
    # score it high: these reviewers hold fraud-override authority (four-eyes applies either way).
    first, fid = _reviewer(admin, base_url, "5000.00", fraud_override=True)
    second, sid = _reviewer(admin, base_url, "5000.00", fraud_override=True)
    url = f"/claims/{claim_id}/reviews/{review['id']}"
    amount = str(Decimal(review["payout"]) + Decimal("50.00"))
    plain = {"approved_amount": amount, "reason": "towing"}
    no_reason = first.post(f"{url}/approve", json=plain, headers=_key())
    assert no_reason.status_code == 422 and "override reason" in no_reason.json()["detail"]
    body = {**plain, "reason": "towing invoice accepted", "override_reason": "towing receipt"}
    held = first.post(f"{url}/approve", json=body, headers=_key())
    assert held.status_code == 200 and held.json()["status"] == "awaiting_second_approval", held.text
    assert admin.get(f"/claims/{claim_id}").json()["status"] == "HUMAN_REVIEW"
    same = first.post(f"{url}/second-approval", json={}, headers=_key())
    assert same.status_code == 403 and same.json()["code"] == "AUTHORITY_EXCEEDED"
    ok = second.post(f"{url}/second-approval", json={"note": "receipt checked"}, headers=_key())
    assert ok.status_code == 200, ok.text
    ex = _wait(admin, execution_id, ("completed", "failed"))
    assert ex["status"] == "completed", ex.get("error_detail")
    decision = admin.get(f"/claims/{claim_id}/decisions").json()[-1]
    assert decision["override"] and decision["override_reason"] == "towing receipt"
    assert (decision["decided_by"], decision["second_approver_id"]) == (fid, sid)
    assert Decimal(decision["approved_amount"]) == Decimal(amount)
    assert admin.get(f"/claims/{claim_id}").json()["status"] == "APPROVED"
