"""Phase 7 orchestration on the running stack (real uploads, ClamAV, OCR, workflow worker, beat).

1. End to end: a synthetic motor claim with four documents runs through the LangGraph graph
   (parallel policy/fraud branches, join, aggregation, decision, compliance) and stops at the
   HUMAN_REVIEW interrupt; live events stream over SSE through nginx; a reviewer returns it for
   reassessment (CLAIM_REASSESSMENT -> back to review); cancelling returns the claim to SUBMITTED.
2. Crash recovery: the workflow worker is killed (SIGKILL) while the graph waits in
   DOCUMENT_PROCESSING; after restart the beat task detects the stale heartbeat and LangGraph resumes
   from the last checkpoint (INTAKE is not re-run).
Needs the ``docker`` CLI for the kill test.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from typing import Any

import pytest

from evaluations.builders import driving_licence, motor_claim_form, motor_estimate
from evaluations.renderers import photo_image, text_pdf
from tests.system.conftest import SystemClient, wait_for_documents
from tests.system.test_agents_live import _claim_with_documents, book  # noqa: F401 - shared fixture

pytestmark = pytest.mark.system
MAIN_NODES = (
    "INTAKE",
    "DOCUMENT_PROCESSING",
    "DOCUMENT_VALIDATION",
    "POLICY_VERIFICATION",
    "FRAUD_ANALYSIS",
    "CLAIM_ASSESSMENT",
    "RESULT_AGGREGATION",
    "CROSS_VALIDATION",
    "DECISION_RECOMMENDATION",
    "COMPLIANCE_CHECK",
)
STAGES = [
    "DOCUMENT_PROCESSING",
    "POLICY_VERIFICATION",
    "FRAUD_ANALYSIS",
    "CLAIM_ASSESSMENT",
    "VALIDATION",
    "DECISION_PENDING",
    "HUMAN_REVIEW",
]


def _wait(predicate: Any, timeout: float, what: str, every: float = 2.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(every)
    pytest.fail(f"Timed out waiting for {what}")


def _execution(admin: SystemClient, execution_id: str) -> dict[str, Any]:
    body: dict[str, Any] = admin.get(f"/workflow-executions/{execution_id}").json()
    return body


def _docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True, timeout=120)  # noqa: S603, S607


def test_end_to_end_to_human_review_with_live_events(
    admin: SystemClient,
    book: dict[str, Any],  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    claim_id = _claim_with_documents(
        admin, book, "Rear-ended at a junction in Leeds by a delivery van while stationary."
    )
    started = admin.post(f"/claims/{claim_id}/processing", headers={"Idempotency-Key": uuid.uuid4().hex})
    assert started.status_code == 202, started.text
    execution_id = started.json()["id"]

    # Live events through nginx (SSE): read until the run stops at the review step.
    seen: list[dict[str, Any]] = []
    cookie = "; ".join(f"{k}={v}" for k, v in admin.cookies.items())
    with admin.http.stream(
        "GET", f"/workflow-executions/{execution_id}/events", headers={"Cookie": cookie}, timeout=300
    ) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("data: "):
                seen.append(json.loads(line[6:]))
                if seen[-1]["type"] in ("interrupt", "execution_failed"):
                    break
    ex = _execution(admin, execution_id)
    timeline = [
        (e["seq"], e["type"], e["node"], str(e["payload"].get("summary", ""))[:60])
        for e in ex["events"]
        if e["type"] in ("node_completed", "interrupt")
    ]
    with capsys.disabled():
        print("\nTimeline:", timeline)
    assert ex["status"] == "interrupted", ex["error_detail"]
    assert [e["seq"] for e in seen] == [e["seq"] for e in ex["events"]][
        : len(seen)
    ]  # the stream matched the outbox
    interrupt = ex["interrupt"]
    assert interrupt["kind"] == "human_review" and interrupt["recommendation"] == "APPROVE_RECOMMENDED"
    completed = [e["node"] for e in ex["events"] if e["type"] == "node_completed"]
    assert set(MAIN_NODES) <= set(completed) and float(ex["progress"]) == 1.0
    began = {e["node"]: e["seq"] for e in ex["events"] if e["type"] == "node_started"}
    done = {e["node"]: e["seq"] for e in ex["events"] if e["type"] == "node_completed"}
    assert (
        began["FRAUD_ANALYSIS"] < done["POLICY_VERIFICATION"]
        or began["POLICY_VERIFICATION"] < done["FRAUD_ANALYSIS"]
    )
    assert (
        max(done["FRAUD_ANALYSIS"], done["CLAIM_ASSESSMENT"]) < began["RESULT_AGGREGATION"]
    )  # join waits for both
    assert admin.get(f"/claims/{claim_id}").json()["status"] == "HUMAN_REVIEW"
    assert [e["payload"]["to"] for e in ex["events"] if e["type"] == "status_changed"] == STAGES
    runs = admin.get(f"/claims/{claim_id}/agent-runs").json()
    assert {r["agent_key"] for r in runs} == {
        "intake",
        "document",
        "policy",
        "fraud",
        "assessment",
        "validation",
        "decision",
        "compliance",
    }

    # Reviewer returns it for reassessment (Phase 9: through the review API, never the generic
    # resume): only the assessment and downstream nodes re-run.
    generic = admin.post(
        f"/workflow-executions/{execution_id}/resume",
        json={"interrupt_id": interrupt["interrupt_id"], "action": "reassess"},
    )
    assert generic.status_code == 422
    resumed = admin.post(
        f"/claims/{claim_id}/reviews/{interrupt['review_id']}/return",
        json={
            "reason": "check the labour rate",
            "instructions": "use the published labour rate",
            "override_reason": "labour rate looks above the published rate",  # disagrees with APPROVE
        },
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert resumed.status_code == 200 and resumed.json()["outcome"] == "returned", resumed.text

    def second_review() -> dict[str, Any] | None:
        x = _execution(admin, execution_id)
        new = x["status"] == "interrupted" and x["interrupt"]["interrupt_id"] != interrupt["interrupt_id"]
        return x if new or x["status"] == "failed" else None

    again = _wait(second_review, 300, "second review")
    assert again["status"] == "interrupted" and again["reassessment_count"] == 1, again["error_detail"]
    last = ex["events"][-1]["seq"]
    rerun = [e["node"] for e in again["events"] if e["type"] == "node_completed" and e["seq"] > last]
    assert rerun[:2] == ["HUMAN_REVIEW", "CLAIM_REASSESSMENT"]  # the review step completes, then reassessment
    assert "FRAUD_ANALYSIS" not in rerun and "INTAKE" not in rerun

    cancelled = admin.post(f"/claims/{claim_id}/processing/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert admin.get(f"/claims/{claim_id}").json()["status"] == "SUBMITTED"


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker CLI to kill the worker")
def test_killed_worker_resumes_from_checkpoint(
    admin: SystemClient,
    book: dict[str, Any],  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    r = admin.post(
        "/claims",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json={
            "customer_id": book["customer"]["id"],
            "policy_id": book["policy"]["id"],
            "loss_type": "collision",
            "incident_date": "2026-10-08",
            "is_synthetic": True,
            "description": "Rear-ended at a junction in Leeds.",
            "claimed_amount": {"amount": "1475.00", "currency": "GBP"},
        },
    )
    assert r.status_code == 201, r.text
    claim_id = r.json()["id"]
    _docker(
        "compose", "stop", "worker-documents"
    )  # documents stay queued: the graph waits in DOCUMENT_PROCESSING
    try:
        files = [
            ("files", ("claim-form.pdf", text_pdf(motor_claim_form()), "application/pdf")),
            ("files", ("estimate.pdf", text_pdf(motor_estimate()), "application/pdf")),
            ("files", ("licence.pdf", text_pdf(driving_licence()), "application/pdf")),
            ("files", ("damage.jpg", photo_image(), "image/jpeg")),
        ]
        assert admin.post(f"/claims/{claim_id}/documents", files=files).status_code == 201
        started = admin.post(f"/claims/{claim_id}/processing")
        assert started.status_code == 202, started.text
        execution_id = started.json()["id"]
        _wait(
            lambda: any(
                e["type"] == "node_started" and e["node"] == "DOCUMENT_PROCESSING"
                for e in _execution(admin, execution_id)["events"]
            ),
            120,
            "DOCUMENT_PROCESSING to start",
        )
        _docker("kill", "claimos-worker-1")  # SIGKILL mid-node: no cleanup, no ack
    finally:
        _docker("compose", "start", "worker-documents")
    wait_for_documents(admin, claim_id, 4)
    _docker("start", "claimos-worker-1")
    ex = _wait(
        lambda: (x := _execution(admin, execution_id))["status"] in ("interrupted", "failed") and x,
        600,
        "recovered execution to reach review",
        every=3.0,
    )
    types = [e["type"] for e in ex["events"]]
    with capsys.disabled():
        print(
            "\nRecovery:",
            ex["status"],
            "recoveries",
            ex["recovery_count"],
            [t for t in types if t in ("retry", "recovered")],
        )
    assert ex["status"] == "interrupted" and ex["interrupt"]["kind"] == "human_review", ex["error_detail"]
    assert ex["recovery_count"] >= 1 and "retry" in types and "recovered" in types
    intake = [e for e in ex["events"] if e["type"] == "node_completed" and e["node"] == "INTAKE"]
    assert len(intake) == 1  # finished before the crash: restored from the checkpoint, not re-run
    assert [e["seq"] for e in ex["events"]] == list(range(1, len(ex["events"]) + 1))
    admin.post(f"/claims/{claim_id}/processing/cancel")
