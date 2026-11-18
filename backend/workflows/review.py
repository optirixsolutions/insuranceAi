"""HUMAN_REVIEW node (Phase 9): opens one ``human_reviews`` row per interrupt and routes the
reviewer's outcome. Decisions themselves are taken (and authority-checked) by
``app.services.review_actions`` before the workflow is resumed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from langgraph.types import interrupt

from app.models.claims import Claim
from app.services import events as domain_events
from app.services import review_service
from app.services.review_service import ReviewFacts
from rules_engine.claim_status import ClaimStatus
from workflows import nodes as n
from workflows.runtime import Runtime, publish
from workflows.state import ClaimState

REVIEW_ACTIONS = ("approve", "reject", "request_information", "escalate")


async def _review_facts(rt: Runtime, state: ClaimState, decision: dict[str, Any]) -> ReviewFacts:
    runs = state.get("runs", {})
    fraud, assessment = await n._output(rt, runs.get("fraud")), await n._output(rt, runs.get("assessment"))
    payout = assessment.get("eligible_payout")
    summaries = state.get("summaries", {})
    failed_gates = [
        f"STP gate failed: {c.get('code')}"
        for c in summaries.get("_gates", {}).get("checks", [])
        if not c.get("passed")
    ]
    return ReviewFacts(
        recommendation=decision.get("recommendation"),
        payout=Decimal(str(payout))
        if assessment.get("status") == "calculated" and payout is not None
        else None,
        fraud_level=fraud.get("risk_level"),
        fraud_score=fraud.get("risk_score"),
        reasons=[
            *map(str, decision.get("mandatory_review_reasons", [])),
            *failed_gates,
            *map(str, summaries.get("_contradictions", [])),
        ],
    )


async def human_review(rt: Runtime, state: ClaimState) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Open (or, on the post-resume re-run, find) the review row, then wait for a person.

    The interrupt id is derived from ``review_round`` so the re-run after resume computes the same
    id and never opens a second review. Approve/reject are recorded by the review service before
    the resume; this node only routes.
    """
    rnd = state.get("review_round", 0)
    interrupt_id = f"review-{rnd}"
    decision = await n._output(rt, state.get("runs", {}).get("decision"))
    facts = await _review_facts(rt, state, decision)
    async with rt.session() as s:
        execution = await rt.execution(s)
        review, created = await review_service.open_for_interrupt(s, execution, interrupt_id, facts)
        events = []
        if created:
            events = await n.move(
                s, execution, ClaimStatus.HUMAN_REVIEW, "workflow: recommendation ready for review"
            )
            claim = await s.get(Claim, execution.claim_id)
            assert claim is not None
            deciders = await domain_events.holders_of(s, claim.organization_id, "reviews.decide")
            await domain_events.emit(
                s,
                claim.organization_id,
                "review.opened",
                domain_events.claim_data(
                    claim, review_id=str(review.id), recommendation=facts.recommendation
                ),
                claim_id=claim.id,
                notices=[
                    domain_events.Notice(
                        deciders,
                        "review.opened",
                        f"{claim.claim_reference} is waiting for human review",
                        f"Recommendation: {facts.recommendation or 'none'}. "
                        f"{len(facts.reasons)} reason(s) a person must decide.",
                        link=f"/reviews/{claim.id}/{review.id}",
                    )
                ],
            )
        await s.commit()
    await publish(rt.redis, events)
    count = state.get("reassessment_count", 0)
    actions = [*REVIEW_ACTIONS, *(["reassess"] if count < n.MAX_REASSESSMENTS else [])]
    answer = interrupt(
        {
            "interrupt_id": interrupt_id,
            "kind": "human_review",
            "review_id": str(review.id),
            "recommendation": decision.get("recommendation"),
            "matrix_rule": decision.get("matrix_rule"),
            "explanation": decision.get("explanation"),
            "mandatory_review_reasons": decision.get("mandatory_review_reasons", []),
            "contradictions": state.get("summaries", {}).get("_contradictions", []),
            "reassessments": count,
            "actions": actions,
        }
    )
    action = str(answer.get("action"))
    done = {"review_round": rnd + 1, "resume": answer}
    if action in ("approve", "reject"):
        verb = "Approved" if action == "approve" else "Rejected"
        return {**done, "route": "done"}, f"{verb} by reviewer", {"action": action}
    if action == "reassess" and count < n.MAX_REASSESSMENTS:
        async with rt.session() as s:
            (await rt.execution(s, lock=True)).reassessment_count = count + 1
            await s.commit()
        return (
            {**done, "route": "reassess", "reassessment_count": count + 1},
            "Returned for reassessment",
            {"action": action},
        )
    return {**done, "route": "more_info"}, "Information requested by reviewer", {"action": action}
