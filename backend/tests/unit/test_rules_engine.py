"""Pure business rules: exercised directly, no mocks needed."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from rules_engine.claim_status import (
    ClaimStatus as S,
)
from rules_engine.claim_status import (
    InvalidTransitionError,
    allowed_targets,
    assert_transition,
    can_transition,
)
from rules_engine.intake import LOSS_TYPES, PolicyTerms, assess, duplicate_window
from rules_engine.money import Money, MoneyError, validate_amount

TERMS = PolicyTerms(date(2026, 1, 1), date(2027, 1, 1), Decimal("20000"), "USD", 0)


# ---- money --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        ("5200", "USD", "5200.00"),
        ("5200.5", "USD", "5200.50"),
        ("150000", "JPY", "150000"),
        ("1.234", "KWD", "1.234"),
    ],
)
def test_valid_amounts_are_normalised(amount: str, currency: str, expected: str) -> None:
    assert str(validate_amount(Decimal(amount), currency)) == expected


@pytest.mark.parametrize(
    ("amount", "currency", "message"),
    [
        ("10.505", "USD", "at most 2 decimal"),
        ("100.5", "JPY", "at most 0 decimal"),
        ("0", "USD", "greater than zero"),
        ("-1", "USD", "greater than zero"),
        ("1e20", "USD", "too large"),
        ("10", "XYZ", "Unsupported currency"),
    ],
)
def test_invalid_amounts_are_rejected(amount: str, currency: str, message: str) -> None:
    with pytest.raises(MoneyError, match=message):
        validate_amount(Decimal(amount), currency)


def test_money_never_mixes_currencies_and_rounds_half_even() -> None:
    assert (Money(Decimal("1.005"), "USD") + Money(Decimal("2"), "USD")).amount == Decimal("3.005")
    assert Money(Decimal("2.125"), "USD").rounded().amount == Decimal("2.12")
    assert Money(Decimal("2.135"), "USD").rounded().amount == Decimal("2.14")
    with pytest.raises(MoneyError, match="Cannot combine"):
        _ = Money(Decimal("1"), "USD") + Money(Decimal("1"), "EUR")
    with pytest.raises(MoneyError):
        Money(1.5, "USD")  # type: ignore[arg-type]  # deliberately wrong type


# ---- status machine ------------------------------------------------------------------------


def test_humans_can_only_make_documented_manual_moves() -> None:
    assert allowed_targets(S.SUBMITTED, "human") == [S.AWAITING_INFORMATION]
    assert allowed_targets(S.AWAITING_INFORMATION, "human") == [S.SUBMITTED]
    assert can_transition(S.FAILED, S.DOCUMENT_PROCESSING, "human")


@pytest.mark.parametrize("target", [S.APPROVED, S.REJECTED])
def test_decisions_cannot_be_set_as_plain_status(target: S) -> None:
    for current in S:
        assert not can_transition(current, target, "human")
        assert not can_transition(current, target, "workflow")
    assert can_transition(S.HUMAN_REVIEW, target, "decision")


def test_rejection_requires_human_review() -> None:
    assert not can_transition(S.DECISION_PENDING, S.REJECTED, "decision")


def test_completed_is_terminal_and_invalid_moves_raise() -> None:
    for actor in ("workflow", "human", "decision"):
        assert allowed_targets(S.COMPLETED, actor) == []
    with pytest.raises(InvalidTransitionError, match="SUBMITTED to APPROVED"):
        assert_transition(S.SUBMITTED, S.APPROVED, "human")


# ---- intake --------------------------------------------------------------------------------


def _assess(amount: str, **kw: object) -> tuple[str, set[str]]:
    params: dict[str, object] = {
        "incident_date": date(2026, 6, 1),
        "reported_on": date(2026, 6, 3),
        "claimed_amount": Decimal(amount),
        "currency": "USD",
        "terms": TERMS,
        "policy_status": "active",
        "duplicate_references": [],
    }
    params.update(kw)
    result = assess(**params)  # type: ignore[arg-type]
    return result.priority, {f.code for f in result.findings}


def test_priority_thresholds() -> None:
    assert _assess("20000") == ("urgent", set())
    assert _assess("10000") == ("high", set())
    assert _assess("5200") == ("normal", set())
    assert _assess("300") == ("low", set())


def test_findings_are_raised_and_affect_priority() -> None:
    assert _assess("300", duplicate_references=["CLM-2026-000001"]) == ("high", {"POSSIBLE_DUPLICATE"})
    assert _assess("300", terms=None) == ("normal", {"NO_POLICY_VERSION"})
    assert _assess("300", policy_status="lapsed")[1] == {"POLICY_NOT_ACTIVE"}
    assert _assess("300", currency="EUR")[1] == {"CURRENCY_MISMATCH"}
    late = _assess("300", reported_on=date(2026, 10, 1))
    assert late == ("low", {"LATE_REPORT"})


def test_waiting_period() -> None:
    terms = PolicyTerms(date(2026, 5, 20), date(2027, 5, 20), Decimal("20000"), "USD", 30)
    assert "WITHIN_WAITING_PERIOD" in _assess("5000", terms=terms)[1]
    assert "WITHIN_WAITING_PERIOD" not in _assess("5000", terms=terms, incident_date=date(2026, 7, 1))[1]


def test_duplicate_window_and_loss_types() -> None:
    assert duplicate_window(date(2026, 6, 10)) == (date(2026, 6, 7), date(2026, 6, 13))
    assert "collision" in LOSS_TYPES["motor"] and "inpatient" in LOSS_TYPES["health"]
