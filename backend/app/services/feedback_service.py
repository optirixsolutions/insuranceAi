"""Feedback loop (Phase 12): labels on agent outputs, from people's decisions and from people directly.

* When a person approves or rejects a claim, the latest decision-recommendation run is labelled
  ``agree``/``disagree`` and a fraud analysis that scored high or critical is labelled ``false_positive``
  (approved anyway) or ``true_positive`` (rejected). These are the labels the analytics and evaluation
  curation use; nobody has to enter them.
* Anyone who can read the claim may mark an agent run ``helpful``/``not_helpful`` or ``agree``/``disagree``
  with a note (one label per person per run; a new one replaces the old).
* Disagreements are listed as candidate cases for new evaluation sets.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleError, NotFoundError
from app.models.agents import AgentOutput, AgentRun
from app.models.claims import Claim
from app.models.intelligence import AgentFeedback
from app.security.principal import Principal
from app.services.claim_access import get_visible_claim
from app.services.metrics_common import Scope, ratio

PERSON_LABELS = ("helpful", "not_helpful", "agree", "disagree")
AGREES = {"approved": "APPROVE_RECOMMENDED", "rejected": "REJECT_RECOMMENDED"}


async def _latest(
    db: AsyncSession, claim_id: uuid.UUID, agent_key: str
) -> tuple[AgentRun, dict[str, Any]] | None:
    row = (
        await db.execute(
            select(AgentRun, AgentOutput.output)
            .join(AgentOutput, AgentOutput.agent_run_id == AgentRun.id)
            .where(AgentRun.claim_id == claim_id, AgentRun.agent_key == agent_key)
            .order_by(AgentRun.started_at.desc())
            .limit(1)
        )
    ).first()
    return (row[0], dict(row[1])) if row else None


async def _label(
    db: AsyncSession, claim: Claim, run: AgentRun, label: str, decider: uuid.UUID | None, note: str
) -> None:
    await db.execute(
        insert(AgentFeedback)
        .values(
            id=uuid.uuid4(),
            organization_id=claim.organization_id,
            claim_id=claim.id,
            agent_run_id=run.id,
            agent_key=run.agent_key,
            label=label,
            source="review_decision",
            note=note[:1000],
            created_by=decider,
            created_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing()
    )


async def from_decision(db: AsyncSession, claim: Claim, decision: str, decider: uuid.UUID | None) -> None:
    """Called in the decision's transaction (human approve/reject)."""
    if decision not in AGREES:
        return
    rec = await _latest(db, claim.id, "decision")
    if rec is not None:
        run, out = rec
        agreed = out.get("recommendation") == AGREES[decision]
        note = f"recommended {out.get('recommendation')}, person {decision}"
        await _label(db, claim, run, "agree" if agreed else "disagree", decider, note)
    fraud = await _latest(db, claim.id, "fraud")
    if fraud is not None and fraud[1].get("risk_level") in ("high", "critical"):
        label = "false_positive" if decision == "approved" else "true_positive"
        note = f"risk {fraud[1].get('risk_level')}, person {decision}"
        await _label(db, claim, fraud[0], label, decider, note)


async def give(
    db: AsyncSession, principal: Principal, run_id: uuid.UUID, label: str, note: str | None
) -> AgentFeedback:
    if label not in PERSON_LABELS:
        raise BusinessRuleError(f"Choose one of {list(PERSON_LABELS)}")
    run = await db.get(AgentRun, run_id)
    if run is None or run.organization_id != principal.organization_id:
        raise NotFoundError("Agent run not found")
    await get_visible_claim(db, principal, run.claim_id)
    await db.execute(
        delete(AgentFeedback).where(
            AgentFeedback.agent_run_id == run.id,
            AgentFeedback.source == "person",
            AgentFeedback.created_by == principal.user_id,
        )
    )
    row = AgentFeedback(
        id=uuid.uuid4(),
        organization_id=run.organization_id,
        claim_id=run.claim_id,
        agent_run_id=run.id,
        agent_key=run.agent_key,
        label=label,
        source="person",
        note=(note or "")[:1000] or None,
        created_by=principal.user_id,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    return row


def _rates(counts: dict[str, int]) -> dict[str, float | None]:
    def r(a: str, b: str) -> float | None:
        return ratio(counts.get(a, 0), counts.get(a, 0) + counts.get(b, 0))

    return {
        "agreement_rate": r("agree", "disagree"),
        "precision": r("true_positive", "false_positive"),
        "helpful_rate": r("helpful", "not_helpful"),
    }


async def summary(db: AsyncSession, scope: Scope) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(AgentFeedback.agent_key, AgentFeedback.label, func.count())
            .where(scope.visible(AgentFeedback.claim_id), AgentFeedback.created_at >= scope.since)
            .group_by(AgentFeedback.agent_key, AgentFeedback.label)
        )
    ).all()
    agents: dict[str, dict[str, int]] = {}
    for key, label, n in rows:
        agents.setdefault(key, {})[label] = int(n)
    return {
        "days": scope.days,
        "definition": "Labels on agent outputs in the period. Agreement = agree / (agree + disagree); fraud "
        "precision = true positives / (true + false positives), from people's final decisions on flagged "
        "claims; helpful rate from people's own ratings.",
        "agents": [{"agent_key": k, "counts": c, **_rates(c)} for k, c in sorted(agents.items())],
    }


async def curation_cases(db: AsyncSession, scope: Scope, limit: int = 100) -> list[dict[str, Any]]:
    """Disagreements, false positives and unhelpful outputs: candidates for new labelled evaluation cases."""
    rows = (
        await db.execute(
            select(AgentFeedback, Claim.claim_reference)
            .join(Claim, Claim.id == AgentFeedback.claim_id)
            .where(
                scope.visible(AgentFeedback.claim_id),
                AgentFeedback.label.in_(("disagree", "false_positive", "not_helpful")),
                AgentFeedback.created_at >= scope.since,
            )
            .order_by(AgentFeedback.created_at.desc())
            .limit(min(limit, 500))
        )
    ).all()
    return [
        {
            "claim_id": str(f.claim_id),
            "claim_reference": ref,
            "agent_key": f.agent_key,
            "agent_run_id": str(f.agent_run_id),
            "label": f.label,
            "source": f.source,
            "note": f.note,
            "created_at": f.created_at.isoformat(),
        }
        for f, ref in rows
    ]
