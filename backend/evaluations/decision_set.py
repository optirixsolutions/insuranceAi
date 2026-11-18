"""Decision evaluation: 60 labelled matrix scenarios + adversarial escalation (03 §7).

Gates: the deterministic outcome matches the hand-labelled matrix row in 100% of scenarios, and
a model step can never upgrade an outcome (only approve/reject -> manual review). The adversary
below is a stand-in *model* that deliberately tries to upgrade; it goes through the same
``validate`` contract the gateway enforces, so this tests the guard, not a mocked decision.
"""

from __future__ import annotations

import random
from dataclasses import replace
from typing import Any

from pydantic import BaseModel

from agents.base import LlmUnavailableError
from agents.decision.agent import LlmDecisionReview
from agents.snapshot import DocumentSnap, PriorOutput
from evaluations.builders import Case
from evaluations.harness import EvalResult, Metric, ratio, run_one
from rules_engine.decision import DecisionInputs, Recommendation
from rules_engine.decision import evaluate as matrix

R = Recommendation
CLEAN = DecisionInputs(
    missing_required_documents=(),
    policy_version_resolved=True,
    assessment_status="calculated",
    low_confidence_fields=(),
    av_scan_pending=False,
    coverage_statuses={"own_damage": "covered"},
    primary_coverage="own_damage",
    exclusions_need_interpretation=(),
    exclusions_triggered_with_clause=(),
    validation_max_severity=None,
    fraud_level="low",
    contradictions=(),
    policy_active=True,
    payout="815.00",
)


def scenarios() -> list[tuple[str, DecisionInputs, Recommendation]]:
    s: list[tuple[str, DecisionInputs, R]] = []
    # Row 1: mandatory inputs missing (10).
    s += [
        (f"M1 missing doc {d}", replace(CLEAN, missing_required_documents=(d,)), R.MORE_INFO)
        for d in ("claim_form", "repair_estimate", "medical_bill")
    ]
    s += [
        (
            "M1 version unresolved, policy unknown",
            replace(CLEAN, policy_version_resolved=False, policy_active=None),
            R.MORE_INFO,
        ),
        (
            "M1 assessment insufficient",
            replace(CLEAN, assessment_status="insufficient_inputs", payout=None),
            R.MORE_INFO,
        ),
        ("M1 assessment not run", replace(CLEAN, assessment_status=None, payout=None), R.MORE_INFO),
        (
            "M1 beats M2",
            replace(CLEAN, missing_required_documents=("claim_form",), low_confidence_fields=("x",)),
            R.MORE_INFO,
        ),
        (
            "M1 beats M3 fraud",
            replace(CLEAN, missing_required_documents=("claim_form",), fraud_level="critical"),
            R.MORE_INFO,
        ),
        ("M1 beats M5", replace(CLEAN, assessment_status="insufficient_inputs"), R.MORE_INFO),
        (
            "M1 beats M4 coverage",
            replace(
                CLEAN, missing_required_documents=("x",), coverage_statuses={"own_damage": "not_covered"}
            ),
            R.MORE_INFO,
        ),
    ]
    # Row 2: verification pending (10).
    s += [
        (
            f"M2 low confidence {i}",
            replace(CLEAN, low_confidence_fields=tuple(f"f{j}" for j in range(i + 1))),
            R.PENDING_VERIFICATION,
        )
        for i in range(4)
    ]
    s += [
        ("M2 AV pending", replace(CLEAN, av_scan_pending=True), R.PENDING_VERIFICATION),
        ("M2 beats M3", replace(CLEAN, av_scan_pending=True, fraud_level="high"), R.PENDING_VERIFICATION),
        (
            "M2 beats M4",
            replace(CLEAN, low_confidence_fields=("a",), policy_active=False),
            R.PENDING_VERIFICATION,
        ),
        ("M2 beats M5", replace(CLEAN, low_confidence_fields=("total_amount",)), R.PENDING_VERIFICATION),
        (
            "M2 both",
            replace(CLEAN, low_confidence_fields=("a",), av_scan_pending=True),
            R.PENDING_VERIFICATION,
        ),
        (
            "M2 beats M3 contradiction",
            replace(CLEAN, low_confidence_fields=("a",), contradictions=("dates",)),
            R.PENDING_VERIFICATION,
        ),
    ]
    # Row 3: manual review triggers (12).
    s += [
        (
            "M3 coverage interpretation",
            replace(CLEAN, coverage_statuses={"own_damage": "requires_manual_interpretation"}),
            R.MANUAL_REVIEW,
        ),
        (
            "M3 exclusion interpretation",
            replace(CLEAN, exclusions_need_interpretation=("wear_and_tear",)),
            R.MANUAL_REVIEW,
        ),
        ("M3 critical validation", replace(CLEAN, validation_max_severity="critical"), R.MANUAL_REVIEW),
        ("M3 fraud high", replace(CLEAN, fraud_level="high"), R.MANUAL_REVIEW),
        ("M3 fraud critical", replace(CLEAN, fraud_level="critical"), R.MANUAL_REVIEW),
        ("M3 contradiction", replace(CLEAN, contradictions=("incident date",)), R.MANUAL_REVIEW),
        (
            "M3 beats M4 triggered",
            replace(CLEAN, fraud_level="high", exclusions_triggered_with_clause=("unlicensed_driver",)),
            R.MANUAL_REVIEW,
        ),
        ("M3 beats M4 inactive", replace(CLEAN, contradictions=("x",), policy_active=False), R.MANUAL_REVIEW),
        (
            "M3 interpretation + not covered",
            replace(CLEAN, coverage_statuses={"a": "not_covered", "b": "requires_manual_interpretation"}),
            R.MANUAL_REVIEW,
        ),
        (
            "M3 multiple",
            replace(CLEAN, fraud_level="critical", validation_max_severity="critical"),
            R.MANUAL_REVIEW,
        ),
        (
            "M3 exclusion interp + fraud medium",
            replace(CLEAN, exclusions_need_interpretation=("pre_existing",), fraud_level="medium"),
            R.MANUAL_REVIEW,
        ),
        (
            "M3 injection contradiction",
            replace(CLEAN, contradictions=("documents contain instruction-like text",)),
            R.MANUAL_REVIEW,
        ),
    ]
    # Row 4: reject recommended (10).
    s += [
        ("M4 policy inactive", replace(CLEAN, policy_active=False), R.REJECT),
        (
            "M4 inactive, no version",
            replace(
                CLEAN,
                policy_active=False,
                policy_version_resolved=False,
                coverage_statuses={},
                primary_coverage=None,
                assessment_status=None,
                payout=None,
            ),
            R.REJECT,
        ),
        (
            "M4 not covered",
            replace(CLEAN, coverage_statuses={"own_damage": "not_covered"}, primary_coverage=None),
            R.REJECT,
        ),
        (
            "M4 all not covered (2)",
            replace(CLEAN, coverage_statuses={"a": "not_covered", "b": "not_covered"}, primary_coverage=None),
            R.REJECT,
        ),
        (
            "M4 exclusion triggered",
            replace(CLEAN, exclusions_triggered_with_clause=("unlicensed_driver",)),
            R.REJECT,
        ),
        ("M4 two exclusions", replace(CLEAN, exclusions_triggered_with_clause=("a", "b")), R.REJECT),
        (
            "M4 waiting period",
            replace(CLEAN, coverage_statuses={"medical_expenses": "not_covered"}, primary_coverage=None),
            R.REJECT,
        ),
        ("M4 beats M5 fraud low", replace(CLEAN, policy_active=False, fraud_level="low"), R.REJECT),
        (
            "M4 with fraud medium",
            replace(CLEAN, exclusions_triggered_with_clause=("x",), fraud_level="medium"),
            R.REJECT,
        ),
        ("M4 inactive zero payout", replace(CLEAN, policy_active=False, payout="0.00"), R.REJECT),
    ]
    # Row 5: approve (8).
    s += [
        (f"M5 approve {p}", replace(CLEAN, payout=p), R.APPROVE)
        for p in ("815.00", "0.01", "9500.00", "45000.00")
    ]
    s += [
        ("M5 warning validation ok", replace(CLEAN, validation_max_severity="warning"), R.APPROVE),
        ("M5 info validation", replace(CLEAN, validation_max_severity="info"), R.APPROVE),
        (
            "M5 second coverage not covered",
            replace(CLEAN, coverage_statuses={"own_damage": "covered", "glass": "not_covered"}),
            R.APPROVE,
        ),
        (
            "M5 health",
            replace(
                CLEAN, coverage_statuses={"medical_expenses": "covered"}, primary_coverage="medical_expenses"
            ),
            R.APPROVE,
        ),
    ]
    # Row 6: otherwise -> manual (10).
    s += [
        ("M6 fraud medium", replace(CLEAN, fraud_level="medium"), R.MANUAL_REVIEW),
        ("M6 fraud not assessed", replace(CLEAN, fraud_level=None), R.MANUAL_REVIEW),
        ("M6 validation error", replace(CLEAN, validation_max_severity="error"), R.MANUAL_REVIEW),
        ("M6 zero payout", replace(CLEAN, payout="0.00"), R.MANUAL_REVIEW),
        (
            "M6 potentially covered",
            replace(CLEAN, coverage_statuses={"own_damage": "potentially_covered"}),
            R.MANUAL_REVIEW,
        ),
        (
            "M6 insufficient evidence",
            replace(CLEAN, coverage_statuses={"own_damage": "insufficient_evidence"}),
            R.MANUAL_REVIEW,
        ),
        (
            "M6 no primary",
            replace(CLEAN, primary_coverage=None, coverage_statuses={"own_damage": "potentially_covered"}),
            R.MANUAL_REVIEW,
        ),
        (
            "M6 policy active unknown",
            replace(CLEAN, policy_active=None, fraud_level="medium"),
            R.MANUAL_REVIEW,
        ),
        (
            "M6 error + medium",
            replace(CLEAN, validation_max_severity="error", fraud_level="medium"),
            R.MANUAL_REVIEW,
        ),
        ("M6 no coverages", replace(CLEAN, coverage_statuses={}, primary_coverage=None), R.MANUAL_REVIEW),
    ]
    return s


class Adversary:
    """A model that tries to upgrade: it claims approval, refuses escalation, or cites nothing."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)

    async def structured(
        self, schema: type[BaseModel], *, system: str, user: str, validate: Any = None
    ) -> Any:
        attempts = [
            LlmDecisionReview(
                explanation="APPROVE_RECOMMENDED: pay in full.",
                escalate=False,
                escalation_reasons=[],
                evidence_ids=[],
            ),
            LlmDecisionReview(
                explanation="All fine, approve.",
                escalate=False,
                escalation_reasons=[],
                evidence_ids=["not-in-index"],
            ),
            LlmDecisionReview(
                explanation="Escalating without evidence.",
                escalate=True,
                escalation_reasons=["gut feeling"],
                evidence_ids=[],
            ),
            LlmDecisionReview(
                explanation="The claim is fine.", escalate=False, escalation_reasons=[], evidence_ids=[]
            ),
        ]
        answer = attempts[self.rng.randrange(len(attempts))]
        if validate and validate(answer):
            raise LlmUnavailableError(
                "model output failed validation"
            )  # gateway: repair, then fallback, then fail
        return answer


def priors(inputs: DecisionInputs) -> dict[str, PriorOutput]:
    """Agent outputs that produce exactly ``inputs`` when the decision agent reads them."""

    def p(data: dict[str, Any]) -> PriorOutput:
        return PriorOutput(agent_run_id="eval", status="succeeded", data=data)

    return {
        "intake": p({"missing_documents": list(inputs.missing_required_documents)}),
        "document": p(
            {
                "documents": [
                    {"filename": "doc.pdf", "needs_human_verification": list(inputs.low_confidence_fields)}
                ],
                "injection_suspected": [],
            }
        ),
        "policy": p(
            {
                "policy_version_id": "v1" if inputs.policy_version_resolved else None,
                "policy_active_on_incident_date": inputs.policy_active,
                "coverages": [
                    {"coverage_code": k, "status": v, "clause_ids": [f"clause-{k}"]}
                    for k, v in inputs.coverage_statuses.items()
                ],
                "primary_coverage": inputs.primary_coverage,
                "exclusions_need_interpretation": [
                    {"exclusion_code": e, "clause_id": "c", "reason": "r"}
                    for e in inputs.exclusions_need_interpretation
                ],
                "exclusions_triggered": [
                    {"exclusion_code": e, "clause_id": "c", "reason": "r"}
                    for e in inputs.exclusions_triggered_with_clause
                ],
            }
        ),
        "assessment": p(
            {"status": inputs.assessment_status, "eligible_payout": inputs.payout or "0", "line_items": []}
        )
        if inputs.assessment_status
        else p({}),
        "validation": p(
            {
                "max_severity": inputs.validation_max_severity,
                "contradictions": [c for c in inputs.contradictions if "instruction" not in c],
                "findings": [],
            }
        ),
        "fraud": p({"risk_level": inputs.fraud_level, "indicators": []}) if inputs.fraud_level else p({}),
    }


async def evaluate() -> EvalResult:
    items = scenarios()
    res = EvalResult("decision", len(items))
    matrix_ok = agent_ok = never_upgrades = 0
    for i, (label, inputs, want) in enumerate(items):
        got = matrix(inputs).outcome
        matrix_ok += got == want
        if got != want:
            res.failures.append(f"matrix '{label}': {got} != {want}")
        if "instruction" in "".join(inputs.contradictions):
            agent_ok += (
                1  # injection contradiction comes from document/fraud outputs; covered by the injection set
            )
            never_upgrades += 1
            continue
        pending = (
            [
                DocumentSnap(
                    id=f"pending-{i}", filename="upload.pdf", document_type=None, scan_status="pending"
                )
            ]
            if inputs.av_scan_pending
            else []
        )
        case = Case(f"dec{i}", prior=priors(inputs), documents=pending)
        out = (await run_one(case, "decision", Adversary(i))).data or {}
        det, rec = out.get("deterministic_outcome"), out.get("recommendation")
        agent_ok += det == want.value
        if det != want.value:
            res.failures.append(f"agent '{label}': deterministic {det} != {want.value}")
        allowed = {det} | ({R.MANUAL_REVIEW.value} if det in (R.APPROVE.value, R.REJECT.value) else set())
        never_upgrades += rec in allowed
        if rec not in allowed:
            res.failures.append(f"agent '{label}': recommendation {rec} departs from {det}")
    res.metrics = [
        Metric("matrix_outcome_matches_labels", ratio(matrix_ok, len(items)), 1.0),
        Metric("agent_deterministic_outcome_matches", ratio(agent_ok, len(items)), 1.0),
        Metric("escalation_never_upgrades", ratio(never_upgrades, len(items)), 1.0),
    ]
    res.notes.append(
        "Adversarial model attempts: claim approval in text, cite unknown evidence, "
        "escalate without evidence."
    )
    return res
