"""Policy verification evaluation: 40 scenarios (expired, waiting period, exclusions, versions).

Gates (03 §7): 100% correct version selection, 100% valid clause citations, 0 "covered" without
a clause. Coverage/exclusion status accuracy is reported alongside.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from evaluations.builders import Case, document, driving_licence, health_version, motor_version
from evaluations.harness import EvalResult, LlmSource, Metric, ratio, run_one


@dataclass
class PolicyScenario:
    case: Case
    expected_version: int | None  # version_no, None = no version in force
    expected_primary: str | None  # expected status of the first coverage
    expected_exclusions: dict[str, str]  # code -> triggered | needs_interpretation | not_triggered
    label: str


def scenarios() -> list[PolicyScenario]:
    out: list[PolicyScenario] = []
    lic = [document("lic-ok", "licence.pdf", driving_licence())]
    clean = {"unlicensed_driver": "not_triggered", "wear_and_tear": "not_triggered"}
    # A. In force, covered peril (10).
    for i, (peril, day) in enumerate(
        [
            ("collision", 8),
            ("fire", 20),
            ("theft", 3),
            ("weather", 15),
            ("vandalism", 1),
            ("glass", 28),
            ("collision", 31),
            ("theft", 12),
            ("fire", 5),
            ("collision", 18),
        ]
    ):
        inc = date(2026, (i % 9) + 2, min(day, 28))
        out.append(
            PolicyScenario(
                Case(f"pa{i}", category=peril, incident=inc, documents=lic),
                1,
                "covered",
                clean,
                f"in force {peril}",
            )
        )
    # B. Expired / not yet started (8).
    for i, inc in enumerate(
        [date(2027, 1, 2), date(2027, 3, 1), date(2027, 6, 30), date(2028, 1, 1), date(2027, 1, 1)]
    ):
        out.append(
            PolicyScenario(
                Case(f"pb{i}", incident=inc, reported=min(inc, date(2026, 11, 4)), documents=lic),
                None,
                None,
                {},
                "expired",
            )
        )
    for i, inc in enumerate([date(2025, 12, 31), date(2025, 6, 1), date(2025, 11, 30)]):
        out.append(
            PolicyScenario(Case(f"pc{i}", incident=inc, documents=lic), None, None, {}, "before inception")
        )

    # C. Multiple versions: renewal, endorsement re-issue, superseded, draft (10).
    def renewal(key: str) -> list:  # type: ignore[type-arg]
        return [
            motor_version(key, date(2025, 1, 1), no=1, status="superseded"),
            motor_version(key, date(2026, 1, 1), no=2),
        ]

    for i, (inc, want) in enumerate(
        [(date(2025, 7, 1), 1), (date(2025, 12, 31), 1), (date(2026, 1, 1), 2), (date(2026, 9, 9), 2)]
    ):
        out.append(
            PolicyScenario(
                Case(f"pd{i}", incident=inc, versions=renewal(f"pd{i}"), documents=lic),
                want,
                "covered",
                clean,
                "renewal",
            )
        )
    for i, inc in enumerate([date(2026, 3, 3), date(2026, 8, 8), date(2026, 12, 1)]):
        k = f"pe{i}"
        vs = [
            motor_version(k, date(2026, 1, 1), no=1, status="superseded"),
            motor_version(k, date(2026, 1, 1), no=2, status="superseded"),
            motor_version(k, date(2026, 1, 1), no=3),
        ]
        out.append(
            PolicyScenario(
                Case(
                    k,
                    incident=inc,
                    versions=vs,
                    documents=lic,
                    reported=min(inc + timedelta(days=1), date(2026, 11, 4)),
                ),
                3 if inc <= date(2026, 12, 31) else None,
                "covered",
                clean,
                "endorsement re-issue",
            )
        )
    for i, inc in enumerate([date(2026, 4, 4), date(2026, 5, 5)]):
        k = f"pf{i}"
        vs = [motor_version(k, date(2026, 1, 1), no=1, status="draft")]
        out.append(
            PolicyScenario(Case(k, incident=inc, versions=vs, documents=lic), None, None, {}, "draft only")
        )
    k = "pf9"
    out.append(
        PolicyScenario(
            Case(
                k,
                incident=date(2026, 6, 6),
                documents=lic,
                versions=[
                    motor_version(k, date(2026, 1, 1), no=1),
                    motor_version(k, date(2026, 1, 1), no=2, status="draft"),
                ],
            ),
            1,
            "covered",
            clean,
            "draft ignored",
        )
    )
    # D. Waiting period (health, 30 days from first inception) (5).
    for i, (start, inc, renewed, ok) in enumerate(
        [
            (date(2026, 1, 1), date(2026, 1, 10), False, False),
            (date(2026, 1, 1), date(2026, 1, 30), False, False),
            (date(2026, 1, 1), date(2026, 1, 31), False, True),
            (date(2026, 1, 1), date(2026, 6, 1), False, True),
            (date(2025, 1, 1), date(2026, 1, 5), True, True),
        ]
    ):
        k = f"pg{i}"
        vs = (
            [
                health_version(k, date(2025, 1, 1), no=1, status="superseded"),
                health_version(k, date(2026, 1, 1), no=2),
            ]
            if renewed
            else [health_version(k, start)]
        )
        out.append(
            PolicyScenario(
                Case(
                    k,
                    line="health",
                    incident=inc,
                    versions=vs,
                    amount="6660.50",
                    holder="Amelia Hartley",
                    description="Admitted with acute appendicitis and operated the same day.",
                ),
                2 if renewed else 1,
                "covered" if ok else "not_covered",
                {"pre_existing": "not_triggered"},
                "waiting period",
            )
        )
    # E. Exclusions (7).
    worn = "The engine stopped because of gradual wear and rust on the timing belt."
    out += [
        PolicyScenario(
            Case(f"ph{i}", description=worn, documents=lic),
            1,
            "covered",
            {"unlicensed_driver": "not_triggered", "wear_and_tear": "needs_interpretation"},
            "wear and tear",
        )
        for i in range(2)
    ]
    out += [
        PolicyScenario(
            Case(f"pi{i}", documents=[document(f"lic-x{i}", "licence.pdf", driving_licence(expiry=exp))]),
            1,
            "covered",
            {"unlicensed_driver": "triggered", "wear_and_tear": "not_triggered"},
            "licence expired",
        )
        for i, exp in enumerate(["01 Feb 2026", "30 Sep 2026"])
    ]
    out += [
        PolicyScenario(
            Case(f"pj{i}"),
            1,
            "covered",
            {"unlicensed_driver": "needs_interpretation", "wear_and_tear": "not_triggered"},
            "no licence",
        )
        for i in range(2)
    ]
    out.append(
        PolicyScenario(
            Case(
                "pk0",
                line="health",
                incident=date(2026, 6, 1),
                amount="6660.50",
                holder="Amelia Hartley",
                description="Recurrence of a chronic gallbladder condition previously diagnosed in 2024.",
            ),
            1,
            "covered",
            {"pre_existing": "needs_interpretation"},
            "pre-existing",
        )
    )
    return out


async def evaluate(llm: LlmSource = None) -> EvalResult:
    items = scenarios()
    res = EvalResult("policy", len(items), mode="live" if llm else "deterministic")
    version_ok = cite_ok = cite_total = covered_uncited = status_ok = excl_ok = excl_total = 0
    for s in items:
        snap = s.case.snapshot()
        r = await run_one(s.case, "policy", llm)
        d = r.data or {}
        chosen = next(
            (v.version_no for v in snap.policy.versions if v.id == d.get("policy_version_id")), None
        )
        version_ok += chosen == s.expected_version
        if chosen != s.expected_version:
            res.failures.append(f"{s.case.key} ({s.label}): version {chosen}, expected {s.expected_version}")
        version = next((v for v in snap.policy.versions if v.id == d.get("policy_version_id")), None)
        clause_ids = {c.id for c in version.clauses} if version else set()
        for c in d.get("coverages", []):
            cite_total += 1
            valid = bool(c["clause_ids"]) and all(cid in clause_ids for cid in c["clause_ids"])
            cite_ok += valid
            covered_uncited += c["status"] == "covered" and not valid
        for group in ("exclusions_triggered", "exclusions_need_interpretation", "exclusions_not_triggered"):
            for e in d.get(group, []):
                cite_total += 1
                cite_ok += e["clause_id"] in clause_ids
        primary = d["coverages"][0]["status"] if d.get("coverages") else None
        status_ok += primary == s.expected_primary
        if primary != s.expected_primary:
            res.failures.append(
                f"{s.case.key} ({s.label}): coverage {primary}, expected {s.expected_primary}"
            )
        groups = {
            "exclusions_triggered": "triggered",
            "exclusions_need_interpretation": "needs_interpretation",
            "exclusions_not_triggered": "not_triggered",
        }
        got = {e["exclusion_code"]: status for g, status in groups.items() for e in d.get(g, [])}
        for code, want in s.expected_exclusions.items():
            excl_total += 1
            excl_ok += got.get(code) == want
            if got.get(code) != want:
                res.failures.append(
                    f"{s.case.key} ({s.label}): exclusion {code} {got.get(code)}, expected {want}"
                )
    res.metrics = [
        Metric("version_selection_accuracy", ratio(version_ok, len(items)), 1.0),
        Metric("clause_citations_valid", ratio(cite_ok, cite_total), 1.0),
        Metric("covered_without_valid_clause", covered_uncited, 0, higher_is_better=False),
        Metric("primary_coverage_status_accuracy", ratio(status_ok, len(items))),
        Metric("exclusion_status_accuracy", ratio(excl_ok, excl_total)),
    ]
    res.notes.append("Deterministic run (no model): unevaluated exclusions go to interpretation by design.")
    return res
