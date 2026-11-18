"""Workflow orchestration on real PostgreSQL + Redis: LangGraph graph, Postgres checkpointer,
interrupts/resume, cancellation, crash recovery and the event outbox / SSE replay.

The worker entry point (``workflow_service.run``) is called directly instead of through Celery
(the test broker has no consumer); everything it touches is real. The full path with uploaded
documents and a killed worker runs against the live stack in the system tests.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.services import workflow_service
from tests.integration.claims_helpers import claim_payload, create_claim, setup_book
from tests.integration.conftest import ApiClient, register

pytestmark = pytest.mark.integration


def _claim(client: ApiClient) -> tuple[uuid.UUID, str]:
    me = client.get("/auth/me").json()
    _, customer, policy = setup_book(client)
    created = create_claim(client, claim_payload(customer, policy))
    assert created.status_code == 201, created.text
    return uuid.UUID(me["organization"]["id"]), created.json()["id"]


def _start(client: ApiClient, claim_id: str) -> dict[str, Any]:
    r = client.post(f"/claims/{claim_id}/processing", headers={"Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 202, r.text
    body: dict[str, Any] = r.json()
    return body


async def _run(
    settings: Settings, org: uuid.UUID, execution_id: str, resume: dict[str, Any] | None = None
) -> str:
    return await workflow_service.run(settings, org, uuid.UUID(execution_id), None, resume)


def _detail(client: ApiClient, execution_id: str) -> dict[str, Any]:
    body: dict[str, Any] = client.get(f"/workflow-executions/{execution_id}").json()
    return body


async def test_missing_documents_interrupt_for_information(
    new_client: Callable[[], ApiClient], settings: Settings
) -> None:
    admin = new_client()
    register(admin)
    org, claim_id = _claim(admin)
    execution = _start(admin, claim_id)
    assert execution["status"] == "queued"
    assert admin.post(f"/claims/{claim_id}/processing").status_code == 409  # one active run per claim

    assert await _run(settings, org, execution["id"]) == "interrupted"
    detail = _detail(admin, execution["id"])
    assert detail["status"] == "interrupted" and detail["interrupt"]["kind"] == "more_information"
    assert any(i.startswith("document:") for i in detail["interrupt"]["requested_items"])
    seqs = [e["seq"] for e in detail["events"]]
    assert seqs == list(range(1, len(seqs) + 1))  # gap-free outbox
    kinds = [(e["type"], e["node"]) for e in detail["events"]]
    assert kinds[0] == ("execution_started", None)
    assert ("node_completed", "INTAKE") in kinds and ("interrupt", "MORE_INFO") in kinds
    assert any(
        e["type"] == "status_changed" and e["payload"]["to"] == "AWAITING_INFORMATION"
        for e in detail["events"]
    )
    assert admin.get(f"/claims/{claim_id}").json()["status"] == "AWAITING_INFORMATION"
    runs = admin.get(f"/claims/{claim_id}/agent-runs").json()
    assert [r["agent_key"] for r in runs] == ["intake"]


async def test_resume_is_validated_single_use_and_continues_from_the_checkpoint(
    new_client: Callable[[], ApiClient], settings: Settings
) -> None:
    admin = new_client()
    register(admin)
    org, claim_id = _claim(admin)
    execution = _start(admin, claim_id)
    await _run(settings, org, execution["id"])
    pending = _detail(admin, execution["id"])["interrupt"]
    url = f"/workflow-executions/{execution['id']}/resume"
    wrong = admin.post(url, json={"interrupt_id": "0" * 32, "action": "information_supplied"})
    assert wrong.status_code == 409
    assert (
        admin.post(url, json={"interrupt_id": pending["interrupt_id"], "action": "reassess"}).status_code
        == 422
    )
    ok = admin.post(url, json={"interrupt_id": pending["interrupt_id"], "action": "information_supplied"})
    assert ok.status_code == 202 and ok.json()["status"] == "queued"
    again = admin.post(url, json={"interrupt_id": pending["interrupt_id"], "action": "information_supplied"})
    assert again.status_code == 409

    resume = {
        "action": "information_supplied",
        "note": "",
        "by": "test",
        "interrupt_id": pending["interrupt_id"],
    }
    assert await _run(settings, org, execution["id"], resume) == "interrupted"
    detail = _detail(admin, execution["id"])
    assert detail["more_info_count"] == 2 and detail["interrupt"]["interrupt_id"] != pending["interrupt_id"]
    types = [e["type"] for e in detail["events"]]
    assert "resumed" in types and types.count("interrupt") == 2
    intake_done = [e for e in detail["events"] if e["type"] == "node_completed" and e["node"] == "INTAKE"]
    assert len(intake_done) == 2  # the resume re-checks intake from the checkpoint, not from scratch


async def test_cancel_returns_the_claim_to_submitted(
    new_client: Callable[[], ApiClient], settings: Settings
) -> None:
    admin = new_client()
    register(admin)
    org, claim_id = _claim(admin)
    execution = _start(admin, claim_id)
    await _run(settings, org, execution["id"])
    cancelled = admin.post(f"/claims/{claim_id}/processing/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert admin.get(f"/claims/{claim_id}").json()["status"] == "SUBMITTED"
    assert _detail(admin, execution["id"])["events"][-1]["type"] == "execution_cancelled"
    assert await _run(settings, org, execution["id"]) == "skipped"  # a late delivery does nothing
    assert _start(admin, claim_id)["status"] == "queued"  # a new run may start


async def test_events_replay_over_sse_from_last_event_id(
    new_client: Callable[[], ApiClient], settings: Settings
) -> None:
    admin = new_client()
    register(admin)
    org, claim_id = _claim(admin)
    execution = _start(admin, claim_id)
    await _run(settings, org, execution["id"])
    admin.post(f"/claims/{claim_id}/processing/cancel")  # terminal: the stream ends after the replay
    total = len(_detail(admin, execution["id"])["events"])
    path = f"/api/v1/workflow-executions/{execution['id']}/events"
    with admin.http.stream("GET", path, headers={"Last-Event-ID": "2"}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    ids = [int(line[4:]) for line in body.splitlines() if line.startswith("id: ")]
    assert ids == list(range(3, total + 1)) and "event: end" in body

    other = new_client()
    register(other)
    assert other.get(f"/workflow-executions/{execution['id']}/events").status_code == 404


async def test_stale_running_execution_is_recovered(
    new_client: Callable[[], ApiClient], settings: Settings, migrator_engine: AsyncEngine
) -> None:
    admin = new_client()
    register(admin)
    org, claim_id = _claim(admin)
    execution = _start(admin, claim_id)
    async with migrator_engine.begin() as conn:  # a worker that died mid-run: running, no heartbeat for 5 min
        await conn.execute(
            text(
                "UPDATE workflow_executions SET status = 'running', "
                "started_at = now() - interval '6 minutes', "
                "heartbeat_at = now() - interval '5 minutes' WHERE id = :id"
            ),
            {"id": execution["id"]},
        )
    recovered = await workflow_service.recover_stale(settings, 60)
    assert execution["id"] in recovered
    detail = _detail(admin, execution["id"])
    assert detail["recovery_count"] == 1 and detail["events"][-1]["type"] == "retry"
    assert await _run(settings, org, execution["id"]) == "interrupted"
    assert "recovered" in [e["type"] for e in _detail(admin, execution["id"])["events"]]


async def test_outbox_is_append_only(
    new_client: Callable[[], ApiClient], settings: Settings, app_engine: AsyncEngine
) -> None:
    admin = new_client()
    register(admin)
    org, claim_id = _claim(admin)
    execution = _start(admin, claim_id)
    await _run(settings, org, execution["id"])
    async with app_engine.connect() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, false)"), {"o": str(org)})
        for statement in (
            "UPDATE workflow_events SET payload = '{}'::jsonb WHERE execution_id = :id",
            "DELETE FROM workflow_events WHERE execution_id = :id",
            "DELETE FROM workflow_executions WHERE id = :id",
        ):
            with pytest.raises(DBAPIError):
                async with conn.begin_nested():
                    await conn.execute(text(statement), {"id": execution["id"]})
        await conn.rollback()
