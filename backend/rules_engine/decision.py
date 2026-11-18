"""Deterministic decision matrix (03-agent-contracts.md §5.1). Top-down, first match wins.

The model may only *escalate*: APPROVE_RECOMMENDED or REJECT_RECOMMENDED -> MANUAL_REVIEW_REQUIRED,
and only with cited evidence. ``apply_escalation`` enforces that monotonic rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Recommendation(StrEnum):
    APPROVE = "APPROVE_RECOMMENDED"
    REJECT = "REJECT_RECOMMENDED"
    MANUAL_REVIEW = "MANUAL_REVIEW_REQUIRED"
    MORE_INFO = "MORE_INFORMATION_REQUIRED"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"


@dataclass(frozen=True, slots=True)
class DecisionInputs:
    missing_required_documents: tuple[str, ...]
    policy_version_resolved: bool
    assessment_status: str | None  # calculated | insufficient_inputs | None (not run)
    low_confidence_fields: tuple[str, ...]
    av_scan_pending: bool
    coverage_statuses: dict[str, str]  # coverage_code -> status
    primary_coverage: str | None
    exclusions_need_interpretation: tuple[str, ...]
    exclusions_triggered_with_clause: tuple[str, ...]
    validation_max_severity: str | None  # info | warning | error | critical
    fraud_level: str | None  # low | medium | high | critical
    contradictions: tuple[str, ...]
    policy_active: bool | None
    payout: str | None  # Decimal as string, None when not calculated


@dataclass(slots=True)
class MatrixResult:
    outcome: Recommendation
    rule: str
    applied_rules: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    mandatory_review_reasons: list[str] = field(default_factory=list)


_SEVERITY = {"info": 0, "warning": 1, "error": 2, "critical": 3}


def evaluate(d: DecisionInputs) -> MatrixResult:
    applied: list[str] = []
    missing = [f"document: {doc}" for doc in d.missing_required_documents]
    if not d.policy_version_resolved:
        missing.append("policy version in force on the incident date")
    if d.assessment_status in (None, "insufficient_inputs"):
        missing.append("calculable assessment")
    review: list[str] = []
    if any(s == "requires_manual_interpretation" for s in d.coverage_statuses.values()):
        review.append("coverage requires manual interpretation")
    review += [f"exclusion needs interpretation: {e}" for e in d.exclusions_need_interpretation]
    if _SEVERITY.get(d.validation_max_severity or "info", 0) >= 3:
        review.append("critical validation finding")
    if d.fraud_level in ("high", "critical"):
        review.append(f"fraud risk {d.fraud_level}")
    review += [f"contradiction: {c}" for c in d.contradictions]

    # 1. Mandatory inputs missing.
    # A policy that is plainly not in force is a reason to reject (row 4), not to ask for more.
    no_cover = d.policy_active is False
    blocking_missing = [m for m in missing if not (no_cover and m.startswith("policy version"))]
    if no_cover:
        blocking_missing = [m for m in blocking_missing if m != "calculable assessment"]
    if blocking_missing:
        return MatrixResult(Recommendation.MORE_INFO, "M1", ["M1 mandatory inputs missing"], missing, review)
    applied.append("M1 inputs complete")
    # 2. Verification pending.
    if d.low_confidence_fields or d.av_scan_pending:
        pending = [f"verify field: {f}" for f in d.low_confidence_fields]
        if d.av_scan_pending:
            pending.append("antivirus scan pending")
        return MatrixResult(
            Recommendation.PENDING_VERIFICATION, "M2", [*applied, "M2 verification pending"], pending, review
        )
    applied.append("M2 nothing awaiting verification")
    # 3. Manual review triggers.
    if review:
        return MatrixResult(
            Recommendation.MANUAL_REVIEW, "M3", [*applied, "M3 mandatory review condition"], [], review
        )
    applied.append("M3 no mandatory review condition")
    # 4. Reject recommended (always human-reviewed).
    statuses = list(d.coverage_statuses.values())
    all_not_covered = bool(statuses) and all(s == "not_covered" for s in statuses)
    if d.policy_active is False or all_not_covered or d.exclusions_triggered_with_clause:
        reasons = []
        if d.policy_active is False:
            reasons.append("policy not active on the incident date")
        if all_not_covered:
            reasons.append("no claimed coverage applies (clauses cited)")
        reasons += [f"exclusion triggered: {e}" for e in d.exclusions_triggered_with_clause]
        return MatrixResult(
            Recommendation.REJECT,
            "M4",
            [*applied, "M4 " + "; ".join(reasons)],
            [],
            ["rejections are always human-reviewed"],
        )
    applied.append("M4 no rejection ground")
    # 5. Approve recommended.
    primary_ok = d.primary_coverage is not None and d.coverage_statuses.get(d.primary_coverage) == "covered"
    validation_ok = _SEVERITY.get(d.validation_max_severity or "info", 0) < 2
    payout_ok = d.payout is not None and float(d.payout) > 0
    if primary_ok and validation_ok and d.fraud_level == "low" and payout_ok:
        return MatrixResult(
            Recommendation.APPROVE, "M5", [*applied, "M5 covered, clean, low risk, payout > 0"]
        )
    # 6. Otherwise.
    why = []
    if not primary_ok:
        why.append("primary coverage not confirmed")
    if not validation_ok:
        why.append("validation errors")
    if d.fraud_level != "low":
        why.append(f"fraud risk {d.fraud_level or 'not assessed'}")
    if not payout_ok:
        why.append("no positive payout")
    return MatrixResult(Recommendation.MANUAL_REVIEW, "M6", [*applied, "M6 otherwise"], [], why)


def apply_escalation(
    outcome: Recommendation, escalate: bool, evidence_count: int
) -> tuple[Recommendation, bool]:
    """Monotonic: the model can only move approve/reject to manual review, with evidence."""
    if escalate and evidence_count > 0 and outcome in (Recommendation.APPROVE, Recommendation.REJECT):
        return Recommendation.MANUAL_REVIEW, True
    return outcome, False
