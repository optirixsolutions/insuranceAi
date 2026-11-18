"""Deterministic intake rules: loss types, priority, duplicate window and findings.

Documented thresholds (applied in this order; the first match wins):

* ``urgent``  claimed amount >= sum insured (possible total loss or limit breach)
* ``high``    claimed amount >= 50% of sum insured, or a possible duplicate exists
* ``low``     claimed amount < 2% of sum insured and no findings above ``info``
* ``normal``  otherwise (also when no policy version applies on the incident date)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

DUPLICATE_WINDOW_DAYS = 3
LATE_REPORT_DAYS = 90

LOSS_TYPES: dict[str, tuple[str, ...]] = {
    "motor": ("collision", "theft", "fire", "weather", "vandalism", "glass", "third_party", "other"),
    "health": ("inpatient", "outpatient", "emergency", "maternity", "day_care", "other"),
    "property": ("fire", "flood", "storm", "theft", "escape_of_water", "subsidence", "other"),
    "travel": ("medical", "cancellation", "baggage", "delay", "other"),
}

DEFAULT_DOCUMENTS: dict[str, tuple[tuple[str, bool], ...]] = {
    "motor": (
        ("claim_form", True),
        ("driving_licence", True),
        ("repair_estimate", True),
        ("photo_evidence", True),
        ("police_report", False),
        ("repair_invoice", False),
    ),
    "health": (
        ("claim_form", True),
        ("identity_document", True),
        ("hospital_report", True),
        ("medical_bill", True),
        ("bank_information", False),
    ),
    "property": (
        ("claim_form", True),
        ("photo_evidence", True),
        ("repair_estimate", True),
        ("police_report", False),
    ),
    "travel": (("claim_form", True), ("identity_document", True), ("supporting_correspondence", True)),
}

Severity = Literal["info", "warning", "error"]
Priority = Literal["low", "normal", "high", "urgent"]


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: Severity
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "message": self.message}


@dataclass(frozen=True, slots=True)
class PolicyTerms:
    effective_from: date
    effective_to: date
    sum_insured: Decimal
    currency: str
    waiting_period_days: int


@dataclass(slots=True)
class IntakeAssessment:
    priority: Priority
    findings: list[Finding] = field(default_factory=list)


def duplicate_window(incident_date: date) -> tuple[date, date]:
    delta = timedelta(days=DUPLICATE_WINDOW_DAYS)
    return incident_date - delta, incident_date + delta


def assess(
    *,
    incident_date: date,
    reported_on: date,
    claimed_amount: Decimal,
    currency: str,
    terms: PolicyTerms | None,
    policy_status: str,
    duplicate_references: list[str],
) -> IntakeAssessment:
    findings: list[Finding] = []
    if policy_status != "active":
        findings.append(Finding("POLICY_NOT_ACTIVE", "error", f"Policy status is '{policy_status}'"))
    if terms is None:
        findings.append(
            Finding("NO_POLICY_VERSION", "error", "No published policy version covers the incident date")
        )
    else:
        if terms.currency != currency:
            findings.append(
                Finding(
                    "CURRENCY_MISMATCH",
                    "error",
                    f"Claim currency {currency} differs from policy currency {terms.currency}",
                )
            )
        waiting_ends = terms.effective_from + timedelta(days=terms.waiting_period_days)
        if terms.waiting_period_days and incident_date < waiting_ends:
            findings.append(
                Finding(
                    "WITHIN_WAITING_PERIOD",
                    "warning",
                    f"Incident is within the {terms.waiting_period_days}-day waiting period",
                )
            )
    if duplicate_references:
        findings.append(
            Finding(
                "POSSIBLE_DUPLICATE",
                "warning",
                "Other claims on this policy within ±3 days: " + ", ".join(duplicate_references),
            )
        )
    if (reported_on - incident_date).days > LATE_REPORT_DAYS:
        findings.append(
            Finding("LATE_REPORT", "info", f"Reported more than {LATE_REPORT_DAYS} days after the incident")
        )

    priority: Priority = "normal"
    if terms is not None and terms.currency == currency:
        if claimed_amount >= terms.sum_insured:
            priority = "urgent"
        elif claimed_amount * 2 >= terms.sum_insured or duplicate_references:
            priority = "high"
        elif claimed_amount * 50 < terms.sum_insured and all(f.severity == "info" for f in findings):
            priority = "low"
    elif duplicate_references:
        priority = "high"
    return IntakeAssessment(priority, findings)
