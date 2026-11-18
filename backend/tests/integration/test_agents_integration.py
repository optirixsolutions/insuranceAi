"""Agent runs on real PostgreSQL: persistence, immutability, tenant isolation, permissions, prompts.

No verified model is assigned in these organisations, so model-backed agents run their
deterministic cores and say so (``llm_note``). Live models are exercised by the system tests.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from agents.base import Prompt, load_prompt
from agents.registry import ORDER
from app.core.config import Settings
from app.core.db import Database
from app.core.tenancy import bind_tenant
from app.services import agent_service
from app.services.audit_service import AuditActor
from model_gateway.throttle import Throttle
from tests.integration.claims_helpers import claim_payload, create_claim, setup_book
from tests.integration.conftest import ApiClient, accept, invite, register

pytestmark = pytest.mark.integration
AUDIT_PATH = "/audit-logs"


def _claim(client: ApiClient) -> tuple[uuid.UUID, uuid.UUID]:
    me = client.get("/auth/me").json()
    _, customer, policy = setup_book(client)
    created = create_claim(client, claim_payload(customer, policy))
    assert created.status_code == 201, created.text
    return uuid.UUID(me["organization"]["id"]), uuid.UUID(created.json()["id"])


async def _execute(settings: Settings, org: uuid.UUID, run_id: uuid.UUID) -> Any:
    db = Database.from_settings(settings)
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    try:
        async with db.sessionmaker() as session:
            await bind_tenant(session, org)
            run = await agent_service.execute_run(session, settings, Throttle(redis), org, run_id)
            await session.commit()
            return run
    finally:
        await redis.aclose()
        await db.dispose()


async def test_run_lifecycle_through_the_api(new_client: Callable[[], ApiClient], settings: Settings) -> None:
    admin = new_client()
    register(admin)
    org, claim_id = _claim(admin)
    started = admin.post(f"/claims/{claim_id}/agent-runs", json={"agent_key": "intake"})
    assert started.status_code == 202, started.text
    assert started.json()["status"] == "running" and started.json()["attempt"] == 1
    again = admin.post(f"/claims/{claim_id}/agent-runs", json={"agent_key": "intake"})
    assert again.status_code == 409  # one running run per claim and agent
    assert admin.post(f"/claims/{claim_id}/agent-runs", json={"agent_key": "supervisor"}).status_code == 422

    run = await _execute(settings, org, uuid.UUID(started.json()["id"]))
    assert run.status == "succeeded" and run.input_hash != "pending" and run.finished_at is not None
    detail = admin.get(f"/agent-runs/{run.id}").json()
    assert detail["output_schema"] == "IntakeOutput" and len(detail["output_hash"]) == 64
    assert detail["output"]["missing_documents"]  # no documents uploaded yet
    assert {f["code"] for f in detail["findings"]} >= {"MISSING_DOCUMENT"}
    listed = admin.get(f"/claims/{claim_id}/agent-runs").json()
    assert [r["id"] for r in listed] == [str(run.id)]
    catalog = admin.get("/agent-catalog").json()
    assert [a["key"] for a in catalog] == list(ORDER)
    events = admin.get(AUDIT_PATH, params={"entity_id": str(claim_id)})
    assert events.status_code == 200, events.text
    actions = {e["action"] for e in events.json()["items"]}
    assert {"agent.run.started", "agent.run.succeeded"} <= actions


async def test_finished_runs_and_outputs_are_immutable(
    new_client: Callable[[], ApiClient], settings: Settings, app_engine: AsyncEngine
) -> None:
    admin = new_client()
    register(admin)
    org, claim_id = _claim(admin)
    run_id = uuid.UUID(
        admin.post(f"/claims/{claim_id}/agent-runs", json={"agent_key": "validation"}).json()["id"]
    )
    await _execute(settings, org, run_id)
    async with app_engine.connect() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, false)"), {"o": str(org)})
        for statement in (
            "UPDATE agent_runs SET status = 'failed' WHERE id = :id",
            "DELETE FROM agent_runs WHERE id = :id",
            "UPDATE agent_outputs SET output = '{}'::jsonb WHERE agent_run_id = :id",
            "DELETE FROM agent_outputs WHERE agent_run_id = :id",
        ):
            with pytest.raises(DBAPIError):
                async with conn.begin_nested():
                    await conn.execute(text(statement), {"id": run_id})
        await conn.rollback()


async def test_tenant_isolation_and_permissions(
    new_client: Callable[[], ApiClient], settings: Settings
) -> None:
    admin = new_client()
    register(admin)
    org, claim_id = _claim(admin)
    run_id = admin.post(f"/claims/{claim_id}/agent-runs", json={"agent_key": "intake"}).json()["id"]
    await _execute(settings, org, uuid.UUID(run_id))

    other = new_client()
    register(other)
    assert other.get(f"/agent-runs/{run_id}").status_code == 404
    assert other.get(f"/claims/{claim_id}/agent-runs").status_code == 404
    assert other.post(f"/claims/{claim_id}/agent-runs", json={"agent_key": "intake"}).status_code == 404

    auditor = new_client()
    accept(auditor, invite(admin, ["auditor"]))
    assert auditor.post(f"/claims/{claim_id}/agent-runs", json={"agent_key": "intake"}).status_code == 403


async def test_prompt_drift_refuses_to_run(
    new_client: Callable[[], ApiClient], settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = new_client()
    register(admin)
    org, claim_id = _claim(admin)
    run_id = uuid.UUID(
        admin.post(f"/claims/{claim_id}/agent-runs", json={"agent_key": "policy"}).json()["id"]
    )
    await _execute(settings, org, run_id)  # registers prompt v1 rows
    real = load_prompt

    def edited(key: str, version: str = "v1") -> Prompt:
        p = real(key, version)
        return Prompt(
            p.agent_key, p.version, p.content + "\nEdited without a version bump.", "0" * 64, p.path
        )

    monkeypatch.setattr("app.services.agent_service.load_prompt", edited)
    second = uuid.UUID(
        admin.post(f"/claims/{claim_id}/agent-runs", json={"agent_key": "policy"}).json()["id"]
    )
    run = await _execute(settings, org, second)
    assert run.status == "failed" and run.error_code == "PromptDriftError"


async def test_all_agents_persist_in_order(new_client: Callable[[], ApiClient], settings: Settings) -> None:
    admin = new_client()
    register(admin)
    org, claim_id = _claim(admin)
    db = Database.from_settings(settings)
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    try:
        async with db.sessionmaker() as session:
            await bind_tenant(session, org)
            for key in ORDER:
                run = await agent_service.create_run(
                    session, org, claim_id, key, AuditActor("system", None), None, None
                )
                await session.commit()
                run = await agent_service.execute_run(session, settings, Throttle(redis), org, run.id)
                await session.commit()
                assert run.status in ("succeeded", "partial"), (key, run.error_code, run.error_detail)
                if key in ("document", "policy", "fraud", "assessment", "decision"):
                    assert run.llm_note and not run.used_llm  # no verified model: deterministic core, stated
    finally:
        await redis.aclose()
        await db.dispose()
    runs = admin.get(f"/claims/{claim_id}/agent-runs").json()
    assert sorted(r["agent_key"] for r in runs) == sorted(ORDER)
    decision = next(r for r in runs if r["agent_key"] == "decision")
    out = admin.get(f"/agent-runs/{decision['id']}").json()["output"]
    assert out["recommendation"] == "MORE_INFORMATION_REQUIRED"  # no documents yet
    assert out["inputs_from"]["intake"] is not None
