"""Phase 10 metric query tests: every dashboard and analytics figure is checked against a known dataset
in real PostgreSQL (exit criterion "every metric traced to a query test").

Claims are created through the API; executions, events, agent runs, model calls, decisions, reviews
and extracted fields with exact durations, costs and outcomes are inserted with the migrator role, so
each expected value below can be worked out by hand. Then the API is read as an org admin, as an
adjuster who only sees one claim, and as another organisation.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models.agents import AgentOutput, AgentRun
from app.models.ai import ModelInvocation
from app.models.documents import ClaimDocument, ExtractedField, FieldCorrection
from app.models.rules import ClaimDecision, HumanReview
from app.models.workflow import WorkflowEvent, WorkflowExecution
from app.services.dashboard_service import SUMMARY_KEYS
from tests.integration.claims_helpers import claim_payload, create_claim, setup_book
from tests.integration.conftest import ApiClient, accept, invite, register

pytestmark = pytest.mark.integration
NOW = datetime.now(UTC)
Factory = Callable[[], ApiClient]
U = uuid.UUID


def _execution(org: U, claim: U, status: str, wall_ms: int, age: timedelta) -> WorkflowExecution:
    finished = NOW - age
    return WorkflowExecution(
        id=uuid.uuid4(),
        organization_id=org,
        claim_id=claim,
        graph_name="claim_processing",
        graph_version="test",
        status=status,
        trigger="initial",
        created_at=finished - timedelta(minutes=5),
        started_at=finished - timedelta(minutes=5),
        finished_at=finished,
        duration_ms=wall_ms,
        error_code="NodeFailedError" if status == "failed" else None,
    )


def _event(org: U, ex: WorkflowExecution, seq: int, duration_ms: int) -> WorkflowEvent:
    return WorkflowEvent(
        id=uuid.uuid4(),
        organization_id=org,
        execution_id=ex.id,
        claim_id=ex.claim_id,
        seq=seq,
        event_type="node_completed",
        node=f"N{seq}",
        payload={"duration_ms": duration_ms},
        occurred_at=NOW,
    )


def _run(
    org: U,
    claim: U,
    key: str,
    *,
    status: str = "succeeded",
    attempt: int = 1,
    cost: str = "0",
    duration_ms: int = 100,
    used_llm: bool = False,
) -> AgentRun:
    started = NOW - timedelta(hours=1)
    return AgentRun(
        id=uuid.uuid4(),
        organization_id=org,
        claim_id=claim,
        agent_key=key,
        node=key.upper(),
        attempt=attempt,
        status=status,
        started_at=started,
        finished_at=started + timedelta(milliseconds=duration_ms),
        duration_ms=duration_ms,
        rule_set_version="rules_v1-default",
        input_hash="0" * 64,
        used_llm=used_llm,
        escalate=False,
        findings=[],
        missing=[],
        prompt_tokens=10 if used_llm else 0,
        completion_tokens=5 if used_llm else 0,
        cost_usd=Decimal(cost),
    )


def _output(org: U, run: AgentRun, output: dict[str, Any]) -> AgentOutput:
    return AgentOutput(
        id=uuid.uuid4(),
        organization_id=org,
        agent_run_id=run.id,
        schema_name="Test",
        schema_version="1",
        output=output,
        validation_errors=[],
        output_hash="0" * 64,
    )


def _decision(org: U, admin_id: U, claim: U, kind: str, mode: str, override: bool) -> ClaimDecision:
    return ClaimDecision(
        id=uuid.uuid4(),
        organization_id=org,
        claim_id=claim,
        decision=kind,
        currency="USD",
        approved_amount=Decimal("815.00") if kind == "approved" else None,
        decided_by=None if mode == "stp" else admin_id,
        decision_mode=mode,
        override=override,
        created_at=NOW - timedelta(hours=2),
    )


def _review(org: U, claim: U, ex: WorkflowExecution, status: str, due: timedelta) -> HumanReview:
    return HumanReview(
        id=uuid.uuid4(),
        organization_id=org,
        claim_id=claim,
        execution_id=ex.id,
        interrupt_id="review-0",
        currency="USD",
        reasons=[],
        status=status,
        priority_score=10,
        due_at=NOW + due,
        four_eyes_required=status == "awaiting_second_approval",
    )


def _model_call(org: U, run: AgentRun, latency: int, status: str) -> ModelInvocation:
    return ModelInvocation(
        id=uuid.uuid4(),
        organization_id=org,
        agent_key="fraud",
        purpose="fraud",
        model_slug="test/model:free",
        attempt=1,
        status=status,
        latency_ms=latency,
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=Decimal("0"),
        request_sha256="0" * 64,
        data_classification="synthetic",
        agent_run_id=run.id,
        created_at=NOW - timedelta(hours=1),
    )


def _field(org: U, doc: ClaimDocument, name: str, source: str, grounding: str) -> ExtractedField:
    return ExtractedField(
        id=uuid.uuid4(),
        organization_id=org,
        document_id=doc.id,
        field_name=name,
        value_text="x",
        value_normalized="x",
        value_type="text",
        confidence=0.9,
        grounding_status=grounding,
        validation_status="unverified",
        source=source,
        is_current=True,
        created_at=NOW - timedelta(hours=1),
    )


async def _seed(engine: AsyncEngine, org: U, admin_id: U, c1: U, c2: U, c3: U) -> None:
    e1 = _execution(org, c1, "completed", 60_000, timedelta(days=1))
    e2 = _execution(org, c2, "completed", 120_000, timedelta(days=2))
    e3 = _execution(org, c3, "failed", 5_000, timedelta(hours=3))
    old = _execution(org, c1, "completed", 999_000, timedelta(days=60))  # outside the 30-day window
    f1 = _run(org, c1, "fraud", cost="0.01")
    f2 = _run(org, c2, "fraud", cost="0.02", used_llm=True, duration_ms=300)
    f3 = _run(org, c3, "fraud", cost="0.03", duration_ms=500)
    failed_doc = _run(org, c1, "document", status="failed", attempt=2)
    assess = _run(org, c1, "assessment")
    two_flags = [{"rule_code": "F-DUP-CLAIM"}, {"rule_code": "F-ID-MISMATCH"}]
    doc = ClaimDocument(
        id=uuid.uuid4(),
        organization_id=org,
        claim_id=c1,
        version_no=1,
        original_filename="invoice.pdf",
        declared_mime="application/pdf",
        detected_mime="application/pdf",
        size_bytes=10,
        sha256="1" * 64,
        storage_bucket="test",
        storage_key=f"test/{uuid.uuid4()}",
        scan_status="clean",
        processing_status="processed",
        document_type="invoice",
    )
    rows: list[Any] = [
        e1,
        e2,
        e3,
        old,
        _event(org, e1, 1, 1_000),
        _event(org, e1, 2, 2_000),  # machine 3,000
        _event(org, e2, 1, 4_000),
        _event(org, e2, 2, 5_000),  # machine 9,000
        f1,
        f2,
        f3,
        failed_doc,
        assess,
        doc,
    ]
    later: list[Any] = [
        _output(org, f1, {"risk_level": "low", "risk_score": 5, "indicators": []}),
        _output(org, f2, {"risk_level": "high", "risk_score": 70, "indicators": two_flags}),
        _output(org, f3, {"risk_level": "critical", "risk_score": 90, "indicators": two_flags[:1]}),
        _output(org, assess, {"status": "calculated", "eligible_payout": "815.00", "currency": "USD"}),
        _model_call(org, f2, 100, "ok"),
        _model_call(org, f2, 300, "provider_error"),
        _decision(org, admin_id, c1, "approved", "stp", False),
        _decision(org, admin_id, c2, "approved", "human", True),
        _decision(org, admin_id, c3, "rejected", "human", False),
        _review(org, c3, e3, "pending", -timedelta(hours=1)),
        _review(org, c2, e2, "awaiting_second_approval", timedelta(days=1)),
    ]
    wrong = _field(org, doc, "invoice_no", "rules", "grounded")
    manual = _field(org, doc, "invoice_no", "manual", "not_applicable")
    fields = [
        _field(org, doc, "total", "rules", "grounded"),
        wrong,
        _field(org, doc, "date", "llm", "ungrounded"),
        manual,
    ]
    async with AsyncSession(engine, expire_on_commit=False) as s:
        parents = [e1, e2, e3, old, f1, f2, f3, failed_doc, assess, doc]
        children = [r for r in rows if r not in parents]
        for batch in (parents, children, later, fields):  # no ORM relationships: insert in FK order
            s.add_all(batch)
            await s.flush()
        s.add(
            FieldCorrection(
                id=uuid.uuid4(),
                organization_id=org,
                document_id=doc.id,
                previous_field_id=wrong.id,
                new_field_id=manual.id,
                field_name="invoice_no",
                previous_value="x",
                new_value="y",
                reason="typo",
                corrected_by=admin_id,
                created_at=NOW,
            )
        )
        await s.execute(text("UPDATE claims SET status = 'APPROVED' WHERE id = :c"), {"c": c1})
        await s.commit()


async def _book(admin: ApiClient, engine: AsyncEngine) -> dict[str, Any]:
    me = admin.get("/auth/me").json()
    org, admin_id = uuid.UUID(me["organization"]["id"]), uuid.UUID(me["user"]["id"])
    _, customer, policy = setup_book(admin)
    ids = []
    for _ in range(3):
        created = create_claim(admin, claim_payload(customer, policy))
        assert created.status_code == 201, created.text
        ids.append(created.json()["id"])
    await _seed(engine, org, admin_id, *(uuid.UUID(i) for i in ids))
    return {"org": org, "claims": ids, "product_id": policy["product_id"]}


def _metrics(client: ApiClient, **params: Any) -> dict[str, Any]:
    r = client.get("/dashboard/summary", params=params)
    assert r.status_code == 200, r.text
    return {m["key"]: m["value"] for m in r.json()["metrics"]}


async def test_every_dashboard_metric_matches_its_query(
    new_client: Factory, migrator_engine: AsyncEngine
) -> None:
    admin = new_client()
    register(admin)
    await _book(admin, migrator_engine)
    expected = {
        "claims_received": 3,
        "open_claims": 2,  # one claim was approved
        "in_review": 2,
        "overdue_reviews": 1,
        "awaiting_second_approval": 1,
        "approved": 2,
        "rejected": 1,
        "stp_rate": 0.5,  # 1 straight-through of 2 approvals
        "override_rate": 0.5,  # 1 override among 2 human approve/reject decisions
        "median_wall_ms": 90_000.0,  # completed runs in the window: 60 s and 120 s (999 s is 60 days old)
        "median_machine_ms": 6_000.0,  # summed steps: 3 s and 9 s
        "failed_runs": 1,
        "fraud_alerts": 2,  # high + critical
        "model_cost_usd": 0.06,
    }
    assert set(expected) == set(SUMMARY_KEYS)  # a new metric without a query test fails here
    assert _metrics(admin, days=30) == pytest.approx(expected)
    body = admin.get("/dashboard/summary").json()
    assert body["scope"] == "organisation" and all(m["definition"] for m in body["metrics"])

    # synthetic data excluded: every count is 0 and every rate is unknown, not 0%
    none = _metrics(admin, include_synthetic="false")
    assert none["claims_received"] == 0 and none["stp_rate"] is None and none["median_wall_ms"] is None


async def test_scope_follows_claim_visibility_and_tenancy(
    new_client: Factory, migrator_engine: AsyncEngine
) -> None:
    admin = new_client()
    register(admin)
    book = await _book(admin, migrator_engine)
    adjuster = new_client()
    me = accept(adjuster, invite(admin, ["claims_adjuster"]))
    r = admin.put(
        f"/claims/{book['claims'][0]}/assignments", json={"role": "adjuster", "user_id": me["user"]["id"]}
    )
    assert r.status_code == 200, r.text
    mine = _metrics(adjuster)
    assert adjuster.get("/dashboard/summary").json()["scope"] == "assigned claims"
    assert (mine["claims_received"], mine["approved"], mine["rejected"], mine["fraud_alerts"]) == (1, 1, 0, 0)
    assert mine["stp_rate"] == 1.0 and mine["model_cost_usd"] == pytest.approx(0.01)

    other = new_client()
    register(other)
    theirs = _metrics(other)
    assert all(theirs[k] in (0, None) for k in SUMMARY_KEYS)
    assert other.get("/analytics/agents").json()["agents"] == []


async def test_analytics_queries(new_client: Factory, migrator_engine: AsyncEngine) -> None:
    admin = new_client()
    register(admin)
    book = await _book(admin, migrator_engine)

    times = admin.get("/analytics/processing-times").json()["products"]
    assert len(times) == 1 and times[0]["product_id"] == book["product_id"]
    t = times[0]
    assert t["runs"] == 2 and t["wall_avg_ms"] == 90_000.0 and t["wall_p50_ms"] == 90_000.0
    assert (t["wall_p90_ms"], t["wall_p95_ms"]) == pytest.approx((114_000.0, 117_000.0))
    machine = (t["machine_p50_ms"], t["machine_p90_ms"], t["machine_p95_ms"])
    assert machine == pytest.approx((6_000.0, 8_400.0, 8_700.0))
    other_product = {"product_id": str(uuid.uuid4())}
    assert admin.get("/analytics/processing-times", params=other_product).json()["products"] == []

    claims = admin.get("/analytics/claims", params={"bucket": "week"}).json()
    assert sum(v["count"] for v in claims["volume"]) == 3
    assert sum(c["count"] for c in claims["by_category"]) == 3
    assert {s["status"]: s["count"] for s in claims["by_status"]} == {"APPROVED": 1, "SUBMITTED": 2}
    assert {(d["decision"], d["mode"]): d["count"] for d in claims["decisions"]} == {
        ("approved", "human"): 1,
        ("approved", "stp"): 1,
        ("rejected", "human"): 1,
    }

    fraud_r = admin.get("/analytics/fraud")
    assert fraud_r.status_code == 200, fraud_r.text
    fraud = fraud_r.json()
    assert fraud["by_level"] == {"low": 1, "high": 1, "critical": 1} and fraud["alerts"] == 2
    assert fraud["top_indicators"] == [
        {"code": "F-DUP-CLAIM", "count": 2},
        {"code": "F-ID-MISMATCH", "count": 1},
    ]
    # flagged + final human decision: c2 approved (a false positive), c3 rejected
    labelled = (fraud["labelled_flagged"], fraud["false_positives"], fraud["false_positive_rate"])
    assert labelled == (2, 1, 0.5)

    agents = admin.get("/analytics/agents").json()
    by_key = {a["agent_key"]: a for a in agents["agents"]}
    assert set(by_key) == {"assessment", "document", "fraud"}
    assert by_key["document"]["failure_rate"] == 1.0 and by_key["document"]["retries"] == 1
    f = by_key["fraud"]
    assert (f["runs"], f["failed"], f["failure_rate"], f["p50_ms"], f["llm_rate"]) == (
        3,
        0,
        0.0,
        300.0,
        0.3333,
    )
    assert f["cost_usd"] == pytest.approx(0.06) and f["tokens"] == 15
    assert agents["claims"] == 3 and agents["cost_per_claim_usd"] == pytest.approx(0.02)
    assert agents["models"] == [
        {
            "model_slug": "test/model:free",
            "calls": 2,
            "errors": 1,
            "error_rate": 0.5,
            "p95_latency_ms": pytest.approx(290.0),
            "cost_usd": 0.0,
        },
    ]

    extraction = admin.get("/analytics/extraction-accuracy").json()["document_types"]
    assert extraction == [  # the manual correction itself is not a machine extraction
        {"document_type": "invoice", "fields": 3, "corrected": 1, "accuracy": 0.6667, "ungrounded": 1},
    ]

    register_rows = admin.get("/assessments").json()
    assert [(a["claim_id"], a["assessment_status"], a["eligible_payout"]) for a in register_rows] == [
        (book["claims"][0], "calculated", "815.00"),
    ]


async def test_activity_lists_live_work_and_exceptions(
    new_client: Factory, migrator_engine: AsyncEngine
) -> None:
    admin = new_client()
    register(admin)
    book = await _book(admin, migrator_engine)
    activity = admin.get("/dashboard/activity").json()
    assert activity["running"] == [] and activity["waiting"] == {}
    assert [f["claim_id"] for f in activity["recent_failures"]] == [book["claims"][2]]
    assert activity["recent_failures"][0]["error_code"] == "NodeFailedError"
    assert [p["claim_id"] for p in activity["pending_approvals"]] == [book["claims"][1]]
    assert [o["claim_id"] for o in activity["overdue_reviews"]] == [book["claims"][2]]
    assert admin.get("/dashboard/summary", params={"days": 0}).status_code == 422
