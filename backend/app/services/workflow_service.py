"""Workflow executions: start, cancel, resume, retry, run (worker), crash recovery, read.

One LangGraph thread per execution (``thread_id = execution id``), checkpointed in PostgreSQL by
``AsyncPostgresSaver``. A Celery delivery runs only if its task id is the execution's current one,
so a broker redelivery after a crash can never run an execution twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import Database
from app.core.errors import BusinessRuleError, ConflictError, NotFoundError
from app.core.tenancy import bind_tenant
from app.models.claims import Claim
from app.models.workflow import WorkflowCheckpoint, WorkflowEvent, WorkflowExecution
from app.security.principal import Principal
from app.services import audit_service
from app.services import events as domain_events
from app.services.audit_service import AuditActor
from app.services.claim_access import get_visible_claim
from rules_engine.claim_status import ClaimStatus, InvalidTransitionError
from workflows.graph import build
from workflows.runtime import (
    GRAPH_NAME,
    GRAPH_VERSION,
    CancelledByUserError,
    Runtime,
    emit,
    publish,
    set_claim_status,
)

logger = structlog.get_logger(__name__)
STARTABLE = {ClaimStatus.SUBMITTED.value, ClaimStatus.AWAITING_INFORMATION.value, ClaimStatus.FAILED.value}
ACTIVE = ("queued", "running", "interrupted")
HEARTBEAT_SECONDS = 15
MAX_RECOVERIES = 3
RESUME_ACTIONS = {
    "document_review": {"fields_verified", "continue"},
    "more_information": {"information_supplied", "escalate"},
}  # human_review interrupts are resolved only through the review endpoints (review_actions)


def checkpointer_url(settings: Settings) -> str:
    return settings.database_url.get_secret_value().replace("postgresql+asyncpg://", "postgresql://", 1)


def enqueue(execution: WorkflowExecution, resume: dict[str, Any] | None = None) -> None:
    from tasks.celery_app import celery_app

    celery_app.send_task(
        "workflow.run",
        args=[str(execution.organization_id), str(execution.id), resume],
        task_id=execution.celery_task_id,
        queue="workflow",
    )


# --- API-side operations -------------------------------------------------------------------------


async def start(
    db: AsyncSession,
    principal: Principal,
    claim_id: uuid.UUID,
    actor: AuditActor,
    idempotency_key: str | None,
) -> WorkflowExecution:
    claim = await get_visible_claim(db, principal, claim_id, lock=True)
    if idempotency_key:
        existing = await db.scalar(
            select(WorkflowExecution).where(
                WorkflowExecution.organization_id == claim.organization_id,
                WorkflowExecution.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return existing
    if claim.status not in STARTABLE:
        raise BusinessRuleError(f"Processing cannot start while the claim is {claim.status}")
    execution = WorkflowExecution(
        id=uuid.uuid4(),
        organization_id=claim.organization_id,
        claim_id=claim.id,
        graph_name=GRAPH_NAME,
        graph_version=GRAPH_VERSION,
        status="queued",
        trigger="initial",
        started_by=principal.user_id,
        celery_task_id=uuid.uuid4().hex,
        idempotency_key=idempotency_key,
        created_at=datetime.now(UTC),
    )
    db.add(execution)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise ConflictError("This claim already has an active processing run") from exc
    await audit_service.record(
        db,
        actor=actor,
        action="workflow.started",
        entity_type="claim",
        entity_id=claim.id,
        after={"execution_id": str(execution.id)},
    )
    await db.commit()
    enqueue(execution)
    return execution


async def _active(db: AsyncSession, principal: Principal, claim_id: uuid.UUID) -> WorkflowExecution:
    claim = await get_visible_claim(db, principal, claim_id)
    execution = await db.scalar(
        select(WorkflowExecution)
        .where(WorkflowExecution.claim_id == claim.id, WorkflowExecution.status.in_(ACTIVE))
        .with_for_update(key_share=True)
    )
    if execution is None:
        raise NotFoundError("This claim has no active processing run")
    return execution


async def _finish_cancel(db: AsyncSession, execution: WorkflowExecution) -> list[WorkflowEvent]:
    from app.services import review_service

    events = []
    await review_service.cancel_open(db, execution.claim_id)
    with contextlib.suppress(InvalidTransitionError):
        events += await set_claim_status(db, execution, ClaimStatus.SUBMITTED, "processing cancelled")
    execution.status, execution.finished_at, execution.interrupt = "cancelled", datetime.now(UTC), None
    events.append(await emit(db, execution, "execution_cancelled"))
    return events


async def cancel(
    db: AsyncSession, principal: Principal, claim_id: uuid.UUID, actor: AuditActor, redis: Redis
) -> WorkflowExecution:
    execution = await _active(db, principal, claim_id)
    execution.cancel_requested_at = datetime.now(UTC)
    events: list[WorkflowEvent] = []
    if execution.status in ("queued", "interrupted"):  # no worker holds it: finish now
        events = await _finish_cancel(db, execution)
    await audit_service.record(
        db,
        actor=actor,
        action="workflow.cancel_requested",
        entity_type="claim",
        entity_id=execution.claim_id,
        after={"execution_id": str(execution.id)},
    )
    await db.commit()
    await publish(redis, events)
    return execution


async def resume(
    db: AsyncSession,
    principal: Principal,
    execution_id: uuid.UUID,
    *,
    interrupt_id: str,
    action: str,
    note: str | None,
    actor: AuditActor,
) -> WorkflowExecution:
    execution = await get_execution(db, principal, execution_id, lock=True)
    pending = execution.interrupt or {}
    if execution.status != "interrupted" or pending.get("interrupt_id") != interrupt_id:
        raise ConflictError("This step is not waiting for input (it may already have been resumed)")
    kind = str(pending.get("kind"))
    if kind == "human_review":
        raise BusinessRuleError("Decide human-review steps in the review workspace (/reviews)")
    if action not in RESUME_ACTIONS.get(kind, set()) or action not in pending.get("actions", []):
        raise BusinessRuleError(f"'{action}' is not available here; choose one of {pending.get('actions')}")
    payload = prepare_resume(execution, interrupt_id, action, principal.user_id, note or "")
    await audit_service.record(
        db,
        actor=actor,
        action="workflow.resumed",
        entity_type="claim",
        entity_id=execution.claim_id,
        after={"execution_id": str(execution.id), "interrupt": kind, "action": action},
    )
    await db.commit()
    enqueue(execution, payload)
    return execution


def prepare_resume(
    execution: WorkflowExecution, interrupt_id: str, action: str, by: uuid.UUID, note: str
) -> dict[str, Any]:
    """Mark an interrupted execution queued for resume; the caller commits, then calls enqueue()."""
    execution.status, execution.celery_task_id = "queued", uuid.uuid4().hex
    execution.interrupt = {**(execution.interrupt or {}), "resumed_with": action, "resumed_by": str(by)}
    return {"action": action, "note": note[:500], "by": str(by), "interrupt_id": interrupt_id}


async def retry(
    db: AsyncSession, principal: Principal, execution_id: uuid.UUID, actor: AuditActor
) -> WorkflowExecution:
    execution = await get_execution(db, principal, execution_id, lock=True)
    if execution.status != "failed":
        raise ConflictError("Only a failed run can be retried")
    execution.status, execution.celery_task_id = "queued", uuid.uuid4().hex
    execution.error_code = execution.error_detail = None
    execution.finished_at = None
    await audit_service.record(
        db,
        actor=actor,
        action="workflow.retried",
        entity_type="claim",
        entity_id=execution.claim_id,
        after={"execution_id": str(execution.id)},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        raise ConflictError("This claim already has another active processing run") from exc
    enqueue(execution)
    return execution


async def get_execution(
    db: AsyncSession, principal: Principal, execution_id: uuid.UUID, *, lock: bool = False
) -> WorkflowExecution:
    stmt = select(WorkflowExecution).where(WorkflowExecution.id == execution_id)
    execution = await db.scalar(stmt.with_for_update(key_share=True) if lock else stmt)
    if execution is None:
        raise NotFoundError("Processing run not found")
    await get_visible_claim(db, principal, execution.claim_id)  # object-level access
    return execution


async def list_executions(
    db: AsyncSession, principal: Principal, claim_id: uuid.UUID
) -> list[WorkflowExecution]:
    claim = await get_visible_claim(db, principal, claim_id)
    return list(
        (
            await db.scalars(
                select(WorkflowExecution)
                .where(WorkflowExecution.claim_id == claim.id)
                .order_by(WorkflowExecution.created_at.desc())
            )
        ).all()
    )


async def events_after(
    db: AsyncSession, execution_id: uuid.UUID, seq: int, limit: int = 500
) -> list[WorkflowEvent]:
    return list(
        (
            await db.scalars(
                select(WorkflowEvent)
                .where(WorkflowEvent.execution_id == execution_id, WorkflowEvent.seq > seq)
                .order_by(WorkflowEvent.seq)
                .limit(limit)
            )
        ).all()
    )


# --- worker side ---------------------------------------------------------------------------------


async def _heartbeat_loop(rt: Runtime) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            async with rt.session() as s:
                await rt.heartbeat(s)
                await s.commit()
        except Exception:  # a missed heartbeat only risks an early recovery, never data
            logger.warning("workflow_heartbeat_failed", execution_id=str(rt.execution_id))


async def _mirror_checkpoints(rt: Runtime, saver: AsyncPostgresSaver) -> None:
    rows = []
    async for cp in saver.alist({"configurable": {"thread_id": str(rt.execution_id)}}):
        conf = cp.config.get("configurable", {})
        writes = (cp.metadata or {}).get("writes") or {}
        rows.append(
            {
                "id": uuid.uuid4(),
                "organization_id": rt.organization_id,
                "execution_id": rt.execution_id,
                "checkpoint_id": str(conf.get("checkpoint_id")),
                "checkpoint_ns": str(conf.get("checkpoint_ns", "")),
                "node": (",".join(sorted(writes)) if isinstance(writes, dict) else "")[:60] or None,
            }
        )
    if rows:
        async with rt.session() as s:
            await s.execute(
                insert(WorkflowCheckpoint)
                .values(rows)
                .on_conflict_do_nothing(constraint="uq_workflow_checkpoints")
            )
            await s.commit()


def initial_state(ex: WorkflowExecution) -> dict[str, Any]:
    return {
        "organization_id": str(ex.organization_id),
        "claim_id": str(ex.claim_id),
        "execution_id": str(ex.id),
        "runs": {},
        "summaries": {},
        "completed": [],
        "errors": [],
        "reassessment_count": 0,
        "more_info_count": 0,
        "review_round": 0,
    }


async def run(
    settings: Settings,
    organization_id: uuid.UUID,
    execution_id: uuid.UUID,
    task_id: str | None,
    resume_payload: dict[str, Any] | None,
) -> str:
    db = Database.from_settings(settings, application_name="claimos-workflow")
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    try:
        return await _run(db, redis, settings, organization_id, execution_id, task_id, resume_payload)
    finally:
        await redis.aclose()
        await db.dispose()


async def _begin(
    db: Database,
    redis: Redis,
    settings: Settings,
    org: uuid.UUID,
    execution_id: uuid.UUID,
    task_id: str | None,
    resume_payload: dict[str, Any] | None,
) -> tuple[Runtime, dict[str, Any]] | None:
    async with db.sessionmaker() as s:
        await bind_tenant(s, org)
        ex = await s.scalar(
            select(WorkflowExecution)
            .where(WorkflowExecution.id == execution_id)
            .with_for_update(key_share=True)
        )
        if ex is None or ex.status not in ("queued", "running") or (task_id and ex.celery_task_id != task_id):
            return None  # stale delivery, or finished/cancelled meanwhile
        recovering = ex.status == "running" or (
            ex.recovery_count > 0 and ex.started_at is not None and not resume_payload
        )
        ex.status, ex.heartbeat_at = "running", datetime.now(UTC)
        ex.started_at = ex.started_at or ex.heartbeat_at
        if resume_payload:
            kind, payload = "resumed", {"action": resume_payload.get("action")}
        elif recovering:
            kind, payload = "recovered", {"recovery": ex.recovery_count}
        else:
            kind, payload = "execution_started", {"trigger": ex.trigger}
        events = [await emit(s, ex, kind, payload=payload)]
        rt = Runtime(db, redis, settings, org, ex.id, ex.claim_id, ex.started_by, None)
        state0 = initial_state(ex)
        await s.commit()
    await publish(redis, events)
    return rt, state0


async def _run(
    db: Database,
    redis: Redis,
    settings: Settings,
    org: uuid.UUID,
    execution_id: uuid.UUID,
    task_id: str | None,
    resume_payload: dict[str, Any] | None,
) -> str:
    begun = await _begin(db, redis, settings, org, execution_id, task_id, resume_payload)
    if begun is None:
        return "skipped"
    rt, state0 = begun
    beat = asyncio.create_task(_heartbeat_loop(rt))
    outcome, error = "completed", None
    interrupt_value: dict[str, Any] | None = None
    try:
        async with AsyncPostgresSaver.from_conn_string(checkpointer_url(settings)) as saver:
            graph = build().compile(checkpointer=saver)
            thread = {"configurable": {"thread_id": str(execution_id)}}
            config: Any = {
                "configurable": {"thread_id": str(execution_id), "runtime": rt},
                "recursion_limit": 100,
            }
            has_checkpoint = await saver.aget_tuple(thread) is not None  # type: ignore[arg-type]
            graph_input: Any = (
                Command(resume=resume_payload) if resume_payload else (None if has_checkpoint else state0)
            )
            await graph.ainvoke(graph_input, config)
            snapshot = await graph.aget_state(config)
            pending = list(
                getattr(snapshot, "interrupts", ()) or [i for t in snapshot.tasks for i in t.interrupts]
            )
            if pending:
                outcome, interrupt_value = "interrupted", dict(pending[0].value)
            await _mirror_checkpoints(rt, saver)
    except CancelledByUserError:
        outcome = "cancelled"
    except Exception as exc:
        logger.exception("workflow_failed", execution_id=str(execution_id))
        outcome, error = "failed", exc
    finally:
        beat.cancel()
    await _finish(db, redis, org, execution_id, outcome, error, interrupt_value)
    return outcome


async def _finish(
    db: Database,
    redis: Redis,
    org: uuid.UUID,
    execution_id: uuid.UUID,
    outcome: str,
    error: BaseException | None,
    interrupt_value: dict[str, Any] | None,
) -> None:
    async with db.sessionmaker() as s:
        await bind_tenant(s, org)
        ex = await s.scalar(
            select(WorkflowExecution)
            .where(WorkflowExecution.id == execution_id)
            .with_for_update(key_share=True)
        )
        if ex is None:
            return
        events: list[WorkflowEvent] = []
        now = datetime.now(UTC)
        if outcome == "interrupted" and ex.cancel_requested_at is not None:
            outcome = "cancelled"
        if outcome == "cancelled":
            events += await _finish_cancel(s, ex)
        elif outcome == "interrupted":
            ex.status, ex.interrupt = "interrupted", interrupt_value
            events.append(
                await emit(s, ex, "interrupt", node=ex.current_node, payload={"interrupt": interrupt_value})
            )
        elif outcome == "failed":
            ex.status, ex.finished_at = "failed", now
            ex.error_code, ex.error_detail = type(error).__name__, str(error)[:1000]
            with contextlib.suppress(InvalidTransitionError):
                events += await set_claim_status(
                    s, ex, ClaimStatus.FAILED, f"processing failed: {ex.error_code}"
                )
            claim = await s.get(Claim, ex.claim_id)
            if claim is not None:
                await domain_events.emit(
                    s,
                    claim.organization_id,
                    "claim.processing_failed",
                    domain_events.claim_data(claim, execution_id=str(ex.id), error_code=ex.error_code),
                    claim_id=claim.id,
                    notices=[
                        domain_events.Notice(
                            [ex.started_by] if ex.started_by else [],
                            "processing.failed",
                            f"Processing failed for {claim.claim_reference}",
                            f"Step {ex.current_node or 'unknown'} failed ({ex.error_code}). "
                            "You can retry the run.",
                            link=f"/claims/{claim.id}?tab=analysis",
                        )
                    ],
                )
            events.append(
                await emit(
                    s,
                    ex,
                    "execution_failed",
                    node=ex.current_node,
                    payload={"error": ex.error_code, "detail": (ex.error_detail or "")[:300]},
                )
            )
        else:
            ex.status, ex.finished_at = "completed", now
            events.append(await emit(s, ex, "execution_completed"))
        if ex.started_at:
            ex.duration_ms = int((now - ex.started_at).total_seconds() * 1000)
        await s.commit()
    await publish(redis, events)


async def recover_stale(settings: Settings, stale_seconds: int) -> list[str]:
    """Re-enqueue executions whose worker stopped heartbeating (LangGraph resumes from the checkpoint)."""
    db = Database.from_settings(settings, application_name="claimos-workflow-recovery")
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    recovered: list[str] = []
    try:
        async with db.sessionmaker() as s:
            rows = (
                await s.execute(
                    text("SELECT execution_id, organization_id FROM claimos_stale_executions(:s)"),
                    {"s": stale_seconds},
                )
            ).all()
        for execution_id, org in rows:
            async with db.sessionmaker() as s:
                await bind_tenant(s, org)
                ex = await s.scalar(
                    select(WorkflowExecution)
                    .where(WorkflowExecution.id == execution_id)
                    .with_for_update(key_share=True)
                )
                if ex is None or ex.status not in ("running", "queued"):
                    continue
                if (ex.heartbeat_at or ex.created_at) >= datetime.now(UTC) - timedelta(seconds=stale_seconds):
                    continue
                ex.recovery_count += 1
                if ex.recovery_count > MAX_RECOVERIES:
                    ex.status, ex.error_code, ex.finished_at = "failed", "RECOVERY_LIMIT", datetime.now(UTC)
                    ex.error_detail = f"worker lost {MAX_RECOVERIES} times; retry manually"
                    events = [await emit(s, ex, "execution_failed", payload={"error": "RECOVERY_LIMIT"})]
                else:
                    ex.celery_task_id, ex.heartbeat_at = uuid.uuid4().hex, datetime.now(UTC)
                    events = [
                        await emit(
                            s,
                            ex,
                            "retry",
                            node=ex.current_node,
                            payload={"reason": "worker lost", "recovery": ex.recovery_count},
                        )
                    ]
                await s.commit()
                await publish(redis, events)
                if ex.status != "failed":
                    enqueue(ex)
                    recovered.append(str(ex.id))
    finally:
        await redis.aclose()
        await db.dispose()
    return recovered
