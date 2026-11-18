"""Phase 6 deterministic cores: text matching, policy validity, coverage, calculators, fraud
scoring and the decision matrix. Pure functions; the evaluation sets cover them end to end."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from rules_engine.calculation import LineItem, health_medical, motor_depreciation_pct, motor_own_damage
from rules_engine.coverage import (
    CoverageTerms,
    ExclusionTerms,
    Facts,
    evaluate_coverages,
    evaluate_exclusions,
)
from rules_engine.decision import DecisionInputs, Recommendation, apply_escalation, evaluate
from rules_engine.fraud_scoring import Indicator, level_for, score
from rules_engine.policy_validity import VersionTerms, is_active, select_version, waiting_period_ok
from rules_engine.text import excerpt_in_text, jaro_winkler, name_similarity, names_match, token_set_ratio

D = Decimal
EV: tuple[dict[str, object], ...] = ({"kind": "claim", "id": "c1"},)


# --- text -------------------------------------------------------------------------------------


def test_jaro_winkler_matches_the_reference_values() -> None:
    assert jaro_winkler("MARTHA", "MARHTA") == pytest.approx(0.9611, abs=1e-4)
    assert jaro_winkler("DIXON", "DICKSONX") == pytest.approx(0.8133, abs=1e-4)
    assert jaro_winkler("", "") == 0.0 and jaro_winkler("abc", "abc") == 1.0


@pytest.mark.parametrize(
    ("a", "b", "match"),
    [
        ("Rohan Tanaka", "TANAKA, Rohan", True),
        ("Dr Ayesha Khan", "Ayesha Khan", True),
        ("A Khan", "Ayesha Khan", True),
        ("Rohen Tanaka", "Rohan Tanaka", True),  # one OCR letter
        ("Marcus Bellweather", "Rohan Tanaka", False),
        ("Khan", "Ayesha Khan", False),  # a surname alone is not enough
    ],
)
def test_name_matching(a: str, b: str, match: bool) -> None:
    assert names_match(a, b) is match
    assert 0 <= name_similarity(a, b) <= 1


def test_grounding_excerpts_tolerate_ocr_noise_but_not_invention() -> None:
    page = "Repair estimate from Bridgewater Auto Body\nTotal due: GBP 1,475.00\nDate of loss: 08 Oct 2026"
    assert excerpt_in_text("Total due: GBP 1,475.00", page) == 1.0
    assert excerpt_in_text("Total due GBP 1,475.0O", page) >= 0.9  # OCR O-for-0
    assert excerpt_in_text("Total due: GBP 9,999.00 approved", page) == 0.0
    assert excerpt_in_text("", page) == 0.0
    assert token_set_ratio("loss of date", "date of loss") == 1.0


# --- policy validity --------------------------------------------------------------------------


def v(no: int, start: date, end: date, status: str = "published", wait: int = 0) -> VersionTerms:
    return VersionTerms(f"v{no}", no, status, start, end, wait)


def test_version_selection_prefers_the_latest_issued_version_in_force() -> None:
    versions = [
        v(1, date(2026, 1, 1), date(2026, 12, 31), "superseded"),
        v(2, date(2026, 1, 1), date(2026, 12, 31)),
        v(3, date(2026, 1, 1), date(2026, 12, 31), "draft"),
    ]
    chosen, reason = select_version(versions, date(2026, 5, 1))
    assert chosen is not None and chosen.version_no == 2 and "supersedes 1" in reason


@pytest.mark.parametrize(
    ("incident", "expect"),
    [(date(2027, 2, 1), "Cover ended"), (date(2025, 6, 1), "Cover starts"), (date(2026, 7, 1), "gap")],
)
def test_no_version_in_force_explains_why(incident: date, expect: str) -> None:
    versions = [v(1, date(2026, 1, 1), date(2026, 6, 30)), v(2, date(2026, 8, 1), date(2027, 1, 31))]
    if expect == "gap":
        chosen, reason = select_version(versions, incident)
    else:
        chosen, reason = select_version(versions[:1] if expect == "Cover starts" else versions[1:], incident)
    assert chosen is None and expect in reason


def test_validity_and_waiting_period_from_first_inception() -> None:
    renewal = [
        v(1, date(2025, 1, 1), date(2025, 12, 31), "superseded", 30),
        v(2, date(2026, 1, 1), date(2026, 12, 31), "published", 30),
    ]
    assert waiting_period_ok(renewal[1], renewal, date(2026, 1, 5))  # continuous cover since 2025
    assert not waiting_period_ok(renewal[0], renewal[:1], date(2025, 1, 30))
    assert waiting_period_ok(renewal[0], renewal[:1], date(2025, 1, 31))
    assert is_active("active", renewal[1], date(2026, 3, 1))
    assert not is_active("cancelled", renewal[1], date(2026, 3, 1))
    assert not is_active("active", None, date(2026, 3, 1))


# --- coverage -----------------------------------------------------------------------------------


def facts(**kw: object) -> Facts:
    base: dict[str, object] = {
        "loss_type": "collision",
        "description": "Rear-ended at a junction.",
        "incident_date": date(2026, 10, 8),
        "documents_present": frozenset({"driving_licence"}),
        "fields": {},
    }
    base.update(kw)
    return Facts(**base)  # type: ignore[arg-type]


def test_coverage_cites_the_clause_and_never_guesses_other() -> None:
    cov = [CoverageTerms("own_damage", ("collision", "fire"), "2.1")]
    assert [(o.status, o.clause_ref) for o in evaluate_coverages(cov, "collision")] == [("covered", "2.1")]
    assert evaluate_coverages(cov, "flood")[0].status == "not_covered"
    assert evaluate_coverages(cov, "other")[0].status == "requires_manual_interpretation"


def test_exclusion_predicates_only_trigger_on_hard_facts() -> None:
    excl = [
        ExclusionTerms("unlicensed_driver", ("own_damage",), "", "4.2"),
        ExclusionTerms("wear_and_tear", ("own_damage",), "", "4.5"),
        ExclusionTerms("racing_abroad", ("own_damage",), "", "4.9"),
        ExclusionTerms("flood_only", ("flood_cover",), "", "5.1"),
    ]
    out = {
        e.exclusion_code: e.status
        for e in evaluate_exclusions(excl, {"own_damage"}, facts(fields={"licence_expiry": "2026-02-01"}))
    }
    assert out == {
        "unlicensed_driver": "triggered",
        "wear_and_tear": "not_triggered",
        "racing_abroad": "unevaluated",
    }
    worn = facts(description="Gradual rust on the sill", documents_present=frozenset())
    out = {e.exclusion_code: e.status for e in evaluate_exclusions(excl, {"own_damage"}, worn)}
    assert (
        out["unlicensed_driver"] == "needs_interpretation" and out["wear_and_tear"] == "needs_interpretation"
    )


# --- calculators ------------------------------------------------------------------------------


def items(*pairs: tuple[str, str]) -> list[LineItem]:
    return [LineItem(i + 1, c, c, D(a)) for i, (c, a) in enumerate(pairs)]


def test_motor_spec_example_and_trace() -> None:
    calc = motor_own_damage(
        currency="GBP",
        items=items(("labour", "5000.00")),
        vehicle_age_years=3,
        salvage=D(0),
        limit_remaining=D("10000.00"),
        deductible=D("500.00"),
    )
    assert calc.status == "calculated" and calc.eligible_payout == D("4500.00")
    assert [s.op for s in calc.trace][-3:] == ["cap_at_limit", "subtract_deductible", "round_once"]


def test_motor_depreciation_bands_and_missing_inputs() -> None:
    assert [motor_depreciation_pct(a) for a in (0, 1, 2, 3, 5, 7, 10, 20)] == [
        D(x) for x in (0, 5, 10, 15, 25, 35, 50, 50)
    ]
    calc = motor_own_damage(
        currency="GBP",
        items=items(("parts", "100.00")),
        vehicle_age_years=None,
        salvage=D(0),
        limit_remaining=D("100"),
        deductible=D(0),
    )
    assert calc.status == "insufficient_inputs" and any("vehicle age" in m for m in calc.missing)
    assert (
        motor_own_damage(
            currency="XXX",
            items=items(("labour", "1")),
            vehicle_age_years=0,
            salvage=D(0),
            limit_remaining=D(1),
            deductible=D(0),
        ).status
        == "insufficient_inputs"
    )


def test_health_order_of_operations_and_ineligible_lines() -> None:
    li = [
        *items(("room", "2250.00"), ("procedure", "4100.00")),
        LineItem(3, "spa", "other", D("999.00"), "ineligible", "NOT_MEDICAL"),
    ]
    calc = health_medical(
        currency="USD",
        items=li,
        sub_limits={"room": D("1500.00")},
        aggregate_remaining=D("5000.00"),
        co_pay_pct=D("10"),
        deductible=D("250.00"),
    )
    # min(1500+4100, 5000)=5000 -> co-pay 10% -> 4500 -> deductible 250 -> 4250
    assert calc.eligible_payout == D("4250.00") and calc.gross_claimed == D("7349.00")
    assert [a.eligible for a in calc.items] == [D("1500.00"), D("4100.00"), D(0)]


# --- fraud scoring ----------------------------------------------------------------------------


def test_fraud_score_is_reproducible_and_model_contribution_is_capped() -> None:
    inds = [
        Indicator("F-DUP-CLAIM", "dup", EV),
        Indicator("F-NARRATIVE", "a", EV, D("1")),
        Indicator("F-NARRATIVE", "b", EV, D("1")),
    ]
    s = score(inds)
    assert s.score == 45 and s.level == "medium" and s.recommended_action == "review"  # 30 + min(15, 30)
    assert score(inds).score == s.score
    assert [level_for(x) for x in (24, 25, 49, 50, 74, 75, 100)] == [
        "low",
        "medium",
        "medium",
        "high",
        "high",
        "critical",
        "critical",
    ]
    with pytest.raises(ValueError, match="evidence"):
        score([Indicator("F-FREQ", "no evidence", ())])
    assert (
        score([Indicator(c, c, EV) for c in ("F-DUP-CLAIM", "F-DUP-DOC", "F-DUP-INV", "F-ID-MISMATCH")]).score
        == 100
    )


# --- decision matrix --------------------------------------------------------------------------

CLEAN = DecisionInputs(
    (),
    True,
    "calculated",
    (),
    False,
    {"own_damage": "covered"},
    "own_damage",
    (),
    (),
    None,
    "low",
    (),
    True,
    "815.00",
)


def test_matrix_rows_in_order() -> None:
    assert evaluate(CLEAN).outcome is Recommendation.APPROVE
    assert (
        evaluate(replace(CLEAN, missing_required_documents=("claim_form",), fraud_level="critical")).outcome
        is Recommendation.MORE_INFO
    )
    assert (
        evaluate(replace(CLEAN, av_scan_pending=True, fraud_level="high")).outcome
        is Recommendation.PENDING_VERIFICATION
    )
    assert (
        evaluate(replace(CLEAN, fraud_level="high", policy_active=False)).outcome
        is Recommendation.MANUAL_REVIEW
    )
    reject = evaluate(
        replace(
            CLEAN, policy_active=False, policy_version_resolved=False, assessment_status=None, payout=None
        )
    )
    assert (
        reject.outcome is Recommendation.REJECT
        and "always human-reviewed" in reject.mandatory_review_reasons[0]
    )
    assert evaluate(replace(CLEAN, fraud_level="medium")).outcome is Recommendation.MANUAL_REVIEW


@pytest.mark.parametrize("outcome", list(Recommendation))
@pytest.mark.parametrize("escalate", [True, False])
@pytest.mark.parametrize("evidence", [0, 2])
def test_escalation_is_monotonic(outcome: Recommendation, escalate: bool, evidence: int) -> None:
    final, escalated = apply_escalation(outcome, escalate, evidence)
    if escalate and evidence and outcome in (Recommendation.APPROVE, Recommendation.REJECT):
        assert final is Recommendation.MANUAL_REVIEW and escalated
    else:
        assert final is outcome and not escalated
