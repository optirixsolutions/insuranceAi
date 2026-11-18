"""Straight-through processing nodes (Phase 8): AUTO_APPROVAL_ELIGIBILITY and FINALIZATION.

The gate is re-evaluated from persisted outputs and the product's published rule set, never from
graph state. FINALIZATION records an append-only ``claim_decisions`` row (mode ``stp``) and moves
the claim to APPROVED in the same transaction. Any failed gate sends the claim to a person.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.models.agents import AgentOutput, AgentRun
from app.models.claims import Claim
from app.models.rules import ClaimDecision
from app.services import ai_service, rule_set_service
from app.services import events as domain_events
from rules_engine.claim_status import ClaimStatus
from rules_engine.gates import StpInputs, stp_gate
from rules_engine.rule_set import DEFAULT_CONFIG, RuleSetConfig
from workflows import nodes as n
from workflows.runtime import Runtime, publish
from workflows.state import ClaimState


async def _flagged(s: Any, claim: Claim, months: int) -> bool:
    """Another claim by this customer within ``months`` whose fraud analysis was high or critical."""
    since = datetime.now(UTC) - timedelta(days=30 * months)
    rows = (
        (
            await s.execute(
                select(AgentOutput.output)
                .join(AgentRun, AgentRun.id == AgentOutput.agent_run_id)
                .join(Claim, Claim.id == AgentRun.claim_id)
                .where(
                    Claim.customer_id == claim.customer_id,
                    Claim.id != claim.id,
                    AgentRun.agent_key == "fraud",
                    AgentRun.started_at >= since,
                )
            )
        )
        .scalars()
        .all()
    )
    return any(str(o.get("risk_level")) in ("high", "critical") for o in rows)


async def eligibility(rt: Runtime, state: ClaimState) -> tuple[dict[str, Any], str, dict[str, Any]]:
    runs = state.get("runs", {})
    out = {
        k: await n._output(rt, runs.get(k))
        for k in ("decision", "fraud", "assessment", "document", "validation", "compliance")
    }
    async with rt.session() as s:
        claim = await s.get(Claim, rt.claim_id)
        assert claim is not None
        rule_set = await rule_set_service.active(s, claim.product_id)
        config = RuleSetConfig.model_validate(rule_set.config) if rule_set else DEFAULT_CONFIG
        flagged = await _flagged(s, claim, config.stp.claimant_flag_months)
        approved = claim.is_synthetic or await ai_service.org_allows_real_data(s, rt.organization_id)
    payout = out["assessment"].get("eligible_payout")
    result = stp_gate(
        config,
        StpInputs(
            recommendation=out["decision"].get("recommendation"),
            deterministic_outcome=out["decision"].get("deterministic_outcome"),
            llm_escalated=bool(out["decision"].get("llm_escalated")),
            fraud_score=out["fraud"].get("risk_score"),
            payout=Decimal(str(payout))
            if out["assessment"].get("status") == "calculated" and payout is not None
            else None,
            currency_matches=out["assessment"].get("currency") == claim.currency,
            fields_awaiting_verification=sum(
                len(d.get("needs_human_verification", [])) for d in out["document"].get("documents", [])
            ),
            validation_max_severity=out["validation"].get("max_severity"),
            claimant_flagged_recently=flagged,
            synthetic_or_ai_approved=approved,
            compliance_status=out["compliance"].get("status"),
        ),
    )
    failed = [c for c, ok, _ in result.checks if not ok]
    summary = "All STP gates passed" if result.eligible else f"Sent to review: {', '.join(failed)}"
    gates = {
        "eligible": result.eligible,
        "checks": result.as_list(),
        "rule_set_id": str(rule_set.id) if rule_set else None,
    }
    return (
        {"route": "finalize" if result.eligible else "human_review", "summaries": {"_gates": gates}},
        summary,
        {"failed": failed},
    )


async def finalize(rt: Runtime, state: ClaimState) -> tuple[dict[str, Any], str, dict[str, Any]]:
    gates = state.get("summaries", {}).get("_gates", {})
    if not gates.get("eligible"):
        raise n.NodeFailedError("FINALIZATION reached without a passed STP gate")
    assessment = await n._output(rt, state.get("runs", {}).get("assessment"))
    async with rt.session() as s:
        claim = await s.scalar(select(Claim).where(Claim.id == rt.claim_id).with_for_update(key_share=True))
        assert claim is not None
        execution = await rt.execution(s)
        decision = ClaimDecision(
            id=uuid.uuid4(),
            organization_id=claim.organization_id,
            claim_id=claim.id,
            execution_id=execution.id,
            decision="approved",
            approved_amount=Decimal(str(assessment["eligible_payout"])),
            currency=claim.currency,
            decided_by=None,
            decision_mode="stp",
            rule_set_id=uuid.UUID(gates["rule_set_id"]) if gates.get("rule_set_id") else None,
            gates=gates.get("checks", []),
            before_state={"status": claim.status},
            after_state={
                "status": ClaimStatus.APPROVED.value,
                "approved_amount": str(assessment["eligible_payout"]),
            },
            created_at=datetime.now(UTC),
        )
        s.add(decision)
        await s.flush()
        events = await n.move(
            s, execution, ClaimStatus.APPROVED, "straight-through approval (all gates passed)"
        )
        await domain_events.emit(
            s,
            claim.organization_id,
            "claim.decided",
            domain_events.claim_data(
                claim,
                decision="approved",
                decision_mode="stp",
                approved_amount=str(decision.approved_amount),
                currency=claim.currency,
                claim_decision_id=str(decision.id),
            ),
            claim_id=claim.id,
            notices=[
                domain_events.Notice(
                    await domain_events.claim_people(s, claim),
                    "claim.decided",
                    f"{claim.claim_reference} was approved straight through",
                    f"All automated gates passed; approved {decision.approved_amount} {claim.currency}.",
                    link=f"/claims/{claim.id}?tab=decision",
                )
            ],
        )
        await s.commit()
    await publish(rt.redis, events)
    summary = f"Approved straight through: {assessment['eligible_payout']} {claim.currency}"
    return {"route": "done"}, summary, {"claim_decision_id": str(decision.id)}
