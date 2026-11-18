"""Phase 12 on real PostgreSQL + pgvector (and the real model gateway for the evaluation's live part).

* Similar claims: real embeddings in pgvector, nearest neighbours restricted to visible claims.
* Anomalies: amount outliers (robust z), frequent claimants, near-identical narratives across customers,
  the same file on several claims.
* Feedback: labels written by human decisions, personal labels, curation cases.
* Gate: an evaluation run executes for real (deterministic set + live part through OpenRouter); activation of
  a prompt version or a new model is refused unless a passed run exists for exactly that candidate. The
  passing case uses a passed-run row inserted with the migrator role, the same row the executor writes.
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

from agents.base import load_prompt
from app.core.config import Settings
from app.core.db import Database
from app.models.agents import AgentOutput, AgentRun
from app.models.ai import ModelRegistryEntry
from app.models.documents import ClaimDocument
from app.models.intelligence import EvalRun
from app.services import eval_service
from tests.integration.claims_helpers import (
    claim_payload,
    create_claim,
    make_customer,
    make_policy,
    setup_book,
)
from tests.integration.conftest import ApiClient, accept, invite, register
from tests.integration.test_reviews_integration import _grant, _key, _member, _to_review

pytestmark = pytest.mark.integration
Factory = Callable[[], ApiClient]
STORY = (
    "Rear-ended at the junction of Park Lane while stationary at a red light; "
    "rear bumper and tailgate damaged."
)


def _org(client: ApiClient) -> uuid.UUID:
    return uuid.UUID(client.get("/auth/me").json()["organization"]["id"])


def _claim(client: ApiClient, customer: dict[str, Any], policy: dict[str, Any], **kw: Any) -> str:
    r = create_claim(client, claim_payload(customer, policy, **kw))
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _agent_outputs(
    engine: AsyncEngine, org: uuid.UUID, claim_id: uuid.UUID, outputs: dict[str, Any]
) -> None:
    async with AsyncSession(engine) as s:
        for key, output in outputs.items():
            now = datetime.now(UTC)
            run = AgentRun(
                id=uuid.uuid4(),
                organization_id=org,
                claim_id=claim_id,
                agent_key=key,
                node=key.upper(),
                attempt=1,
                status="succeeded",
                started_at=now,
                finished_at=now,
                duration_ms=5,
                rule_set_version="rules_v1-default",
                input_hash="0" * 64,
                used_llm=False,
                escalate=False,
                findings=[],
                missing=[],
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=Decimal("0"),
            )
            s.add(run)
            await s.flush()
            s.add(
                AgentOutput(
                    id=uuid.uuid4(),
                    organization_id=org,
                    agent_run_id=run.id,
                    schema_name="Test",
                    schema_version="1",
                    output=output,
                    validation_errors=[],
                    output_hash="0" * 64,
                )
            )
        await s.commit()


# --- similar claims ------------------------------------------------------------------------------


async def test_similar_claims_from_pgvector_respect_visibility(new_client: Factory) -> None:
    admin = new_client()
    register(admin)
    product, customer, policy = setup_book(admin)
    other_customer = make_customer(admin, "Priya Natarajan")
    other_policy = make_policy(admin, other_customer, product)
    a = _claim(admin, customer, policy, description=STORY)
    b = _claim(
        admin, other_customer, other_policy, description=STORY.replace("Park Lane", "Park Lane, Leeds")
    )
    c = _claim(admin, customer, policy, description="Hail storm dented the roof and cracked the windscreen.")

    found = admin.get(f"/claims/{a}/similar").json()
    assert found["model"] == "claimos-hash-256-v1"
    items = found["items"]
    assert items[0]["claim_id"] == b and items[0]["similarity"] > 0.9 and not items[0]["same_customer"]
    assert {"bumper", "tailgate"} <= set(items[0]["shared_terms"])
    by_id = {i["claim_id"]: i for i in items}
    assert c not in by_id or by_id[c]["similarity"] < 0.5

    current = admin.get(f"/claims/{c}")
    edited = admin.patch(
        f"/claims/{c}", json={"description": STORY}, headers={"If-Match": current.headers["etag"]}
    )
    assert edited.status_code == 200, edited.text
    again = {i["claim_id"]: i["similarity"] for i in admin.get(f"/claims/{a}/similar").json()["items"]}
    assert again[c] > 0.99  # the edited claim was re-embedded

    adjuster = new_client()
    me = accept(adjuster, invite(admin, ["claims_adjuster"]))
    admin.put(f"/claims/{a}/assignments", json={"role": "adjuster", "user_id": me["user"]["id"]})
    assert adjuster.get(f"/claims/{a}/similar").json()["items"] == []  # b and c are not visible to them
    stranger = new_client()
    register(stranger)
    assert stranger.get(f"/claims/{a}/similar").status_code == 404


# --- anomalies -----------------------------------------------------------------------------------


async def test_portfolio_anomalies(new_client: Factory, migrator_engine: AsyncEngine) -> None:
    admin = new_client()
    register(admin)
    org = _org(admin)
    product, customer, policy = setup_book(admin)
    other = make_customer(admin, "Tomasz Kowalczyk")
    other_policy = make_policy(admin, other, product)
    ids = [
        _claim(
            admin,
            customer,
            policy,
            claimed_amount={"amount": f"{1000 + 10 * i}.00", "currency": "USD"},
            description=f"Minor scrape number {i} on the passenger door in a car park.",
        )
        for i in range(30)
    ]
    outlier = _claim(
        admin,
        other,
        other_policy,
        claimed_amount={"amount": "48000.00", "currency": "USD"},
        description=STORY,
    )
    usual = {"amount": "1150.00", "currency": "USD"}
    twin = _claim(admin, customer, policy, claimed_amount=usual, description=STORY + " ")
    async with AsyncSession(migrator_engine) as s:
        for claim_id in (ids[0], ids[1]):
            s.add(
                ClaimDocument(
                    id=uuid.uuid4(),
                    organization_id=org,
                    claim_id=uuid.UUID(claim_id),
                    version_no=1,
                    original_filename="estimate.pdf",
                    declared_mime="application/pdf",
                    detected_mime="application/pdf",
                    size_bytes=10,
                    sha256="d" * 64,
                    storage_bucket="test",
                    storage_key=f"t/{uuid.uuid4()}",
                    scan_status="clean",
                    processing_status="processed",
                    document_type="repair_estimate",
                )
            )
        await s.commit()

    result = admin.get("/analytics/anomalies").json()
    kinds: dict[str, list[dict[str, Any]]] = {}
    for a in result["anomalies"]:
        kinds.setdefault(a["type"], []).append(a)
    amount = kinds["amount_outlier"]
    assert [x["claims"][0]["claim_id"] for x in amount] == [outlier] and amount[0]["score"] > 3.5
    frequent = kinds["frequent_claimant"]
    assert len(frequent) == 1 and frequent[0]["score"] == 31
    narrative = kinds["similar_narrative"]
    assert {c["claim_id"] for c in narrative[0]["claims"]} == {outlier, twin} and narrative[0]["score"] > 0.9
    shared = kinds["shared_document"]
    assert {c["claim_id"] for c in shared[0]["claims"]} == {ids[0], ids[1]}
    assert result["definition"] and result["notes"] == []

    small = new_client()
    register(small)
    _, cu, po = setup_book(small)
    _claim(small, cu, po)
    notes = small.get("/analytics/anomalies").json()["notes"]
    assert any("below the 30 needed" in n for n in notes)


# --- feedback ------------------------------------------------------------------------------------


async def test_decisions_and_people_label_agent_outputs(
    new_client: Factory, settings: Settings, migrator_engine: AsyncEngine
) -> None:
    admin = new_client()
    register(admin)
    case = await _to_review(admin, settings)
    await _agent_outputs(
        migrator_engine,
        case.org,
        uuid.UUID(case.claim_id),
        {
            "decision": {"recommendation": "REJECT_RECOMMENDED", "matrix_rule": "M2"},
            "fraud": {"risk_level": "high", "risk_score": 80, "indicators": []},
        },
    )
    async with migrator_engine.begin() as c:
        await c.execute(
            text("UPDATE human_reviews SET fraud_level = 'high' WHERE id = :i"), {"i": case.review_id}
        )
    a, aid = _member(new_client, admin, ["reviewer"])
    b, bid = _member(new_client, admin, ["reviewer"])
    _grant(admin, aid, can_override_fraud=True)
    _grant(admin, bid, can_override_fraud=True)
    body = {
        "approved_amount": "700.00",
        "reason": "photos verified",
        "override_reason": "fraud flag explained",
    }
    assert (
        a.post(case.url("approve"), json=body, headers=_key()).json()["status"] == "awaiting_second_approval"
    )
    assert b.post(case.url("second-approval"), json={}, headers=_key()).status_code == 200

    summary = {x["agent_key"]: x for x in admin.get("/analytics/feedback").json()["agents"]}
    assert summary["decision"]["counts"] == {"disagree": 1} and summary["decision"]["agreement_rate"] == 0.0
    assert summary["fraud"]["counts"] == {"false_positive": 1} and summary["fraud"]["precision"] == 0.0

    runs = {r["agent_key"]: r for r in admin.get(f"/claims/{case.claim_id}/agent-runs").json()}
    url = f"/agent-runs/{runs['fraud']['id']}/feedback"
    assert a.post(url, json={"label": "not_helpful", "note": "flag was a typo"}).status_code == 201
    assert a.post(url, json={"label": "helpful"}).status_code == 201  # replaces the earlier label
    assert a.post(url, json={"label": "excellent"}).status_code == 422
    fraud = {x["agent_key"]: x for x in admin.get("/analytics/feedback").json()["agents"]}["fraud"]
    assert fraud["counts"] == {"false_positive": 1, "helpful": 1} and fraud["helpful_rate"] == 1.0
    cases = admin.get("/intelligence/curation-cases").json()
    assert {(c["agent_key"], c["label"]) for c in cases} == {
        ("decision", "disagree"),
        ("fraud", "false_positive"),
    }
    stranger = new_client()
    register(stranger)
    assert stranger.post(url, json={"label": "helpful"}).status_code == 404


# --- evaluation runs and the activation gate -----------------------------------------------------


async def _model(engine: AsyncEngine, org: uuid.UUID, slug: str) -> uuid.UUID:
    model_id = uuid.uuid4()
    async with AsyncSession(engine) as s:
        s.add(
            ModelRegistryEntry(
                id=model_id,
                organization_id=org,
                provider="openrouter",
                slug=slug,
                display_name=slug,
                is_free=True,
                input_modalities=["text"],
                declared_capabilities={"json_mode": True},
                verified_capabilities={"json_mode": {"ok": True}},
                probe_status="passed",
                prompt_price_per_mtok=Decimal("0"),
                completion_price_per_mtok=Decimal("0"),
                data_policy="unknown",
                status="active",
                in_catalog=True,
                requests_per_minute=10,
            )
        )
        await s.commit()
    return model_id


async def _passed(
    engine: AsyncEngine,
    org: uuid.UUID,
    agent: str,
    version: str,
    model: uuid.UUID,
    *,
    age: timedelta = timedelta(0),
    sha: str | None = None,
) -> None:
    async with AsyncSession(engine) as s:
        s.add(
            EvalRun(
                id=uuid.uuid4(),
                organization_id=org,
                agent_key=agent,
                kind="prompt",
                prompt_version=version,
                prompt_sha256=sha or load_prompt(agent, version).sha256,
                model_id=model,
                status="passed",
                cases=10,
                metrics=[],
                parts=[],
                regressions=[],
                model_calls=3,
                created_at=datetime.now(UTC) - age,
                finished_at=datetime.now(UTC) - age,
            )
        )
        await s.commit()


async def test_no_prompt_or_model_activation_without_a_passing_regression(
    new_client: Factory, settings: Settings, migrator_engine: AsyncEngine
) -> None:
    admin = new_client()
    register(admin)
    org = _org(admin)
    assert admin.get("/agents").status_code == 200  # creates the agent configurations
    current = await _model(migrator_engine, org, "google/gemma-3-27b-it:free")
    candidate = await _model(migrator_engine, org, "nvidia/nemotron-3.5-super-vl:free")
    agents = {a["agent_key"]: a for a in admin.get("/agents").json()}
    fraud = agents["fraud"]
    first = admin.patch(
        "/agents/fraud",
        json={"primary_model_id": str(current)},
        headers={"If-Match": f'"{fraud["row_version"]}"'},
    )
    assert (
        first.status_code == 422 and "regression" in first.json()["detail"]
    )  # even the first model is gated

    prompts = {(p["agent_key"], p["version"]): p for p in admin.get("/prompts").json()}
    v2 = prompts[("fraud", "v2")]
    assert prompts[("fraud", "v1")]["active_for_org"] and not v2["active_for_org"]
    refused = admin.post(f"/prompts/{v2['id']}/activate")
    assert refused.status_code == 422 and "regression" in refused.json()["detail"]

    # A real evaluation run: the deterministic fraud set, then the live part through the gateway.
    requested = admin.post(
        "/evals", json={"agent_key": "fraud", "prompt_version": "v2", "model_id": str(candidate)}
    )
    assert requested.status_code == 202, requested.text
    db = Database.from_settings(settings, application_name="claimos-test-eval")
    try:
        status = await eval_service.execute(db, settings, org, uuid.UUID(requested.json()["id"]))
    finally:
        await db.dispose()
    run = admin.get(f"/evals/{requested.json()['id']}").json()
    assert run["status"] == status and status in ("passed", "failed", "error")
    assert run["parts"][0]["agent"] == "fraud" and run["parts"][0]["mode"] == "deterministic"
    assert run["parts"][0]["passed"] is True  # rules and code still pass with v2 selected
    if status != "passed":  # free quota exhausted or the model failed: the gate must hold
        assert admin.post(f"/prompts/{v2['id']}/activate").status_code == 422
        assert run["error"]

    # Runs that do not count: too old, for different prompt text, or another organisation's.
    await _passed(migrator_engine, org, "fraud", "v2", candidate, age=timedelta(days=40))
    await _passed(migrator_engine, org, "fraud", "v2", candidate, sha="0" * 64)
    other = new_client()
    register(other)
    await _passed(migrator_engine, _org(other), "fraud", "v2", candidate)
    if status != "passed":
        assert admin.post(f"/prompts/{v2['id']}/activate").status_code == 422

    await _passed(migrator_engine, org, "fraud", "v2", candidate)
    activated = admin.post(f"/prompts/{v2['id']}/activate")
    assert activated.status_code == 200 and activated.json()["prompt_version"] == "v2"
    assert {(p["agent_key"], p["version"]): p for p in admin.get("/prompts").json()}[("fraud", "v2")][
        "active_for_org"
    ]

    fraud = {a["agent_key"]: a for a in admin.get("/agents").json()}["fraud"]
    ok = admin.patch(
        "/agents/fraud",
        json={"primary_model_id": str(candidate)},
        headers={"If-Match": f'"{fraud["row_version"]}"'},
    )
    assert ok.status_code == 200, ok.text  # the candidate model has a passed run for this agent
    policy = {a["agent_key"]: a for a in admin.get("/agents").json()}["policy"]
    other_agent = admin.patch(
        "/agents/policy",
        json={"primary_model_id": str(candidate)},
        headers={"If-Match": f'"{policy["row_version"]}"'},
    )
    assert other_agent.status_code == 422  # a pass for fraud says nothing about the policy agent
    listed = [r["id"] for r in admin.get("/evals", params={"agent_key": "fraud"}).json()]
    assert requested.json()["id"] in listed
    assert other.get(f"/evals/{requested.json()['id']}").status_code == 404
