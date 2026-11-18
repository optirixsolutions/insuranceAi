"""Straight-through processing gate (03 §5.2). Pure: every check is listed with its outcome.

All conditions must hold. The gate is re-evaluated from persisted data at AUTO_APPROVAL_ELIGIBILITY
(never from graph state), and any doubt sends the claim to a person. No gate ever rejects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from rules_engine.rule_set import RuleSetConfig

SEVERITY = {None: 0, "info": 0, "warning": 1, "error": 2, "critical": 3}


@dataclass(frozen=True, slots=True)
class StpInputs:
    recommendation: str | None
    deterministic_outcome: str | None
    llm_escalated: bool
    fraud_score: int | None
    payout: Decimal | None
    currency_matches: bool
    fields_awaiting_verification: int
    validation_max_severity: str | None
    claimant_flagged_recently: bool
    synthetic_or_ai_approved: bool
    compliance_status: str | None


@dataclass(slots=True)
class GateResult:
    eligible: bool
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def as_list(self) -> list[dict[str, object]]:
        return [{"code": c, "passed": p, "detail": d} for c, p, d in self.checks]


def stp_gate(config: RuleSetConfig, i: StpInputs) -> GateResult:
    stp = config.stp
    approve = i.recommendation == "APPROVE_RECOMMENDED" and i.deterministic_outcome == "APPROVE_RECOMMENDED"
    fraud_ok = i.fraud_score is not None and i.fraud_score < stp.max_fraud_score
    payout_ok = (
        i.payout is not None and stp.max_amount is not None and Decimal(0) < i.payout <= stp.max_amount
    )
    clean = SEVERITY.get(i.validation_max_severity, 3) <= SEVERITY["info"]
    checks: list[tuple[str, bool, str]] = [
        ("STP_ENABLED", stp.enabled, "straight-through processing is enabled for this product"),
        ("PROFILE_ALLOWS", config.automated_approval_allowed, "compliance profile allows automated approval"),
        (
            "APPROVE_RECOMMENDED",
            approve,
            f"recommendation {i.recommendation}, matrix {i.deterministic_outcome}",
        ),
        ("NO_MODEL_ESCALATION", not i.llm_escalated, "no model escalation"),
        ("FRAUD_SCORE", fraud_ok, f"fraud score {i.fraud_score} below {stp.max_fraud_score}"),
        ("PAYOUT_LIMIT", payout_ok, f"payout {i.payout} within {stp.max_amount}"),
        ("CURRENCY", i.currency_matches, "payout currency matches the policy"),
        (
            "FIELDS_VERIFIED",
            i.fields_awaiting_verification == 0,
            f"{i.fields_awaiting_verification} field(s) awaiting verification",
        ),
        ("VALIDATION_CLEAN", clean, f"worst validation finding {i.validation_max_severity or 'none'}"),
        (
            "CLAIMANT_NOT_FLAGGED",
            not i.claimant_flagged_recently,
            f"claimant not flagged in the last {stp.claimant_flag_months} months",
        ),
        ("DATA_POLICY", i.synthetic_or_ai_approved, "synthetic claim or AI processing approved"),
        (
            "COMPLIANCE",
            i.compliance_status in ("compliant", "review_required"),
            f"compliance {i.compliance_status}",
        ),
    ]
    return GateResult(all(p for _, p, _ in checks), checks)
