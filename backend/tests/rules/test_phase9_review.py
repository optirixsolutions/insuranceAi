"""Phase 9 review rules: authority, segregation of duties, four-eyes, override reasons, denied paths."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from rules_engine.review import Authority, DecisionContext, is_override, rule, second_approval

D = Decimal
TODAY = date(2026, 11, 7)
AUTH = Authority(
    D("10000"),
    "GBP",
    None,
    can_reject=True,
    can_override_fraud=False,
    valid_from=date(2026, 1, 1),
    valid_to=None,
)
CTX = DecisionContext(
    "approve",
    "u1",
    D("815.00"),
    "GBP",
    "p1",
    "APPROVE_RECOMMENDED",
    "low",
    frozenset({"adj"}),
    D("5000"),
    TODAY,
)


def test_agreeing_approval_within_authority_is_allowed_without_second_approver() -> None:
    r = rule(CTX, [AUTH], override_reason=None)
    assert r.allowed and not r.override and not r.four_eyes_required and r.reasons == []


@pytest.mark.parametrize(
    ("ctx", "auths", "reason"),
    [
        (CTX, [], "no decision authority"),
        (CTX, [replace(AUTH, currency="USD")], "no decision authority"),
        (CTX, [replace(AUTH, product_id="other")], "no decision authority"),
        (CTX, [replace(AUTH, valid_to=date(2026, 6, 30))], "no decision authority"),
        (CTX, [replace(AUTH, valid_from=date(2026, 12, 1))], "no decision authority"),
        (replace(CTX, amount=D("10000.01")), [AUTH], "exceeds your approval authority"),
        (replace(CTX, amount=None), [AUTH], "non-negative approved amount"),
        (replace(CTX, decider_id="adj"), [AUTH], "assigned to this claim"),
        (replace(CTX, fraud_level="high"), [AUTH], "fraud-override authority"),
        (
            replace(CTX, action="reject", recommendation="REJECT_RECOMMENDED"),
            [replace(AUTH, can_reject=False)],
            "authority to reject",
        ),
        (replace(CTX, recommendation="MANUAL_REVIEW_REQUIRED"), [AUTH], "override reason is required"),
    ],
)
def test_denied_paths(ctx: DecisionContext, auths: list[Authority], reason: str) -> None:
    r = rule(ctx, auths, override_reason=None)
    assert not r.allowed and any(reason in x for x in r.reasons), r.reasons


def test_four_eyes_triggers() -> None:
    assert rule(
        replace(CTX, amount=D("6000")), [AUTH], override_reason=None
    ).four_eyes_required  # above threshold
    over = rule(
        replace(CTX, recommendation="MANUAL_REVIEW_REQUIRED"), [AUTH], override_reason="photos confirm damage"
    )
    assert over.allowed and over.override and over.four_eyes_required  # override with reason
    fraud = rule(
        replace(CTX, fraud_level="critical"), [replace(AUTH, can_override_fraud=True)], override_reason=None
    )
    assert fraud.allowed and fraud.four_eyes_required  # high-risk approval
    assert not rule(replace(CTX, four_eyes_threshold=None), [AUTH], override_reason=None).four_eyes_required
    rej = rule(
        replace(CTX, action="reject", recommendation="APPROVE_RECOMMENDED"),
        [AUTH],
        override_reason="excl 4.5",
    )
    assert rej.allowed and rej.override and rej.four_eyes_required
    agree = rule(
        replace(CTX, action="reject", recommendation="REJECT_RECOMMENDED"), [AUTH], override_reason=None
    )
    assert agree.allowed and not agree.four_eyes_required


def test_non_decision_actions_need_only_an_override_reason_when_disagreeing() -> None:
    info = replace(CTX, action="request_information", recommendation="MORE_INFORMATION_REQUIRED")
    assert rule(info, [], override_reason=None).allowed  # no authority needed to ask for information
    assert not rule(replace(info, recommendation="APPROVE_RECOMMENDED"), [], override_reason=" ").allowed
    assert rule(
        replace(CTX, action="escalate", recommendation="APPROVE_RECOMMENDED"), [], override_reason="unsure"
    ).allowed
    assert is_override("return", "APPROVE_RECOMMENDED") and not is_override(
        "return", "MANUAL_REVIEW_REQUIRED"
    )
    assert not is_override("approve", None)


def test_second_approval_must_be_another_authorised_person() -> None:
    ok = rule(replace(CTX, decider_id="u2"), [AUTH], override_reason=None)
    assert second_approval("u1", "u2", ok) == []
    assert "different person" in second_approval("u1", "u1", ok)[0]
    weak = rule(replace(CTX, decider_id="u3", amount=D("20000")), [AUTH], override_reason=None)
    assert any("exceeds" in r for r in second_approval("u1", "u3", weak))


def test_changing_the_calculated_amount_is_an_override() -> None:
    ctx = replace(CTX, calculated_amount=D("815.00"))
    assert not rule(ctx, [AUTH], override_reason=None).override
    more = replace(ctx, amount=D("900.00"))
    denied = rule(more, [AUTH], override_reason=None)
    assert not denied.allowed and "instead of the calculated 815.00" in denied.reasons[0]
    ok = rule(more, [AUTH], override_reason="towing receipt accepted")
    assert ok.allowed and ok.override and ok.four_eyes_required
