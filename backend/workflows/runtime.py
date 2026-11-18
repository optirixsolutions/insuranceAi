"""Execution runtime shared by the graph nodes: sessions, events, claim status, cancellation.

Every node opens its own tenant-bound session (parallel branches never share one). Events are
written to ``workflow_events`` in the same transaction as the node's writes (outbox) and published
to Redis after commit; ``seq`` is allocated under a per-execution advisory lock, so it is gap-free.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import Database
from app.core.tenancy import bind_tenant
from app.models.claims import Claim, ClaimStatusChange
from app.models.workflow import WorkflowEvent, WorkflowExecution
from app.services import audit_service
from app.services import events as domain_events
from app.services.audit_service import AuditActor
from model_gateway.throttle import Throttle
from rules_engine.claim_status import Actor, ClaimStatus, path

GRAPH_NAME = "claim_processing"
GRAPH_VERSION = "1"


class CancelledByUserError(RuntimeError):
    """The execution was cancelled; raised at the next node boundary."""


def channel(execution_id: uuid.UUID | str) -> str:
    return f"claimos:workflow:{execution_id}"


@dataclass
class Runtime:
    db: Database
    redis: Redis
    settings: Settings
    organization_id: uuid.UUID
    execution_id: uuid.UUID
    claim_id: uuid.UUID
    user_id: uuid.UUID | None
    request_id: str | None

    @property
    def throttle(self) -> Throttle:
        return Throttle(self.redis)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.db.sessionmaker() as s:
            await bind_tenant(s, self.organization_id)
            yield s

    async def execution(self, s: AsyncSession, *, lock: bool = False) -> WorkflowExecution:
        stmt = select(WorkflowExecution).where(WorkflowExecution.id == self.execution_id)
        if lock:
            # FOR NO KEY UPDATE, not FOR UPDATE: parallel branches insert rows that reference this
            # execution (agent runs, events), and those foreign-key checks take KEY SHARE locks. FOR UPDATE
            # blocks them, which together with the per-execution advisory lock deadlocked the policy and
            # fraud branches (found by the Phase 14 benchmark). The key never changes, so this is safe.
            stmt = stmt.with_for_update(key_share=True)
        row = await s.scalar(stmt)
        if row is None:
            raise LookupError(f"workflow execution {self.execution_id} not found")
        return row

    async def check_cancelled(self, s: AsyncSession) -> None:
        if (await self.execution(s)).cancel_requested_at is not None:
            raise CancelledByUserError("cancelled by a user")

    async def heartbeat(self, s: AsyncSession, node: str | None = None) -> None:
        execution = await self.execution(s)
        execution.heartbeat_at = datetime.now(UTC)
        if node is not None:
            execution.current_node = node


async def emit(
    s: AsyncSession,
    execution: WorkflowExecution,
    event_type: str,
    *,
    node: str | None = None,
    agent_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> WorkflowEvent:
    """Append an event in the caller's transaction (publish with ``publish`` after commit)."""
    await s.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"wf:{execution.id}"}
    )
    seq = (
        await s.scalar(select(func.max(WorkflowEvent.seq)).where(WorkflowEvent.execution_id == execution.id))
    ) or 0
    event = WorkflowEvent(
        id=uuid.uuid4(),
        organization_id=execution.organization_id,
        execution_id=execution.id,
        claim_id=execution.claim_id,
        seq=seq + 1,
        event_type=event_type,
        node=node,
        agent_key=agent_key,
        payload=payload or {},
        occurred_at=datetime.now(UTC),
    )
    s.add(event)
    await s.flush()
    return event


def event_dict(e: WorkflowEvent) -> dict[str, Any]:
    return {
        "seq": e.seq,
        "type": e.event_type,
        "node": e.node,
        "agent": e.agent_key,
        "occurred_at": e.occurred_at.isoformat(),
        **e.payload,
    }


async def publish(redis: Redis, events: list[WorkflowEvent]) -> None:
    """Best effort: the SSE endpoint re-reads the table on any gap, so a lost message is harmless."""
    for e in events:
        with contextlib.suppress(Exception):  # delivery is recovered from the outbox
            await redis.publish(channel(e.execution_id), json.dumps(event_dict(e), default=str))


async def set_claim_status(
    s: AsyncSession,
    execution: WorkflowExecution,
    target: ClaimStatus,
    reason: str,
    actor: Actor = "workflow",
    *,
    changed_by: uuid.UUID | None = None,
) -> list[WorkflowEvent]:
    """Walk the claim through every allowed intermediate status to ``target``, recording each hop."""
    claim = await s.scalar(
        select(Claim).where(Claim.id == execution.claim_id).with_for_update(key_share=True)
    )
    if claim is None:
        raise LookupError("claim not found")
    current = ClaimStatus(claim.status)
    events = []
    for hop in path(current, target, actor):
        s.add(
            ClaimStatusChange(
                organization_id=claim.organization_id,
                claim_id=claim.id,
                from_status=claim.status,
                to_status=hop.value,
                reason=reason,
                actor_type="user" if changed_by else "system",
                changed_by=changed_by,
            )
        )
        await audit_service.record(
            s,
            actor=AuditActor("user", changed_by) if changed_by else AuditActor("system", None),
            action="claim.status_changed",
            entity_type="claim",
            entity_id=claim.id,
            before={"status": claim.status},
            after={"status": hop.value, "reason": reason, "execution_id": str(execution.id)},
        )
        events.append(
            await emit(
                s,
                execution,
                "status_changed",
                payload={"from": claim.status, "to": hop.value, "reason": reason},
            )
        )
        previous, claim.status = claim.status, hop.value
        claim.row_version += 1
        await domain_events.emit(
            s,
            claim.organization_id,
            "claim.status_changed",
            domain_events.claim_data(claim, previous_status=previous, reason=reason[:200]),
            claim_id=claim.id,
        )
    return events
