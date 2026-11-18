"""Phase 8 straight-through processing on the running stack.

A product publishes a rule set that enables STP up to 5,000 GBP. A clean claim with real documents
then runs through the graph, passes every gate re-evaluated from persisted data, and is approved
straight through with an append-only ``stp`` decision. A second claim on the same policy within
three days is a possible duplicate (fraud medium), so it goes to a person instead.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest

from tests.system.conftest import SystemClient
from tests.system.test_agents_live import _claim_with_documents, book  # noqa: F401 - shared fixture

pytestmark = pytest.mark.system


def _run(admin: SystemClient, claim_id: str) -> dict[str, Any]:
    started = admin.post(f"/claims/{claim_id}/processing", headers={"Idempotency-Key": uuid.uuid4().hex})
    assert started.status_code == 202, started.text
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        ex: dict[str, Any] = admin.get(f"/workflow-executions/{started.json()['id']}").json()
        if ex["status"] in ("completed", "interrupted", "failed"):
            return ex
        time.sleep(2)
    pytest.fail("processing did not finish")


def test_clean_claim_is_approved_straight_through(admin: SystemClient, book: dict[str, Any]) -> None:  # noqa: F811
    product_id = book["policy"]["product_id"]
    draft = admin.post(f"/products/{product_id}/rule-sets", json={"change_note": "enable STP"}).json()
    config = {
        **draft["config"],
        "automated_approval_allowed": True,
        "stp": {"enabled": True, "max_amount": "5000.00", "max_fraud_score": 25, "claimant_flag_months": 12},
    }
    updated = admin.patch(f"/rule-sets/{draft['id']}", json={"config": config}, headers={"If-Match": '"1"'})
    assert updated.status_code == 200, updated.text
    published = admin.post(f"/rule-sets/{draft['id']}/publish", headers={"If-Match": '"2"'})
    assert published.status_code == 200, published.text

    claim_id = _claim_with_documents(
        admin, book, "Rear-ended at a junction in Leeds by a delivery van while stationary."
    )
    ex = _run(admin, claim_id)
    nodes = [e["node"] for e in ex["events"] if e["type"] == "node_completed"]
    assert ex["status"] == "completed", (ex["status"], ex["error_detail"], ex.get("interrupt"))
    assert nodes[-2:] == ["AUTO_APPROVAL_ELIGIBILITY", "FINALIZATION"] and "HUMAN_REVIEW" not in nodes
    assert admin.get(f"/claims/{claim_id}").json()["status"] == "APPROVED"
    decisions = admin.get(f"/claims/{claim_id}/decisions").json()
    assert len(decisions) == 1
    d = decisions[0]
    assert d["decision_mode"] == "stp" and d["decision"] == "approved" and d["decided_by"] is None
    assert d["approved_amount"] == "815.0000" and d["rule_set_id"] == draft["id"]
    assert all(g["passed"] for g in d["gates"]) and len(d["gates"]) == 12
    runs = admin.get(f"/claims/{claim_id}/agent-runs").json()
    assert {r["rule_set_version"] for r in runs} == {f"rules_v1:{draft['id'][:8]}"}

    # Same policy, same incident date: a possible duplicate is never paid automatically.
    second = _claim_with_documents(
        admin, book, "Rear-ended at a junction in Leeds by a delivery van while stationary."
    )
    ex2 = _run(admin, second)
    assert ex2["status"] == "interrupted" and ex2["interrupt"]["kind"] == "human_review"
    assert admin.get(f"/claims/{second}/decisions").json() == []
    admin.post(f"/claims/{second}/processing/cancel")
