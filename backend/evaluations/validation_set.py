"""Cross-document validation evaluation: 40 contradiction cases (03 §7: all critical detected).

Each case plants one contradiction with a labelled check code and severity; clean cases must
produce no critical finding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from evaluations.builders import (
    Case,
    document,
    driving_licence,
    health_claim_form,
    hospital_bill,
    motor_claim_form,
    motor_estimate,
)
from evaluations.harness import EvalResult, Metric, ratio, run_one


@dataclass
class ValidationScenario:
    case: Case
    expect_code: str | None
    expect_severity: str | None


def motor(
    key: str, form: dict[str, Any] | None = None, est: dict[str, Any] | None = None, **case: object
) -> Case:
    docs = [
        document(f"{key}-f", "claim_form.pdf", motor_claim_form(**(form or {}))),
        document(f"{key}-e", "estimate.pdf", motor_estimate(**(est or {}))),
    ]
    return Case(key, documents=docs, **case)  # type: ignore[arg-type]


def health(
    key: str, bill: dict[str, Any] | None = None, form: dict[str, Any] | None = None, **case: object
) -> Case:
    docs = [
        document(f"{key}-f", "claim_form.pdf", health_claim_form(**(form or {}))),
        document(f"{key}-b", "bill.pdf", hospital_bill(**(bill or {}))),
    ]
    defaults: dict[str, object] = {
        "line": "health",
        "incident": date(2026, 9, 14),
        "amount": "6660.50",
        "holder": "Amelia Hartley",
        "description": "Admitted with acute appendicitis and operated the same day.",
    }
    defaults.update(case)
    return Case(key, documents=docs, **defaults)  # type: ignore[arg-type]


def scenarios() -> list[ValidationScenario]:
    s: list[ValidationScenario] = []
    for i, loss in enumerate(["12 Oct 2026", "29 Sep 2026", "01 Nov 2026", "15 Oct 2026"]):
        s.append(ValidationScenario(motor(f"vd{i}", est={"loss": loss}), "V-INCIDENT-DATE", "critical"))
    for i, loss in enumerate(["14 Oct 2026", "02 Oct 2026"]):
        s.append(
            ValidationScenario(
                motor(f"vx{i}", form={"loss": loss}, est={"loss": "08 Oct 2026"}, incident=date(2026, 10, 8)),
                "V-DATE-ACROSS-DOCS",
                "critical",
            )
        )
    s.append(
        ValidationScenario(
            motor("vd9", est={"loss": "09 Oct 2026"}, form={"loss": "08 Oct 2026"}),
            "V-INCIDENT-DATE",
            "warning",
        )
    )
    for i, pol in enumerate(["NWM-M-99999", "XYZ-12345", "NWM-H-24000", "NWM-M-24011"]):
        s.append(ValidationScenario(motor(f"vp{i}", est={"policy": pol}), "V-POLICY-NUMBER", "critical"))
    for i, name in enumerate(["Marcus Bellweather", "Priya Natarajan", "Olu Adeyemi", "Chen Wei"]):
        s.append(ValidationScenario(motor(f"vn{i}", est={"holder": name}), "V-NAME", "critical"))
    for i, name in enumerate(["Rohan Tanakaa", "Rohen Tanaka"]):
        s.append(
            ValidationScenario(motor(f"vm{i}", est={"holder": name}), None, None)
        )  # OCR-level typo: matches (JW >= 0.92)
    for i, reg in enumerate(["LK21 ABD", "MA19 XYZ", "AB12 CDE"]):
        s.append(ValidationScenario(motor(f"vr{i}", est={"reg": reg}), "V-REGISTRATION", "critical"))
    for i, (adm, dis) in enumerate(
        [("2026-09-14", "2026-09-10"), ("2026-09-20", "2026-09-14"), ("2026-09-15", "2026-09-14")]
    ):
        s.append(
            ValidationScenario(
                health(f"vs{i}", bill={"admitted": adm, "discharged": dis}, incident=date.fromisoformat(adm)),
                "V-STAY-ORDER",
                "critical",
            )
        )
    for i, amt in enumerate(["2475.00", "900.00", "5000.00"]):
        s.append(ValidationScenario(motor(f"va{i}", amount=amt), "V-AMOUNT", "error"))
    s.append(ValidationScenario(motor("va9", amount="1490.00"), "V-AMOUNT", "warning"))
    for i, inc in enumerate([date(2026, 9, 20), date(2026, 9, 25)]):
        s.append(
            ValidationScenario(
                health(
                    f"vh{i}",
                    bill={"admitted": "2026-09-14", "discharged": "2026-09-17"},
                    incident=inc,
                    form={"incident": inc.strftime("%d %b %Y")},
                ),
                "V-STAY-ORDER",
                "warning",
            )
        )
    s.append(
        ValidationScenario(
            Case("vc0", documents=[document("vc0-f", "claim_form.pdf", motor_claim_form())]),
            "V-CALC-INPUTS",
            "warning",
        )
    )
    form_ref = [*motor_claim_form(), "Claim reference: CLM-2025-000123"]
    s.append(
        ValidationScenario(
            Case(
                "vref",
                documents=[
                    document("vref-f", "claim_form.pdf", form_ref),
                    document("vref-e", "estimate.pdf", motor_estimate()),
                ],
            ),
            "V-CLAIM-REFERENCE",
            "error",
        )
    )
    s.append(
        ValidationScenario(
            Case(
                "vdob",
                documents=[
                    document("vdob-l", "licence.pdf", driving_licence(dob="03 Jul 1979")),
                    document("vdob-e", "estimate.pdf", motor_estimate()),
                ],
            ),
            "V-DOB",
            "error",
        )
    )
    s.append(ValidationScenario(health("vnm", bill={"patient": "Amelia Hartly"}), None, None))
    for i in range(7):
        s.append(ValidationScenario(motor(f"vclean{i}") if i % 2 == 0 else health(f"vclean{i}"), None, None))
    return s


async def evaluate() -> EvalResult:
    items = scenarios()
    res = EvalResult("validation", len(items))
    crit_total = crit_hit = labelled = labelled_hit = clean_total = clean_critical = 0
    for sc in items:
        d = (await run_one(sc.case, "validation")).data or {}
        found = {(f["check_code"], f["severity"]) for f in d.get("findings", [])}
        if sc.expect_code:
            labelled += 1
            hit = (sc.expect_code, sc.expect_severity) in found
            labelled_hit += hit
            if sc.expect_severity == "critical":
                crit_total += 1
                crit_hit += hit
            if not hit:
                res.failures.append(
                    f"{sc.case.key}: expected {sc.expect_code}/{sc.expect_severity}, found {sorted(found)}"
                )
        else:
            clean_total += 1
            crit = [f for f in found if f[1] in ("critical", "error")]
            clean_critical += bool(crit)
            if crit:
                res.failures.append(f"{sc.case.key}: clean case raised {crit}")
    res.metrics = [
        Metric("critical_contradictions_detected", ratio(crit_hit, crit_total), 1.0),
        Metric("labelled_findings_detected", ratio(labelled_hit, labelled)),
        Metric("clean_cases_with_error_or_critical", clean_critical, 0, higher_is_better=False),
    ]
    return res
