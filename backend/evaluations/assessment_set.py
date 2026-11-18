"""Assessment evaluation: 80 calculation cases + seeded property checks (03 §7: 100% exact Decimal).

15 hand-written calculator cases, 45 motor and 20 health cases run end to end (synthetic estimate
or bill -> real PDF text extraction -> policy agent -> assessment agent), each compared with an
independently written reference formula. Property checks run 500 random calculator inputs.
"""

from __future__ import annotations

import random
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from evaluations.builders import (
    Case,
    document,
    health_claim_form,
    health_version,
    hospital_bill,
    motor_claim_form,
    motor_estimate,
    motor_version,
)
from evaluations.harness import EvalResult, LlmSource, Metric, ratio, run_chain
from rules_engine.calculation import LineItem, health_medical, motor_own_damage
from rules_engine.money import Money, MoneyError

D = Decimal
CENT = D("0.01")


def ref_motor_dep(age: int) -> D:  # restated independently from the rule-set table
    return D(
        50
        if age >= 10
        else 35
        if age >= 7
        else 25
        if age >= 5
        else 15
        if age >= 3
        else 10
        if age >= 2
        else 5
        if age >= 1
        else 0
    )


def ref_motor(parts: D, labour: D, paint: D, age: int, limit: D, deductible: D) -> D:
    eligible = parts * (1 - ref_motor_dep(age) / 100) + labour + paint
    return max(D(0), min(eligible, limit) - deductible).quantize(CENT, ROUND_HALF_EVEN)


def ref_health(items: dict[str, D], sub: dict[str, D], aggregate: D, co_pay: D, deductible: D) -> D:
    total = sum((min(v, sub[k]) if k in sub else v for k, v in items.items()), D(0))
    after = min(total, aggregate) * (1 - co_pay / 100)
    return max(D(0), after - deductible).quantize(CENT, ROUND_HALF_EVEN)


def literal_cases() -> list[tuple[str, D, D]]:
    """(label, computed, expected): hand-worked examples, including the spec example
    5,000 / 500 / 10,000 -> 4,500."""

    def m(items: list[tuple[str, str]], age: int | None, limit: str, ded: str, salvage: str = "0") -> D:
        li = [LineItem(i + 1, c, c, D(a)) for i, (c, a) in enumerate(items)]
        return motor_own_damage(
            currency="GBP",
            items=li,
            vehicle_age_years=age,
            salvage=D(salvage),
            limit_remaining=D(limit),
            deductible=D(ded),
        ).eligible_payout

    def h(items: list[tuple[str, str]], sub: dict[str, str], agg: str, co: str, ded: str) -> D:
        li = [LineItem(i + 1, c, c, D(a)) for i, (c, a) in enumerate(items)]
        return health_medical(
            currency="USD",
            items=li,
            sub_limits={k: D(v) for k, v in sub.items()},
            aggregate_remaining=D(agg),
            co_pay_pct=D(co),
            deductible=D(ded),
        ).eligible_payout

    return [
        ("spec example", m([("labour", "5000.00")], 0, "10000.00", "500.00"), D("4500.00")),
        ("deductible floors at zero", m([("labour", "300.00")], 0, "10000.00", "500.00"), D("0.00")),
        (
            "cap at limit before deductible",
            m([("labour", "12000.00")], 0, "10000.00", "500.00"),
            D("9500.00"),
        ),
        (
            "parts 25% at age 5",
            m([("parts", "640.00"), ("labour", "455.00"), ("paint", "380.00")], 5, "18000.00", "500.00"),
            D("815.00"),
        ),
        ("parts 50% at age 12", m([("parts", "1000.00")], 12, "18000.00", "0.00"), D("500.00")),
        ("age 0 no depreciation", m([("parts", "999.99")], 0, "18000.00", "0.00"), D("999.99")),
        ("salvage subtracted", m([("labour", "2000.00")], 0, "18000.00", "100.00", "250.00"), D("1650.00")),
        (
            "half-even rounding down",
            m([("parts", "0.10")], 1, "100.00", "0.00"),
            D("0.10"),
        ),  # 0.095 -> 0.10 (half-even to even 0.10)
        ("half-even rounding", m([("parts", "0.30")], 1, "100.00", "0.00"), D("0.28")),  # 0.285 -> 0.28
        ("limit exhausted", m([("labour", "800.00")], 0, "0.00", "0.00"), D("0.00")),
        (
            "health spec order",
            h(
                [("room", "2250.00"), ("procedure", "4100.00"), ("medication", "310.50")],
                {},
                "50000.00",
                "10",
                "250.00",
            ),
            D("5744.45"),
        ),
        (
            "health sub-limit",
            h([("room", "2250.00"), ("procedure", "4100.00")], {"room": "1500.00"}, "50000.00", "0", "0.00"),
            D("5600.00"),
        ),
        ("health aggregate cap", h([("procedure", "9000.00")], {}, "5000.00", "20", "100.00"), D("3900.00")),
        ("health deductible floor", h([("medication", "80.00")], {}, "5000.00", "0", "250.00"), D("0.00")),
        (
            "health co-pay rounding",
            h([("consultation", "333.33")], {}, "5000.00", "15", "0.00"),
            D("283.33"),
        ),  # 283.3305
    ]


def motor_cases(rng: random.Random) -> list[tuple[Case, D]]:
    out = []
    for i in range(45):
        parts, labour, paint = (D(rng.randrange(5000, 400000)) / 100 for _ in range(3))
        year = rng.choice([2026, 2025, 2024, 2023, 2021, 2019, 2016, 2012])
        sum_insured = D(rng.choice([2000, 5000, 12000, 25000]))
        usage = D(rng.choice([0, 0, 0, 1500, 4000]))
        deductible = D(rng.choice(["0.00", "250.00", "500.00", "1000.00"]))
        key = f"am{i}"
        version = motor_version(
            key,
            date(2026, 1, 1),
            sum_insured=f"{sum_insured:.2f}",
            deductible=f"{deductible:.2f}",
            vehicle_year=None,
        )
        est = motor_estimate(parts=f"{parts:.2f}", labour=f"{labour:.2f}", paint=f"{paint:.2f}")
        est = [line.replace("Year of manufacture: 2021", f"Year of manufacture: {year}") for line in est]
        total = parts + labour + paint
        case = Case(
            key,
            amount=f"{total:.2f}",
            versions=[version],
            usage=f"{usage:.2f}",
            documents=[
                document(f"{key}-e", "estimate.pdf", est, mode="pdf"),
                document(f"{key}-f", "form.pdf", motor_claim_form(), mode="pdf"),
            ],
        )
        out.append(
            (case, ref_motor(parts, labour, paint, 2026 - year, max(D(0), sum_insured - usage), deductible))
        )
    return out


def health_cases(rng: random.Random) -> list[tuple[Case, D]]:
    out = []
    for i in range(20):
        ward, proc, pharm = (D(rng.randrange(10000, 900000)) / 100 for _ in range(3))
        aggregate = D(rng.choice([5000, 20000, 50000]))
        usage = D(rng.choice([0, 0, 2500]))
        co_pay = D(rng.choice(["0.00", "10.00", "20.00"]))
        deductible = D(rng.choice(["0.00", "250.00", "500.00"]))
        sub = {"room": D(rng.choice([1000, 3000]))} if i % 2 else {}
        key = f"ah{i}"
        version = health_version(
            key,
            date(2026, 1, 1),
            aggregate=f"{aggregate:.2f}",
            deductible=f"{deductible:.2f}",
            co_pay=f"{co_pay:.2f}",
            sub_limits={k: f"{v:.2f}" for k, v in sub.items()},
        )
        bill = hospital_bill(ward=f"{ward:,.2f}", procedure=f"{proc:,.2f}", pharmacy=f"{pharm:,.2f}")
        case = Case(
            key,
            line="health",
            incident=date(2026, 9, 14),
            amount=f"{ward + proc + pharm:.2f}",
            holder="Amelia Hartley",
            description="Admitted with acute appendicitis and operated the same day.",
            versions=[version],
            usage=f"{usage:.2f}",
            documents=[
                document(f"{key}-b", "bill.pdf", bill, mode="pdf"),
                document(f"{key}-f", "form.pdf", health_claim_form(), mode="pdf"),
            ],
        )
        out.append(
            (
                case,
                ref_health(
                    {"room": ward, "procedure": proc, "medication": pharm},
                    sub,
                    max(D(0), aggregate - usage),
                    co_pay,
                    deductible,
                ),
            )
        )
    return out


def property_checks(rng: random.Random, n: int = 500) -> list[str]:
    problems = []
    for k in range(n):
        items = [
            LineItem(i + 1, c, c, D(rng.randrange(0, 500000)) / 100)
            for i, c in enumerate(rng.choices(["parts", "labour", "paint"], k=rng.randint(1, 6)))
        ]
        limit, ded = D(rng.randrange(0, 3000000)) / 100, D(rng.randrange(0, 200000)) / 100
        age = rng.randint(0, 15)
        c = motor_own_damage(
            currency="GBP",
            items=items,
            vehicle_age_years=age,
            salvage=D(0),
            limit_remaining=limit,
            deductible=ded,
        )
        more = motor_own_damage(
            currency="GBP",
            items=items,
            vehicle_age_years=age,
            salvage=D(0),
            limit_remaining=limit,
            deductible=ded + 1,
        )
        p = c.eligible_payout
        if not (D(0) <= p <= limit.quantize(CENT, ROUND_HALF_EVEN) or (limit == 0 and p == 0)):
            problems.append(f"#{k}: payout {p} outside [0, limit {limit}]")
        if p > c.gross_eligible:
            problems.append(f"#{k}: payout {p} above eligible {c.gross_eligible}")
        if more.eligible_payout > p:
            problems.append(f"#{k}: higher deductible increased payout")
        if p.as_tuple().exponent != -2:
            problems.append(f"#{k}: payout {p} not rounded to cents once")
        if c.trace[-1].op != "round_once" or sum(1 for s in c.trace if s.op == "round_once") != 1:
            problems.append(f"#{k}: rounding not applied exactly once at the end")
    try:
        Money(D(1), "GBP") + Money(D(1), "USD")
        problems.append("cross-currency arithmetic did not raise")
    except MoneyError:
        pass
    return problems


async def evaluate(llm: LlmSource = None) -> EvalResult:
    rng = random.Random(20261105)
    literal = literal_cases()
    e2e = motor_cases(rng) + health_cases(rng)
    res = EvalResult("assessment", len(literal) + len(e2e), mode="live" if llm else "deterministic")
    exact = 0
    for label, computed, want in literal:
        exact += computed == want
        if computed != want:
            res.failures.append(f"literal '{label}': {computed} != {want}")
    for case, want in e2e:
        out = await run_chain(case, ("policy", "assessment"), llm)
        data = out["assessment"].data or {}
        got: D | None = D(str(data["eligible_payout"])) if data.get("status") == "calculated" else None
        exact += got == want
        if got != want:
            res.failures.append(f"{case.key}: payout {got} != reference {want} (status {data.get('status')})")
    props = property_checks(rng)
    res.failures += props[:20]
    res.metrics = [
        Metric("exact_decimal_match", ratio(exact, res.cases), 1.0),
        Metric("property_violations", len(props), 0, higher_is_better=False),
    ]
    res.notes.append(
        "End-to-end cases parse line items from real PDF text layers; 500 seeded property inputs."
    )
    return res
