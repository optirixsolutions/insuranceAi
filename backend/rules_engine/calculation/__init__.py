"""Product calculators. All arithmetic is Decimal; rounding happens once, at the end (ADR-008).

Order of operations (rule set v1, docs/architecture/03-agent-contracts.md §5):

* Motor own damage: eligible items -> depreciation on parts by vehicle age band -> sum -> minus
  salvage -> cap at sum insured remaining -> minus deductible (floor 0) -> round once.
* Health: eligible items capped by per-category sub-limits -> sum -> cap at annual aggregate
  remaining -> co-pay % -> deductible (floor 0) -> round once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from rules_engine.money import Money, MoneyError

RULE_SET_VERSION = "rules_v1"
ZERO = Decimal("0")
HUNDRED = Decimal("100")

ItemStatus = Literal["eligible", "partially_eligible", "ineligible", "unsupported", "duplicate"]

# Parts depreciation by completed vehicle age in years (first band whose floor <= age applies).
MOTOR_PARTS_DEPRECIATION: tuple[tuple[int, Decimal], ...] = (
    (10, Decimal("50")),
    (7, Decimal("35")),
    (5, Decimal("25")),
    (3, Decimal("15")),
    (2, Decimal("10")),
    (1, Decimal("5")),
    (0, Decimal("0")),
)


@dataclass(frozen=True, slots=True)
class LineItem:
    line_no: int
    description: str
    # motor: parts|labour|paint|other; health: room|consultation|procedure|medication|diagnostics|other
    category: str
    claimed: Decimal
    status: ItemStatus = "eligible"
    reason_code: str | None = None
    coverage_code: str | None = None
    evidence_field_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CalcStep:
    op: str
    inputs: dict[str, str]
    output: str
    rule_ref: str

    def as_dict(self) -> dict[str, object]:
        return {"op": self.op, "inputs": self.inputs, "output": self.output, "rule_ref": self.rule_ref}


@dataclass(frozen=True, slots=True)
class AssessedItem:
    item: LineItem
    eligible: Decimal
    depreciation_pct: Decimal


@dataclass(slots=True)
class Calculation:
    currency: str
    status: Literal["calculated", "insufficient_inputs"]
    items: list[AssessedItem] = field(default_factory=list)
    gross_claimed: Decimal = ZERO
    gross_eligible: Decimal = ZERO
    depreciation_total: Decimal = ZERO
    salvage: Decimal = ZERO
    limit_remaining_before: Decimal = ZERO
    limit_capped_amount: Decimal = ZERO
    co_pay_applied: Decimal = ZERO
    deductible_applied: Decimal = ZERO
    eligible_payout: Decimal = ZERO
    trace: list[CalcStep] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def _s(value: Decimal | None) -> str:
    if value is None:
        return "none"
    return "0" if value == ZERO else format(value.normalize(), "f")


def _payable(items: list[LineItem]) -> list[LineItem]:
    return [i for i in items if i.status in ("eligible", "partially_eligible")]


def motor_depreciation_pct(
    vehicle_age_years: int, table: tuple[tuple[int, Decimal], ...] = MOTOR_PARTS_DEPRECIATION
) -> Decimal:
    for floor, pct in table:
        if vehicle_age_years >= floor:
            return pct
    return ZERO


def _check(currency: str, items: list[LineItem], limit_remaining: Decimal | None) -> list[str]:
    missing = []
    try:
        Money(ZERO, currency)
    except MoneyError:
        missing.append(f"supported currency (got {currency})")
    if not _payable(items):
        missing.append("eligible line items (estimate or invoice)")
    if limit_remaining is None:
        missing.append("limit remaining")
    if any(i.claimed < 0 for i in items):
        missing.append("non-negative line amounts")
    return missing


def _step(
    calc: Calculation, op: str, inputs: dict[str, Decimal | str | None], output: Decimal, ref: str
) -> None:
    calc.trace.append(
        CalcStep(op, {k: v if isinstance(v, str) else _s(v) for k, v in inputs.items()}, _s(output), ref)
    )


def _finish(calc: Calculation, amount: Decimal, deductible: Decimal) -> Calculation:
    after = max(ZERO, amount - deductible)
    calc.deductible_applied = amount - after
    _step(
        calc,
        "subtract_deductible",
        {"amount": amount, "deductible": deductible},
        after,
        "§5 deductible, floor 0",
    )
    rounded = Money(after, calc.currency).rounded().amount
    _step(calc, "round_once", {"amount": after}, rounded, "ADR-008 round half-even, once")
    calc.eligible_payout = rounded
    return calc


def motor_own_damage(
    *,
    currency: str,
    items: list[LineItem],
    vehicle_age_years: int | None,
    salvage: Decimal,
    limit_remaining: Decimal | None,
    deductible: Decimal,
    depreciation: tuple[tuple[int, Decimal], ...] = MOTOR_PARTS_DEPRECIATION,
    rule_ref: str = RULE_SET_VERSION,
) -> Calculation:
    calc = Calculation(currency, "calculated")
    calc.missing = _check(currency, items, limit_remaining)
    if vehicle_age_years is None and any(i.category == "parts" for i in _payable(items)):
        calc.missing.append("vehicle age (for parts depreciation)")
    if calc.missing or limit_remaining is None:
        calc.status = "insufficient_inputs"
        return calc
    calc.gross_claimed = sum((i.claimed for i in items), ZERO)
    pct = motor_depreciation_pct(vehicle_age_years or 0, depreciation)
    for item in items:
        if item.status not in ("eligible", "partially_eligible"):
            calc.items.append(AssessedItem(item, ZERO, ZERO))
            continue
        dep = pct if item.category == "parts" else ZERO
        eligible = item.claimed - item.claimed * dep / HUNDRED
        calc.depreciation_total += item.claimed - eligible
        calc.items.append(AssessedItem(item, eligible, dep))
        _step(
            calc,
            "line_depreciation",
            {"line": str(item.line_no), "claimed": item.claimed, "pct": dep, "category": item.category},
            eligible,
            f"{rule_ref} motor parts depreciation (vehicle age {vehicle_age_years})",
        )
    total = sum((a.eligible for a in calc.items), ZERO)
    calc.gross_eligible = total
    _step(calc, "sum_eligible", {"lines": str(len(calc.items))}, total, "§5 sum")
    after_salvage = max(ZERO, total - salvage)
    calc.salvage = total - after_salvage
    _step(calc, "subtract_salvage", {"amount": total, "salvage": salvage}, after_salvage, "§5 salvage")
    calc.limit_remaining_before = limit_remaining
    capped = min(after_salvage, limit_remaining)
    calc.limit_capped_amount = after_salvage - capped
    _step(
        calc,
        "cap_at_limit",
        {"amount": after_salvage, "limit_remaining": limit_remaining},
        capped,
        "§5 sum insured remaining",
    )
    return _finish(calc, capped, deductible)


def health_medical(
    *,
    currency: str,
    items: list[LineItem],
    sub_limits: dict[str, Decimal],
    aggregate_remaining: Decimal | None,
    co_pay_pct: Decimal,
    deductible: Decimal,
) -> Calculation:
    calc = Calculation(currency, "calculated")
    calc.missing = _check(currency, items, aggregate_remaining)
    if calc.missing or aggregate_remaining is None:
        calc.status = "insufficient_inputs"
        return calc
    calc.gross_claimed = sum((i.claimed for i in items), ZERO)
    used: dict[str, Decimal] = {}
    for item in items:
        if item.status not in ("eligible", "partially_eligible"):
            calc.items.append(AssessedItem(item, ZERO, ZERO))
            continue
        eligible = item.claimed
        limit = sub_limits.get(item.category)
        if limit is not None:
            room = max(ZERO, limit - used.get(item.category, ZERO))
            eligible = min(eligible, room)
            used[item.category] = used.get(item.category, ZERO) + eligible
        calc.items.append(AssessedItem(item, eligible, ZERO))
        _step(
            calc,
            "line_sub_limit",
            {
                "line": str(item.line_no),
                "claimed": item.claimed,
                "category": item.category,
                "sub_limit": limit,
            },
            eligible,
            f"{RULE_SET_VERSION} health per-category sub-limit",
        )
    total = sum((a.eligible for a in calc.items), ZERO)
    calc.gross_eligible = total
    _step(calc, "sum_eligible", {"lines": str(len(calc.items))}, total, "§5 sum")
    calc.limit_remaining_before = aggregate_remaining
    capped = min(total, aggregate_remaining)
    calc.limit_capped_amount = total - capped
    _step(
        calc,
        "cap_at_aggregate",
        {"amount": total, "aggregate_remaining": aggregate_remaining},
        capped,
        "§5 annual aggregate remaining",
    )
    co_pay = capped * co_pay_pct / HUNDRED
    calc.co_pay_applied = co_pay
    _step(calc, "apply_co_pay", {"amount": capped, "pct": co_pay_pct}, capped - co_pay, "§5 co-pay")
    return _finish(calc, capped - co_pay, deductible)
