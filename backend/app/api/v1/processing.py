"""Claim processing (Phase 7): start, cancel, resume, retry, read executions, live events (SSE)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import select

from app.api.deps import ActorDep, DbDep, get_redis, require
from app.core.db import Database
from app.core.tenancy import bind_tenant
from app.models.claims import Claim
from app.models.workflow import WorkflowExecution
from app.security.permissions import P
from app.security.principal import Principal
from app.services import workflow_service
from app.services.claim_access import scope_claims
from workflows.runtime import channel, event_dict

router = APIRouter(tags=["processing"])
Reader = Annotated[Principal, Depends(require(P.CLAIMS_READ))]
Starter = Annotated[Principal, Depends(require(P.PROCESSING_START))]
Canceller = Annotated[Principal, Depends(require(P.PROCESSING_CANCEL))]
RedisDep = Annotated[Redis, Depends(get_redis)]
TERMINAL = ("completed", "failed", "cancelled")
KEEPALIVE_SECONDS = 15.0
ResumeAction = Literal[
    "fields_verified", "continue", "information_supplied", "escalate", "reassess", "request_information"
]


class ExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    claim_id: uuid.UUID
    graph_name: str
    graph_version: str
    status: str
    trigger: str
    current_node: str | None
    interrupt: dict[str, Any] | None
    progress: Decimal
    iteration_count: int
    reassessment_count: int
    more_info_count: int
    recovery_count: int
    tokens_used: int
    cost_usd: Decimal
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    cancel_requested_at: datetime | None
    error_code: str | None
    error_detail: str | None


class EventOut(BaseModel):
    seq: int
    type: str
    node: str | None
    agent: str | None
    occurred_at: str
    payload: dict[str, Any]


class ExecutionDetail(ExecutionOut):
    events: list[EventOut]


class ResumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interrupt_id: Annotated[str, Field(min_length=8, max_length=64)]
    action: ResumeAction
    note: Annotated[str, Field(max_length=500)] | None = None


def _event(e: Any) -> EventOut:
    return EventOut(
        seq=e.seq,
        type=e.event_type,
        node=e.node,
        agent=e.agent_key,
        occurred_at=e.occurred_at.isoformat(),
        payload=e.payload,
    )


@router.post(
    "/claims/{claim_id}/processing", response_model=ExecutionOut, status_code=status.HTTP_202_ACCEPTED
)
async def start_processing(
    claim_id: uuid.UUID,
    principal: Starter,
    actor: ActorDep,
    db: DbDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=120)] = None,
) -> WorkflowExecution:
    """Start the orchestrated pipeline. 409 while another run of this claim is active."""
    return await workflow_service.start(db, principal, claim_id, actor, idempotency_key)


@router.post("/claims/{claim_id}/processing/cancel", response_model=ExecutionOut)
async def cancel_processing(
    claim_id: uuid.UUID, principal: Canceller, actor: ActorDep, db: DbDep, redis: RedisDep
) -> WorkflowExecution:
    return await workflow_service.cancel(db, principal, claim_id, actor, redis)


@router.get("/claims/{claim_id}/executions", response_model=list[ExecutionOut])
async def list_executions(claim_id: uuid.UUID, principal: Reader, db: DbDep) -> list[WorkflowExecution]:
    return await workflow_service.list_executions(db, principal, claim_id)


@router.get("/workflow-executions/{execution_id}", response_model=ExecutionDetail)
async def get_execution(execution_id: uuid.UUID, principal: Reader, db: DbDep) -> ExecutionDetail:
    execution = await workflow_service.get_execution(db, principal, execution_id)
    events = await workflow_service.events_after(db, execution.id, 0, limit=2000)
    base = ExecutionOut.model_validate(execution).model_dump()
    return ExecutionDetail(**base, events=[_event(e) for e in events])


@router.post(
    "/workflow-executions/{execution_id}/resume",
    response_model=ExecutionOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resume_execution(
    execution_id: uuid.UUID, body: ResumeIn, principal: Starter, actor: ActorDep, db: DbDep
) -> WorkflowExecution:
    """Answer the step the run is waiting on (the interrupt id prevents a double resume)."""
    return await workflow_service.resume(
        db,
        principal,
        execution_id,
        interrupt_id=body.interrupt_id,
        action=body.action,
        note=body.note,
        actor=actor,
    )


@router.post(
    "/workflow-executions/{execution_id}/retry",
    response_model=ExecutionOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_execution(
    execution_id: uuid.UUID, principal: Starter, actor: ActorDep, db: DbDep
) -> WorkflowExecution:
    """Resume a failed run from its last checkpoint (completed nodes are not re-run)."""
    return await workflow_service.retry(db, principal, execution_id, actor)


@router.get("/workflow-executions/{execution_id}/events")
async def stream_events(
    execution_id: uuid.UUID,
    request: Request,
    principal: Reader,
    db: DbDep,
    redis: RedisDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Server-sent events. Replays rows after ``Last-Event-ID`` from the outbox table, then follows
    Redis; every wake-up re-reads the table, so a dropped pub/sub message never loses a step."""
    execution = await workflow_service.get_execution(db, principal, execution_id)  # authorises
    database: Database = request.app.state.db
    org = execution.organization_id
    start_seq = int(last_event_id or "0") if (last_event_id or "").isdigit() else 0

    async def gen() -> AsyncIterator[str]:
        last = start_seq
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel(execution_id))
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                async with database.sessionmaker() as s:
                    await bind_tenant(s, org)
                    rows = await workflow_service.events_after(s, execution_id, last)
                    current = await s.get(WorkflowExecution, execution_id)
                for e in rows:
                    last = e.seq
                    data = json.dumps(event_dict(e), default=str)
                    yield f"id: {e.seq}\nevent: {e.event_type}\ndata: {data}\n\n"
                if current is None or (current.status in TERMINAL and not rows):
                    yield "event: end\ndata: {}\n\n"
                    return
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=KEEPALIVE_SECONDS)
                if message is None:
                    yield ": keep-alive\n\n"
        finally:
            await pubsub.unsubscribe(channel(execution_id))
            await pubsub.aclose()  # type: ignore[no-untyped-call]

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


class ExecutionListItem(ExecutionOut):
    claim_reference: str


@router.get("/workflow-executions", response_model=list[ExecutionListItem])
async def list_org_executions(
    principal: Reader,
    db: DbDep,
    status_filter: Annotated[
        str | None, Query(alias="status", pattern="^(queued|running|interrupted|completed|failed|cancelled)$")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ExecutionListItem]:
    """Recent processing runs on the claims this user can see (newest first)."""
    stmt = select(WorkflowExecution, Claim.claim_reference).join(
        Claim, Claim.id == WorkflowExecution.claim_id
    )
    if status_filter:
        stmt = stmt.where(WorkflowExecution.status == status_filter)
    rows = (
        await db.execute(
            scope_claims(stmt, principal).order_by(WorkflowExecution.created_at.desc()).limit(limit)
        )
    ).all()
    return [
        ExecutionListItem(**ExecutionOut.model_validate(ex).model_dump(), claim_reference=ref)
        for ex, ref in rows
    ]
