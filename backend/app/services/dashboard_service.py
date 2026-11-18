"""Dashboard KPIs and live activity (05 §2.11 ``/dashboard/*``). Every figure is one SQL query over
the caller's visible claims (``metrics_common.Scope``); definitions travel with the values."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agents import AgentOutput, AgentRun
from app.models.claims import Claim
from app.models.rules import ClaimDecision, HumanReview
from app.models.workflow import WorkflowEvent, WorkflowExecution
from app.services.metrics_common import Metric, Scope, Unit, ms, percentile, ratio

OPEN_REVIEW = ("pending", "in_progress", "awaiting_second_approval", "escalated")
CLOSED_CLAIM = ("APPROVED", "REJECTED", "COMPLETED")
FLAGGED = ("high", "critical")
# key -> (label, unit, definition; "{p}" is the period). The integration tests cover every key.
SUMMARY: dict[str, tuple[str, Unit, str]] = {
    "claims_received": ("Claims received", "count", "Claims reported {p}."),
    "open_claims": ("Open claims", "count", "Claims not yet approved, rejected or completed (now)."),
    "in_review": (
        "In human review",
        "count",
        "Open reviews: pending, in progress, escalated or awaiting a second approver (now).",
    ),
    "overdue_reviews": ("Overdue reviews", "count", "Open reviews past their due time (now)."),
    "awaiting_second_approval": (
        "Awaiting second approver",
        "count",
        "Reviews with a first decision waiting for four-eyes confirmation (now).",
    ),
    "approved": ("Approved", "count", "Approval decisions recorded {p} (people and straight-through)."),
    "rejected": ("Rejected", "count", "Rejection decisions recorded {p}."),
    "stp_rate": ("Straight-through rate", "ratio", "Straight-through approvals / all approvals {p}."),
    "override_rate": (
        "Override rate",
        "ratio",
        "Human approvals and rejections that overrode the recommendation / all of them {p}.",
    ),
    "median_wall_ms": (
        "Median end-to-end time",
        "ms",
        "Median duration of processing runs completed {p}, including time waiting for people.",
    ),
    "median_machine_ms": (
        "Median processing time",
        "ms",
        "Median of the summed step durations of runs completed {p} (system time only).",
    ),
    "failed_runs": ("Failed runs", "count", "Processing runs that ended in failure {p}."),
    "fraud_alerts": (
        "Fraud alerts",
        "count",
        "Claims whose fraud analysis {p} scored high or critical risk.",
    ),
    "model_cost_usd": ("Model cost", "usd", "Model spend recorded on agent runs {p} (USD)."),
}
SUMMARY_KEYS = tuple(SUMMARY)


def machine_time() -> Any:
    """Per execution: the sum of node durations (time the system worked, excluding waits for people)."""
    duration = WorkflowEvent.payload["duration_ms"].as_integer()
    return (
        select(WorkflowEvent.execution_id, func.sum(duration).label("machine_ms"))
        .where(WorkflowEvent.event_type == "node_completed")
        .group_by(WorkflowEvent.execution_id)
        .subquery()
    )


async def _count(db: AsyncSession, stmt: Any) -> int:
    return int(await db.scalar(stmt) or 0)


async def summary(db: AsyncSession, scope: Scope) -> dict[str, Any]:
    since, now = scope.since, datetime.now(UTC)
    seen = scope.visible
    received = await _count(
        db, select(func.count(Claim.id)).where(seen(Claim.id), Claim.reported_at >= since)
    )
    open_claims = await _count(
        db, select(func.count(Claim.id)).where(seen(Claim.id), Claim.status.not_in(CLOSED_CLAIM))
    )
    open_reviews = select(func.count(HumanReview.id)).where(
        seen(HumanReview.claim_id), HumanReview.status.in_(OPEN_REVIEW)
    )
    in_review = await _count(db, open_reviews)
    overdue = await _count(db, open_reviews.where(HumanReview.due_at < now))
    second = await _count(
        db,
        select(func.count(HumanReview.id)).where(
            seen(HumanReview.claim_id), HumanReview.status == "awaiting_second_approval"
        ),
    )
    decided = select(ClaimDecision.decision, ClaimDecision.decision_mode, ClaimDecision.override).where(
        seen(ClaimDecision.claim_id), ClaimDecision.created_at >= since
    )
    rows = (await db.execute(decided)).all()
    approved = sum(1 for d, _, _ in rows if d == "approved")
    rejected = sum(1 for d, _, _ in rows if d == "rejected")
    stp = sum(1 for d, m, _ in rows if d == "approved" and m == "stp")
    human_final = [o for d, m, o in rows if m == "human" and d in ("approved", "rejected")]
    machine = machine_time()
    finished = and_(
        seen(WorkflowExecution.claim_id),
        WorkflowExecution.status == "completed",
        WorkflowExecution.finished_at >= since,
    )
    wall = await db.scalar(select(percentile(0.5, WorkflowExecution.duration_ms)).where(finished))
    machine_median = await db.scalar(
        select(percentile(0.5, machine.c.machine_ms))
        .select_from(WorkflowExecution)
        .join(machine, machine.c.execution_id == WorkflowExecution.id)
        .where(finished)
    )
    failed = await _count(
        db,
        select(func.count(WorkflowExecution.id)).where(
            seen(WorkflowExecution.claim_id),
            WorkflowExecution.status == "failed",
            WorkflowExecution.finished_at >= since,
        ),
    )
    alerts = await _count(
        db,
        select(func.count(distinct(AgentRun.claim_id)))
        .join(AgentOutput, AgentOutput.agent_run_id == AgentRun.id)
        .where(
            seen(AgentRun.claim_id),
            AgentRun.agent_key == "fraud",
            AgentRun.started_at >= since,
            AgentOutput.output["risk_level"].as_string().in_(FLAGGED),
        ),
    )
    cost = await db.scalar(
        select(func.coalesce(func.sum(AgentRun.cost_usd), 0)).where(
            seen(AgentRun.claim_id), AgentRun.started_at >= since
        )
    )
    values: dict[str, Any] = {
        "claims_received": received,
        "open_claims": open_claims,
        "in_review": in_review,
        "overdue_reviews": overdue,
        "awaiting_second_approval": second,
        "approved": approved,
        "rejected": rejected,
        "stp_rate": ratio(stp, approved),
        "override_rate": ratio(sum(human_final), len(human_final)),
        "median_wall_ms": ms(wall),
        "median_machine_ms": ms(machine_median),
        "failed_runs": failed,
        "fraud_alerts": alerts,
        "model_cost_usd": cost,
    }
    period = f"in the last {scope.days} days"
    metrics = [
        Metric(key, label, values[key], unit, definition.format(p=period))
        for key, (label, unit, definition) in SUMMARY.items()
    ]
    return {
        "days": scope.days,
        "include_synthetic": scope.include_synthetic,
        "scope": "organisation" if scope.org_wide else "assigned claims",
        "generated_at": now.isoformat(),
        "metrics": [m.as_dict() for m in metrics],
    }


async def activity(db: AsyncSession, scope: Scope) -> dict[str, Any]:
    """Live work and exceptions: running and waiting runs, recent failures, pending approvals, overdue."""
    seen, now = scope.visible, datetime.now(UTC)
    ref = Claim.claim_reference
    running = (
        await db.execute(
            select(WorkflowExecution, ref)
            .join(Claim, Claim.id == WorkflowExecution.claim_id)
            .where(seen(Claim.id), WorkflowExecution.status.in_(("queued", "running")))
            .order_by(WorkflowExecution.created_at.desc())
            .limit(10)
        )
    ).all()
    kind = WorkflowExecution.interrupt["kind"].as_string()
    waiting = (
        await db.execute(
            select(kind, func.count())
            .where(seen(WorkflowExecution.claim_id), WorkflowExecution.status == "interrupted")
            .group_by(kind)
        )
    ).all()
    failures = (
        await db.execute(
            select(WorkflowExecution, ref)
            .join(Claim, Claim.id == WorkflowExecution.claim_id)
            .where(seen(Claim.id), WorkflowExecution.status == "failed")
            .order_by(WorkflowExecution.finished_at.desc().nulls_last())
            .limit(10)
        )
    ).all()

    async def reviews(*conditions: Any) -> list[dict[str, Any]]:
        rows = (
            await db.execute(
                select(HumanReview, ref)
                .join(Claim, Claim.id == HumanReview.claim_id)
                .where(seen(Claim.id), *conditions)
                .order_by(HumanReview.due_at)
                .limit(10)
            )
        ).all()
        return [
            {
                "review_id": str(r.id),
                "claim_id": str(r.claim_id),
                "claim_reference": c,
                "status": r.status,
                "due_at": r.due_at.isoformat() if r.due_at else None,
                "priority_score": r.priority_score,
            }
            for r, c in rows
        ]

    return {
        "generated_at": now.isoformat(),
        "running": [
            {
                "execution_id": str(e.id),
                "claim_id": str(e.claim_id),
                "claim_reference": c,
                "status": e.status,
                "current_node": e.current_node,
                "progress": float(e.progress),
            }
            for e, c in running
        ],
        "waiting": {str(k or "unknown"): int(n) for k, n in waiting},
        "recent_failures": [
            {
                "execution_id": str(e.id),
                "claim_id": str(e.claim_id),
                "claim_reference": c,
                "error_code": e.error_code,
                "finished_at": e.finished_at.isoformat() if e.finished_at else None,
            }
            for e, c in failures
        ],
        "pending_approvals": await reviews(HumanReview.status == "awaiting_second_approval"),
        "overdue_reviews": await reviews(HumanReview.status.in_(OPEN_REVIEW), HumanReview.due_at < now),
    }
