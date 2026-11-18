"""Phase 8: property-based money and calculator tests (Hypothesis), rule-set validation, the STP gate,
configurable fraud weights and the claim-status path finder."""

from __future__ import annotations

from dataclasses import replace
from decimal import ROUND_HALF_EVEN, Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from rules_engine.calculation import LineItem, health_medical, motor_depreciation_pct, motor_own_damage
from rules_engine.claim_status import ClaimStatus, InvalidTransitionError, allowed_targets, path
from rules_engine.fraud_scoring import Indicator, score
from rules_engine.gates import StpInputs, stp_gate
from rules_engine.money import CURRENCY_EXPONENTS, Money, MoneyError, to_minor_units, validate_amount
from rules_engine.rule_set import DEFAULT_CONFIG, DepreciationBand, RuleSetConfig, StpConfig

D = Decimal
cents = st.integers(min_value=0, max_value=10**11).map(lambda c: D(c) / 100)
currencies = st.sampled_from(sorted(c for c, e in CURRENCY_EXPONENTS.items() if e == 2))
categories = st.sampled_from(["parts", "labour", "paint", "other"])


@st.composite
def depreciation_tables(draw: st.DrawFn) -> tuple[tuple[int, Decimal], ...]:
    ages = sorted(set(draw(st.lists(st.integers(1, 30), max_size=6))))
    pcts = sorted(draw(st.lists(st.integers(0, 100), min_size=len(ages) + 1, max_size=len(ages) + 1)))
    bands = [DepreciationBand(min_age_years=0, pct=D(pcts[0]))]
    bands += [DepreciationBand(min_age_years=a, pct=D(p)) for a, p in zip(ages, pcts[1:], strict=True)]
    return RuleSetConfig(motor_parts_depreciation=bands).depreciation_table()


# --- money properties -------------------------------------------------------------------------


@given(cents, cents, currencies)
def test_money_addition_is_exact_and_commutative(a: Decimal, b: Decimal, cur: str) -> None:
    assert Money(a, cur) + Money(b, cur) == Money(b, cur) + Money(a, cur)
    assert (Money(a, cur) + Money(b, cur)).amount == a + b
    assert ((Money(a, cur) + Money(b, cur)) - Money(b, cur)).amount == a


@given(cents, currencies, currencies)
def test_cross_currency_arithmetic_always_raises(a: Decimal, x: str, y: str) -> None:
    if x == y:
        return
    with pytest.raises(MoneyError):
        Money(a, x) + Money(a, y)
    with pytest.raises(MoneyError):
        Money(a, x) - Money(a, y)


@given(st.decimals(min_value=0, max_value=10**9, places=6, allow_nan=False), currencies)
def test_rounding_happens_once_and_is_idempotent(a: Decimal, cur: str) -> None:
    once = Money(a, cur).rounded()
    assert once.rounded() == once
    assert once.amount == a.quantize(D("0.01"), rounding=ROUND_HALF_EVEN)
    assert to_minor_units(a, cur) == once.amount


@given(st.decimals(min_value=D("0.001"), max_value=10**6, places=3, allow_nan=False))
def test_over_precise_amounts_are_rejected(a: Decimal) -> None:
    if a == a.quantize(D("0.01")):
        assert validate_amount(a, "GBP") == a
    else:
        with pytest.raises(MoneyError):
            validate_amount(a, "GBP")


def test_money_rejects_floats_and_unknown_currencies() -> None:
    with pytest.raises(MoneyError):
        Money(1.5, "GBP")  # type: ignore[arg-type]
    with pytest.raises(MoneyError):
        Money(D(1), "XXX")
    with pytest.raises(MoneyError):
        validate_amount(D("-1"), "GBP")
    with pytest.raises(MoneyError):
        validate_amount(D(0), "GBP")
    assert validate_amount(D(0), "GBP", allow_zero=True) == D(0)
    with pytest.raises(MoneyError):
        validate_amount(D("1e20"), "GBP")
    with pytest.raises(MoneyError):
        validate_amount(D("100.5"), "JPY")
    assert str(Money(D("1.005"), "USD")) == "1.00 USD"


# --- calculator properties under any valid rule set -------------------------------------------


@settings(max_examples=300, deadline=None)
@given(
    st.lists(st.tuples(categories, cents), min_size=1, max_size=8),
    st.integers(0, 40),
    cents,
    cents,
    cents,
    depreciation_tables(),
)
def test_motor_payout_properties(
    lines: list[tuple[str, Decimal]],
    age: int,
    limit: Decimal,
    ded: Decimal,
    salvage: Decimal,
    table: tuple[tuple[int, Decimal], ...],
) -> None:
    items = [LineItem(i + 1, c, c, a) for i, (c, a) in enumerate(lines)]

    def run(**kw: object) -> Decimal:
        args: dict[str, object] = {
            "currency": "GBP",
            "items": items,
            "vehicle_age_years": age,
            "salvage": salvage,
            "limit_remaining": limit,
            "deductible": ded,
            "depreciation": table,
        }
        return motor_own_damage(**{**args, **kw}).eligible_payout  # type: ignore[arg-type]

    calc = motor_own_damage(
        currency="GBP",
        items=items,
        vehicle_age_years=age,
        salvage=salvage,
        limit_remaining=limit,
        deductible=ded,
        depreciation=table,
    )
    p = calc.eligible_payout
    assert D(0) <= p <= limit + D("0.005") and p <= calc.gross_eligible + D("0.005")
    assert p.as_tuple().exponent == -2 and [s.op for s in calc.trace].count("round_once") == 1
    assert calc.gross_eligible <= calc.gross_claimed
    assert run(deductible=ded + 1) <= p  # a higher deductible never pays more
    assert run(vehicle_age_years=age + 5) <= p  # an older vehicle never pays more (monotonic table)
    assert motor_depreciation_pct(age, table) <= motor_depreciation_pct(age + 5, table)


@settings(max_examples=300, deadline=None)
@given(
    st.lists(st.tuples(st.sampled_from(["room", "procedure", "medication"]), cents), min_size=1, max_size=8),
    st.dictionaries(st.sampled_from(["room", "procedure"]), cents, max_size=2),
    cents,
    st.integers(0, 100).map(D),
    cents,
)
def test_health_payout_properties(
    lines: list[tuple[str, Decimal]], subs: dict[str, Decimal], agg: Decimal, co: Decimal, ded: Decimal
) -> None:
    items = [LineItem(i + 1, c, c, a) for i, (c, a) in enumerate(lines)]
    calc = health_medical(
        currency="USD", items=items, sub_limits=subs, aggregate_remaining=agg, co_pay_pct=co, deductible=ded
    )
    p = calc.eligible_payout
    assert D(0) <= p <= agg + D("0.005")
    for cat, limit in subs.items():
        assert sum((a.eligible for a in calc.items if a.item.category == cat), D(0)) <= limit
    more = health_medical(
        currency="USD",
        items=items,
        sub_limits=subs,
        aggregate_remaining=agg,
        co_pay_pct=co,
        deductible=ded + 1,
    )
    assert more.eligible_payout <= p


# --- rule-set validation -----------------------------------------------------------------------


def bands(*pairs: tuple[int, str]) -> list[DepreciationBand]:
    return [DepreciationBand(min_age_years=a, pct=D(p)) for a, p in pairs]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"motor_parts_depreciation": bands((1, "5"))}, "age 0"),
        ({"motor_parts_depreciation": bands((0, "0"), (0, "5"))}, "duplicate"),
        ({"motor_parts_depreciation": bands((0, "20"), (3, "10"))}, "decrease"),
        ({"fraud_weights": {"F-MADE-UP": 5}}, "unknown fraud"),
        ({"health_sub_limits": {"spa": D("10")}}, "sub-limit categories"),
        ({"stp": StpConfig(enabled=True, max_amount=D("5000"))}, "compliance profile"),
        ({"automated_approval_allowed": True, "stp": StpConfig(enabled=True)}, "maximum amount"),
    ],
)
def test_invalid_rule_sets_are_rejected(kwargs: dict[str, object], message: str) -> None:
    base: dict[str, object] = {"motor_parts_depreciation": DEFAULT_CONFIG.motor_parts_depreciation}
    with pytest.raises(ValidationError, match=message):
        RuleSetConfig(**{**base, **kwargs})


def test_defaults_reproduce_the_phase6_constants() -> None:
    from rules_engine.calculation import MOTOR_PARTS_DEPRECIATION

    assert DEFAULT_CONFIG.depreciation_table() == MOTOR_PARTS_DEPRECIATION
    assert DEFAULT_CONFIG.weight("F-DUP-CLAIM") == 30 and not DEFAULT_CONFIG.stp.enabled
    with pytest.raises(ValidationError):
        RuleSetConfig.model_validate({"motor_parts_depreciation": [], "surprise": 1})


def test_fraud_weights_and_llm_cap_come_from_the_rule_set() -> None:
    ev: tuple[dict[str, object], ...] = ({"kind": "claim", "id": "c"},)
    inds = [Indicator("F-DUP-CLAIM", "d", ev), Indicator("F-NARRATIVE", "n", ev)]
    assert score(inds).score == 45
    assert score(inds, {"F-DUP-CLAIM": 50}, D(5)).score == 55


# --- STP gate ----------------------------------------------------------------------------------

STP = DEFAULT_CONFIG.model_copy(
    update={"automated_approval_allowed": True, "stp": StpConfig(enabled=True, max_amount=D("5000"))}
)
OK = StpInputs(
    "APPROVE_RECOMMENDED",
    "APPROVE_RECOMMENDED",
    False,
    0,
    D("815.00"),
    True,
    0,
    None,
    False,
    True,
    "review_required",
)


def test_stp_gate_passes_only_when_every_condition_holds() -> None:
    assert stp_gate(STP, OK).eligible
    assert not stp_gate(DEFAULT_CONFIG, OK).eligible  # STP off by default
    failing = {
        "APPROVE_RECOMMENDED": replace(OK, recommendation="MANUAL_REVIEW_REQUIRED"),
        "NO_MODEL_ESCALATION": replace(OK, llm_escalated=True),
        "FRAUD_SCORE": replace(OK, fraud_score=25),
        "PAYOUT_LIMIT": replace(OK, payout=D("5000.01")),
        "CURRENCY": replace(OK, currency_matches=False),
        "FIELDS_VERIFIED": replace(OK, fields_awaiting_verification=1),
        "VALIDATION_CLEAN": replace(OK, validation_max_severity="warning"),
        "CLAIMANT_NOT_FLAGGED": replace(OK, claimant_flagged_recently=True),
        "DATA_POLICY": replace(OK, synthetic_or_ai_approved=False),
        "COMPLIANCE": replace(OK, compliance_status="non_compliant"),
    }
    for code, inputs in failing.items():
        result = stp_gate(STP, inputs)
        assert not result.eligible and [c for c, ok, _ in result.checks if not ok] == [code], code
    assert not stp_gate(STP, replace(OK, payout=None, fraud_score=None)).eligible
    assert stp_gate(STP, OK).as_list()[0]["code"] == "STP_ENABLED"


# --- claim-status path finder ------------------------------------------------------------------


def test_status_paths_walk_only_allowed_transitions() -> None:
    s = ClaimStatus
    assert path(s.SUBMITTED, s.SUBMITTED, "workflow") == []
    assert path(s.SUBMITTED, s.HUMAN_REVIEW, "workflow")[-1] is s.HUMAN_REVIEW
    assert path(s.DECISION_PENDING, s.APPROVED, "decision") == [s.APPROVED]
    with pytest.raises(InvalidTransitionError):
        path(s.SUBMITTED, s.APPROVED, "workflow")  # approval always needs a decision
    with pytest.raises(InvalidTransitionError):
        path(s.COMPLETED, s.SUBMITTED, "workflow")
    assert s.SUBMITTED in allowed_targets(s.AWAITING_INFORMATION, "human")
