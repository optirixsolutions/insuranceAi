"""Coverage and exclusion evaluation. Every determination cites the clause it rests on.

Exclusions are evaluated by a small predicate library keyed by exclusion code. A predicate may
only conclude ``triggered`` on a hard fact; anything that depends on reading the narrative is
``needs_interpretation``; codes without a predicate are ``unevaluated`` (the policy agent then
asks the model to *flag* concerns and, without a model, treats them as needs_interpretation).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

CoverageStatus = Literal[
    "covered", "potentially_covered", "not_covered", "insufficient_evidence", "requires_manual_interpretation"
]
ExclusionStatus = Literal["triggered", "not_triggered", "needs_interpretation", "unevaluated"]


@dataclass(frozen=True, slots=True)
class CoverageTerms:
    coverage_code: str
    perils: tuple[str, ...]
    clause_ref: str


@dataclass(frozen=True, slots=True)
class ExclusionTerms:
    exclusion_code: str
    applies_to: tuple[str, ...]
    description: str
    clause_ref: str


@dataclass(frozen=True, slots=True)
class Facts:
    loss_type: str
    description: str
    incident_date: date
    documents_present: frozenset[str]
    fields: dict[str, str] = field(default_factory=dict)  # field_name -> normalised value (ISO dates)


@dataclass(frozen=True, slots=True)
class CoverageOutcome:
    coverage_code: str
    status: CoverageStatus
    reasons: tuple[str, ...]
    clause_ref: str


@dataclass(frozen=True, slots=True)
class ExclusionOutcome:
    exclusion_code: str
    status: ExclusionStatus
    reason: str
    clause_ref: str
    evidence: str | None = None


def evaluate_coverages(coverages: list[CoverageTerms], loss_type: str) -> list[CoverageOutcome]:
    out = []
    for c in coverages:
        if loss_type in c.perils:
            out.append(CoverageOutcome(c.coverage_code, "covered", ("PERIL_LISTED",), c.clause_ref))
        elif loss_type == "other":
            out.append(
                CoverageOutcome(
                    c.coverage_code, "requires_manual_interpretation", ("LOSS_TYPE_OTHER",), c.clause_ref
                )
            )
        else:
            out.append(CoverageOutcome(c.coverage_code, "not_covered", ("PERIL_NOT_LISTED",), c.clause_ref))
    return out


def _keywords(pattern: str) -> Callable[[Facts], tuple[ExclusionStatus, str, str | None]]:
    rx = re.compile(pattern, re.IGNORECASE)

    def check(f: Facts) -> tuple[ExclusionStatus, str, str | None]:
        m = rx.search(f.description)
        if m:
            return (
                "needs_interpretation",
                "The loss description mentions a possibly excluded cause",
                m.group(0),
            )
        return "not_triggered", "Nothing in the loss description points to this cause", None

    return check


def _unlicensed(f: Facts) -> tuple[ExclusionStatus, str, str | None]:
    expiry = f.fields.get("licence_expiry")
    if expiry:
        try:
            if date.fromisoformat(expiry) < f.incident_date:
                return "triggered", f"Driving licence expired on {expiry}, before the incident", expiry
        except ValueError:
            pass
    if "driving_licence" not in f.documents_present:
        return (
            "needs_interpretation",
            "No driving licence has been provided to confirm the driver held one",
            None,
        )
    return "not_triggered", "A driving licence is on file and no expiry before the incident was found", None


PREDICATES: dict[str, Callable[[Facts], tuple[ExclusionStatus, str, str | None]]] = {
    "unlicensed_driver": _unlicensed,
    "wear_and_tear": _keywords(
        r"\b(wear and tear|wear|worn|gradual(?:ly)?|rust(?:ed|ing)?|corro(?:sion|ded)|deteriorat\w*|"
        r"mechanical (?:failure|breakdown)|electrical (?:failure|fault))\b"
    ),
    "racing": _keywords(r"\b(rac(?:e|ing)|track day|speed (?:trial|event)|rally)\b"),
    "intoxication": _keywords(r"\b(drunk|alcohol|intoxicat\w*|under the influence|drugs?)\b"),
    "pre_existing": _keywords(
        r"\b(pre[- ]existing|chronic|long[- ]standing|history of|previously diagnosed|recurrence|recurring)\b"
    ),
    "cosmetic": _keywords(r"\b(cosmetic|aesthetic|elective)\b"),
}


def evaluate_exclusions(
    exclusions: list[ExclusionTerms], covered_codes: set[str], facts: Facts
) -> list[ExclusionOutcome]:
    """Only exclusions that apply to a coverage the claim could fall under are evaluated."""
    out = []
    for e in exclusions:
        if not covered_codes.intersection(e.applies_to):
            continue
        predicate = PREDICATES.get(e.exclusion_code)
        if predicate is None:
            out.append(
                ExclusionOutcome(e.exclusion_code, "unevaluated", "No machine-checkable rule", e.clause_ref)
            )
            continue
        status, reason, evidence = predicate(facts)
        out.append(ExclusionOutcome(e.exclusion_code, status, reason, e.clause_ref, evidence))
    return out
