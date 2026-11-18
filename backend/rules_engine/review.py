"""Human-review decision rules (Phase 9): authority, segregation of duties, four-eyes, overrides.

Pure functions; the review service calls them at decision time with persisted facts and raises
``AUTHORITY_EXCEEDED`` / business-rule errors with the returned reasons. Nothing here approves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

Action = Literal["approve", "reject", "request_information", "escalate", "return"]
# The recommendation each action agrees with; any other combination is an override.
AGREES: dict[str, frozenset[str]] = {
    "approve": frozenset({"APPROVE_RECOMMENDED"}),
    "reject": frozenset({"REJECT_RECOMMENDED"}),
    "request_information": frozenset({"MORE_INFORMATION_REQUIRED", "PENDING_VERIFICATION"}),
    "escalate": frozenset({"MANUAL_REVIEW_REQUIRED"}),
    "return": frozenset({"MANUAL_REVIEW_REQUIRED"}),
}


@dataclass(frozen=True, slots=True)
class Authority:
    max_approval_amount: Decimal
    currency: str
    product_id: str | None  # None = every product
    can_reject: bool
    can_override_fraud: bool
    valid_from: date
    valid_to: date | None


@dataclass(frozen=True, slots=True)
class DecisionContext:
    action: Action
    decider_id: str
    amount: Decimal | None
    currency: str
    product_id: str
    recommendation: str | None
    fraud_level: str | None
    assigned_adjuster_ids: frozenset[str]
    four_eyes_threshold: Decimal | None
    today: date
    calculated_amount: Decimal | None = None  # the assessment's eligible payout


@dataclass(slots=True)
class Ruling:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    override: bool = False
    four_eyes_required: bool = False


def is_override(action: str, recommendation: str | None) -> bool:
    return recommendation is not None and recommendation not in AGREES.get(action, frozenset())


def applicable(authorities: list[Authority], ctx: DecisionContext) -> list[Authority]:
    return [
        a
        for a in authorities
        if a.valid_from <= ctx.today
        and (a.valid_to is None or ctx.today <= a.valid_to)
        and (a.product_id is None or a.product_id == ctx.product_id)
        and a.currency == ctx.currency
    ]


def rule(ctx: DecisionContext, authorities: list[Authority], *, override_reason: str | None) -> Ruling:
    """Can this person take this action on this claim, and does it need a second approver?"""
    amount_changed = (
        ctx.action == "approve"
        and ctx.calculated_amount is not None
        and ctx.amount is not None
        and ctx.amount != ctx.calculated_amount
    )
    out = Ruling(allowed=True, override=is_override(ctx.action, ctx.recommendation) or amount_changed)
    if out.override and not (override_reason or "").strip():
        out.allowed = False
        out.reasons.append(
            f"approving {ctx.amount} instead of the calculated {ctx.calculated_amount}: "
            "an override reason is required"
            if amount_changed and not is_override(ctx.action, ctx.recommendation)
            else f"'{ctx.action}' differs from the recommendation {ctx.recommendation}: "
            "an override reason is required"
        )
    if ctx.action not in ("approve", "reject"):
        return out
    usable = applicable(authorities, ctx)
    if not usable:
        out.allowed = False
        out.reasons.append(f"no decision authority for this product in {ctx.currency}")
        return out
    if ctx.decider_id in ctx.assigned_adjuster_ids:
        out.allowed = False
        out.reasons.append("the adjuster assigned to this claim cannot make its final decision")
    high_risk = ctx.fraud_level in ("high", "critical")
    if ctx.action == "approve":
        if ctx.amount is None or ctx.amount < 0:
            out.allowed = False
            out.reasons.append("an approval needs a non-negative approved amount")
        elif not any(ctx.amount <= a.max_approval_amount for a in usable):
            out.allowed = False
            out.reasons.append(f"{ctx.amount} {ctx.currency} exceeds your approval authority")
        if high_risk and not any(a.can_override_fraud for a in usable):
            out.allowed = False
            out.reasons.append(
                f"approving a claim with {ctx.fraud_level} fraud risk needs fraud-override authority"
            )
        above = (
            ctx.four_eyes_threshold is not None
            and ctx.amount is not None
            and ctx.amount > ctx.four_eyes_threshold
        )
        out.four_eyes_required = above or out.override or high_risk
    else:  # reject
        if not any(a.can_reject for a in usable):
            out.allowed = False
            out.reasons.append("you do not have authority to reject claims")
        out.four_eyes_required = out.override  # rejecting against the recommendation needs a second person
    return out


def second_approval(first_decider: str, second_decider: str, ruling_for_second: Ruling) -> list[str]:
    """Reasons a second approval is refused (empty = accepted)."""
    reasons = []
    if first_decider == second_decider:
        reasons.append("the second approver must be a different person")
    reasons += ruling_for_second.reasons
    return reasons
