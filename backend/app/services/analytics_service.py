"""Analytics (05 §2.11 ``/analytics/*``) and the assessments register. One aggregate query per figure,
restricted to the caller's visible claims; rates over zero rows are ``None``."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from sqlalchemy import column, distinct, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agents import AgentOutput, AgentRun
from app.models.ai import ModelInvocation
from app.models.catalog import InsuranceProduct
from app.models.claims import Claim
from app.models.documents import ClaimDocument, ExtractedField, FieldCorrection
from app.models.rules import ClaimDecision
from app.models.workflow import WorkflowExecution
from app.services.dashboard_service import FLAGGED, machine_time
from app.services.metrics_common import Scope, ms, percentile, ratio

FAILED_RUNS = ("failed", "timed_out")  # skipped and partial runs are not failures


async def processing_times(
    db: AsyncSession, scope: Scope, product_id: uuid.UUID | None = None
) -> dict[str, Any]:
    machine = machine_time()
    wall = WorkflowExecution.duration_ms
    stmt = (
        select(
            Claim.product_id,
            InsuranceProduct.name,
            func.count(WorkflowExecution.id),
            func.avg(wall),
            percentile(0.5, wall),
            percentile(0.9, wall),
            percentile(0.95, wall),
            percentile(0.5, machine.c.machine_ms),
            percentile(0.9, machine.c.machine_ms),
            percentile(0.95, machine.c.machine_ms),
        )
        .select_from(WorkflowExecution)
        .join(Claim, Claim.id == WorkflowExecution.claim_id)
        .join(InsuranceProduct, InsuranceProduct.id == Claim.product_id)
        .outerjoin(machine, machine.c.execution_id == WorkflowExecution.id)
        .where(
            scope.visible(Claim.id),
            WorkflowExecution.status == "completed",
            WorkflowExecution.finished_at >= scope.since,
        )
        .group_by(Claim.product_id, InsuranceProduct.name)
        .order_by(InsuranceProduct.name)
    )
    if product_id is not None:
        stmt = stmt.where(Claim.product_id == product_id)
    rows = (await db.execute(stmt)).all()
    return {
        "days": scope.days,
        "definition": "Completed processing runs per product. Wall time runs from start to finish and "
        "includes waits for people; processing time is the sum of step durations.",
        "products": [
            {
                "product_id": str(pid),
                "product_name": name,
                "runs": int(n),
                "wall_avg_ms": ms(avg),
                "wall_p50_ms": ms(p50),
                "wall_p90_ms": ms(p90),
                "wall_p95_ms": ms(p95),
                "machine_p50_ms": ms(m50),
                "machine_p90_ms": ms(m90),
                "machine_p95_ms": ms(m95),
            }
            for pid, name, n, avg, p50, p90, p95, m50, m90, m95 in rows
        ],
    }


async def claims(db: AsyncSession, scope: Scope, bucket: Literal["day", "week"] = "day") -> dict[str, Any]:
    seen = scope.visible(Claim.id)
    period = func.date_trunc(bucket, Claim.reported_at)
    recent = Claim.reported_at >= scope.since
    series = (
        await db.execute(select(period, func.count()).where(seen, recent).group_by(period).order_by(period))
    ).all()
    by_category = (
        await db.execute(
            select(Claim.category, func.count())
            .where(seen, recent)
            .group_by(Claim.category)
            .order_by(Claim.category)
        )
    ).all()
    by_status = (
        await db.execute(
            select(Claim.status, func.count()).where(seen).group_by(Claim.status).order_by(Claim.status)
        )
    ).all()
    decisions = (
        await db.execute(
            select(ClaimDecision.decision, ClaimDecision.decision_mode, func.count())
            .where(scope.visible(ClaimDecision.claim_id), ClaimDecision.created_at >= scope.since)
            .group_by(ClaimDecision.decision, ClaimDecision.decision_mode)
            .order_by(ClaimDecision.decision, ClaimDecision.decision_mode)
        )
    ).all()
    return {
        "days": scope.days,
        "bucket": bucket,
        "definition": "Claims reported in the period by day or week and by category; current status of all "
        "visible claims; decisions recorded in the period by outcome and mode.",
        "volume": [{"bucket": b.date().isoformat(), "count": int(n)} for b, n in series],
        "by_category": [{"category": c, "count": int(n)} for c, n in by_category],
        "by_status": [{"status": s, "count": int(n)} for s, n in by_status],
        "decisions": [{"decision": d, "mode": m, "count": int(n)} for d, m, n in decisions],
    }


async def fraud(db: AsyncSession, scope: Scope) -> dict[str, Any]:
    level = AgentOutput.output["risk_level"].as_string()
    latest = (
        select(AgentRun.claim_id, level.label("level"), AgentOutput.output.label("output"))
        .join(AgentOutput, AgentOutput.agent_run_id == AgentRun.id)
        .where(
            scope.visible(AgentRun.claim_id),
            AgentRun.agent_key == "fraud",
            AgentRun.started_at >= scope.since,
        )
        .distinct(AgentRun.claim_id)
        .order_by(AgentRun.claim_id, AgentRun.started_at.desc())
        .subquery()
    )
    by_level = (await db.execute(select(latest.c.level, func.count()).group_by(latest.c.level))).all()
    elements = func.jsonb_array_elements(latest.c.output["indicators"])
    indicator = elements.table_valued(column("value", JSONB)).alias("i")
    code = indicator.c.value["rule_code"].as_string()
    top = (
        await db.execute(
            select(code, func.count())
            .select_from(latest)
            .join(indicator, literal(True))
            .group_by(code)
            .order_by(func.count().desc(), code)
            .limit(10)
        )
    ).all()
    # Label: the last human approve/reject on a flagged claim. Approved after a flag = false positive.
    final = (
        select(ClaimDecision.claim_id, ClaimDecision.decision)
        .where(ClaimDecision.decision_mode == "human", ClaimDecision.decision.in_(("approved", "rejected")))
        .distinct(ClaimDecision.claim_id)
        .order_by(ClaimDecision.claim_id, ClaimDecision.created_at.desc())
        .subquery()
    )
    labelled = (
        await db.execute(
            select(func.count(), func.count().filter(final.c.decision == "approved"))
            .select_from(latest)
            .join(final, final.c.claim_id == latest.c.claim_id)
            .where(latest.c.level.in_(FLAGGED))
        )
    ).one()
    levels = {str(k): int(n) for k, n in by_level}
    return {
        "days": scope.days,
        "definition": (
            "Latest fraud analysis per claim in the period. False-positive rate counts only flagged "
            "(high/critical) claims with a final human decision: approved after a flag counts as a "
            "false positive."
        ),
        "by_level": levels,
        "alerts": sum(n for k, n in levels.items() if k in FLAGGED),
        "top_indicators": [{"code": c, "count": int(n)} for c, n in top],
        "labelled_flagged": int(labelled[0]),
        "false_positives": int(labelled[1]),
        "false_positive_rate": ratio(int(labelled[1]), int(labelled[0])),
    }


async def agents(db: AsyncSession, scope: Scope) -> dict[str, Any]:
    recent = (scope.visible(AgentRun.claim_id), AgentRun.started_at >= scope.since)
    failed = func.count().filter(AgentRun.status.in_(FAILED_RUNS))
    rows = (
        await db.execute(
            select(
                AgentRun.agent_key,
                func.count(),
                failed,
                func.count().filter(AgentRun.attempt > 1),
                percentile(0.5, AgentRun.duration_ms),
                percentile(0.95, AgentRun.duration_ms),
                func.count().filter(AgentRun.used_llm.is_(True)),
                func.count().filter(AgentRun.escalate.is_(True)),
                func.sum(AgentRun.prompt_tokens + AgentRun.completion_tokens),
                func.sum(AgentRun.cost_usd),
            )
            .where(*recent)
            .group_by(AgentRun.agent_key)
            .order_by(AgentRun.agent_key)
        )
    ).all()
    totals = (
        await db.execute(
            select(
                func.coalesce(func.sum(AgentRun.cost_usd), 0), func.count(distinct(AgentRun.claim_id))
            ).where(*recent)
        )
    ).one()
    models = (
        await db.execute(
            select(
                ModelInvocation.model_slug,
                func.count(),
                func.count().filter(ModelInvocation.status != "ok"),
                percentile(0.95, ModelInvocation.latency_ms),
                func.sum(ModelInvocation.cost_usd),
            )
            .join(AgentRun, AgentRun.id == ModelInvocation.agent_run_id)
            .where(*recent)
            .group_by(ModelInvocation.model_slug)
            .order_by(ModelInvocation.model_slug)
        )
    ).all()
    return {
        "days": scope.days,
        "definition": (
            "Agent runs started in the period. Failure = a run that failed or timed out; retry = "
            "attempt above 1. Model calls are those made by these runs; an error is any non-ok call."
        ),
        "agents": [
            {
                "agent_key": k,
                "runs": int(n),
                "failed": int(f),
                "failure_rate": ratio(int(f), int(n)),
                "retries": int(r),
                "p50_ms": ms(p50),
                "p95_ms": ms(p95),
                "llm_rate": ratio(int(llm), int(n)),
                "escalation_rate": ratio(int(esc), int(n)),
                "tokens": int(tok or 0),
                "cost_usd": float(cost or 0),
            }
            for k, n, f, r, p50, p95, llm, esc, tok, cost in rows
        ],
        "cost_per_claim_usd": round(float(totals[0]) / int(totals[1]), 6) if totals[1] else None,
        "claims": int(totals[1]),
        "models": [
            {
                "model_slug": slug,
                "calls": int(n),
                "errors": int(e),
                "error_rate": ratio(int(e), int(n)),
                "p95_latency_ms": ms(p95),
                "cost_usd": float(c or 0),
            }
            for slug, n, e, p95, c in models
        ],
    }


async def extraction_accuracy(db: AsyncSession, scope: Scope) -> dict[str, Any]:
    machine = (
        select(
            ClaimDocument.document_type.label("doc_type"),
            ExtractedField.id.label("field_id"),
            ExtractedField.grounding_status.label("grounding"),
        )
        .join(ClaimDocument, ClaimDocument.id == ExtractedField.document_id)
        .where(
            scope.visible(ClaimDocument.claim_id),
            ExtractedField.source.in_(("rules", "llm")),
            ExtractedField.created_at >= scope.since,
        )
        .subquery()
    )
    corrected = select(FieldCorrection.previous_field_id).scalar_subquery()
    rows = (
        await db.execute(
            select(
                machine.c.doc_type,
                func.count(),
                func.count().filter(machine.c.field_id.in_(corrected)),
                func.count().filter(machine.c.grounding == "ungrounded"),
            )
            .group_by(machine.c.doc_type)
            .order_by(machine.c.doc_type)
        )
    ).all()
    return {
        "days": scope.days,
        "definition": "Machine-extracted fields (rules or model) in the period per document type. Accuracy = "
        "1 - fields a person corrected / fields extracted. Stored evaluation runs are not included yet.",
        "document_types": [
            {
                "document_type": t or "unclassified",
                "fields": int(n),
                "corrected": int(c),
                "accuracy": None if not n else round(1 - int(c) / int(n), 4),
                "ungrounded": int(u),
            }
            for t, n, c, u in rows
        ],
    }


async def assessments(db: AsyncSession, scope: Scope, limit: int = 100) -> list[dict[str, Any]]:
    """The latest assessment per visible claim (the assessments register)."""
    out = AgentOutput.output
    rows = (
        await db.execute(
            select(
                Claim.id,
                Claim.claim_reference,
                Claim.status,
                AgentRun.rule_set_version,
                AgentRun.finished_at,
                out,
            )
            .join(AgentRun, AgentRun.claim_id == Claim.id)
            .join(AgentOutput, AgentOutput.agent_run_id == AgentRun.id)
            .where(scope.visible(Claim.id), AgentRun.agent_key == "assessment")
            .distinct(Claim.id)
            .order_by(Claim.id, AgentRun.started_at.desc())
        )
    ).all()
    items = [
        {
            "claim_id": str(cid),
            "claim_reference": ref,
            "claim_status": status,
            "assessment_status": o.get("status"),
            "eligible_payout": o.get("eligible_payout"),
            "currency": o.get("currency"),
            "rule_set_version": version,
            "assessed_at": finished.isoformat() if finished else None,
        }
        for cid, ref, status, version, finished, o in rows
    ]
    items.sort(key=lambda i: i["assessed_at"] or "", reverse=True)
    return items[:limit]
