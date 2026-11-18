"""Document intelligence evaluation: 60 synthetic documents, 13 types (03 §7).

Variants per type: text-layer PDF, clean scan, degraded scan (noise / blur / faded), rotated scan
and "handwritten-lite" (slight skew + blur). Scans go through Tesseract, so the full set runs in
the backend container; elsewhere only the PDF variants run and the report says so.
Gates: classification >= 95%; field exact match >= 90% on clean documents (degraded reported
separately); 0 ungrounded fields accepted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from document_processing.ocr import OcrEngine, OcrUnavailableError, build_engine
from evaluations.builders import (
    Case,
    Mode,
    document,
    driving_licence,
    hospital_bill,
    motor_claim_form,
    motor_estimate,
)
from evaluations.harness import EvalResult, LlmSource, Metric, ratio, run_one
from rules_engine.text import excerpt_in_text

GBP = {"amount": "1475.00", "currency": "GBP"}
TEMPLATES: dict[str, tuple[list[str], dict[str, Any]]] = {
    "claim_form": (
        motor_claim_form(),
        {
            "policy_number": "NWM-M-24001",
            "policyholder_name": "Rohan Tanaka",
            "incident_date": "2026-10-08",
            "vehicle_registration": "LK21ABC",
        },
    ),
    "insurance_policy": (
        [
            "NORTHWIND MUTUAL - POLICY SCHEDULE",
            "Policy Number: NWM-M-24001",
            "Policyholder: Rohan Tanaka",
            "Period of insurance: 01 Jan 2026 to 31 Dec 2026",
            "Sum insured: GBP 18,000.00",
            "Excess: GBP 500.00",
            "Exclusions apply as set out in section 4.",
        ],
        {"policy_number": "NWM-M-24001", "policyholder_name": "Rohan Tanaka"},
    ),
    "identity_document": (
        [
            "PASSPORT",
            "Surname: TANAKA",
            "Given names: ROHAN",
            "Nationality: British",
            "Date of birth: 12 Apr 1988",
            "Date of expiry: 03 Jun 2033",
        ],
        {"date_of_birth": "1988-04-12"},
    ),
    "driving_licence": (driving_licence(), {"date_of_birth": "1988-04-12", "licence_expiry": "2031-03-14"}),
    "vehicle_registration": (
        [
            "VEHICLE REGISTRATION CERTIFICATE (V5C)",
            "Registered keeper: Rohan Tanaka",
            "Registration number: LK21 ABC",
            "Vehicle identification number: SB1KV58E30F012345",
            "Year of manufacture: 2021",
        ],
        {"vehicle_registration": "LK21ABC", "vehicle_year": "2021"},
    ),
    "medical_bill": (
        hospital_bill(),
        {
            "invoice_number": "INV-778812",
            "patient_name": "Amelia Hartley",
            "policy_number": "NWM-H-24000",
            "admission_date": "2026-09-14",
            "discharge_date": "2026-09-17",
            "total_amount": {"amount": "6660.50", "currency": "USD"},
        },
    ),
    "hospital_report": (
        [
            "ST. ANNE'S HOSPITAL - DISCHARGE SUMMARY",
            "Patient: Amelia Hartley",
            "Admission date: 2026-09-14",
            "Discharge date: 2026-09-17",
            "Diagnosis: Acute appendicitis",
            "Treatment: laparoscopic appendectomy",
            "Attending physician: Dr Kofi Mensah",
        ],
        {
            "patient_name": "Amelia Hartley",
            "admission_date": "2026-09-14",
            "discharge_date": "2026-09-17",
            "diagnosis": "Acute appendicitis",
        },
    ),
    "repair_estimate": (
        motor_estimate(),
        {
            "estimate_number": "EST-55210",
            "invoice_date": "2026-10-22",
            "policyholder_name": "Rohan Tanaka",
            "policy_number": "NWM-M-24001",
            "vehicle_registration": "LK21ABC",
            "incident_date": "2026-10-08",
            "total_amount": GBP,
            "vehicle_year": "2021",
        },
    ),
    "repair_invoice": (
        [
            "BRIDGEWATER AUTO BODY - INVOICE",
            "Invoice No: INV-30117",
            "Invoice Date: 28 Oct 2026",
            "Policyholder: Rohan Tanaka",
            "Registration: LK21 ABC",
            "Rear bumper replacement - parts 640.00",
            "Labour 6.5 hours at 70.00 - 455.00",
            "Paint and panel work - 380.00",
            "VAT 0.00",
            "Balance due: GBP 1,475.00",
        ],
        {
            "invoice_number": "INV-30117",
            "invoice_date": "2026-10-28",
            "policyholder_name": "Rohan Tanaka",
            "vehicle_registration": "LK21ABC",
            "total_amount": GBP,
        },
    ),
    "accident_report": (
        [
            "ACCIDENT REPORT",
            "Date of accident: 08 Oct 2026",
            "Location: junction of Park Row and Albion St, Leeds",
            "Other driver: delivery van, registration YX70 DLV",
            "Witness: none",
            "Road conditions: wet",
        ],
        {"incident_date": "2026-10-08"},
    ),
    "police_report": (
        [
            "WEST YORKSHIRE POLICE STATION REPORT",
            "Crime reference: CR/2026/48812",
            "Officer: PC J Okafor",
            "Date of incident: 08 Oct 2026",
            "Statement taken from the driver at the scene.",
        ],
        {"crime_reference": "CR/2026/48812", "incident_date": "2026-10-08"},
    ),
    "bank_information": (
        [
            "BANK DETAILS FOR CLAIM PAYMENT",
            "Account holder: Rohan Tanaka",
            "Sort code: 20-00-00",
            "Account number: provided separately",
        ],
        {},
    ),
    "supporting_correspondence": (
        [
            "Dear Claims Team,",
            "Re: Policy Number: NWM-M-24001",
            "Please find my repair estimate attached.",
            "Kind regards,",
            "Rohan Tanaka",
        ],
        {"policy_number": "NWM-M-24001"},
    ),
}


@dataclass(frozen=True)
class Variant:
    mode: Mode
    degrade: str | None = None
    rotate: int = 0
    label: str = "clean"

    @property
    def clean(self) -> bool:
        return self.label in ("pdf", "clean")


def variants() -> list[tuple[str, Variant]]:
    out: list[tuple[str, Variant]] = []
    types = list(TEMPLATES)
    out += [(t, Variant("pdf", label="pdf")) for t in types]  # 13
    out += [(t, Variant("scan", label="clean")) for t in types]  # 13
    out += [
        (t, Variant("scan", degrade=("noise", "blur", "faded")[i % 3], label="degraded"))
        for i, t in enumerate(types)
    ]  # 13
    out += [
        (t, Variant("scan", rotate=(90, 180, 270)[i % 3], label="rotated")) for i, t in enumerate(types)
    ]  # 13
    out += [
        (t, Variant("scan", degrade="blur", rotate=(2, -3)[i % 2], label="handwritten-lite"))
        for i, t in enumerate(types[:8])
    ]  # 8
    return out


def same(got: Any, want: Any) -> bool:
    if isinstance(want, dict):
        return isinstance(got, dict) and all(str(got.get(k)) == str(v) for k, v in want.items())

    def norm(v: Any) -> str:
        return "".join(str(v).split()).upper()

    return got is not None and norm(got) == norm(want)


def _ocr() -> OcrEngine | None:
    try:
        engine = build_engine("tesseract")
        return engine
    except (OcrUnavailableError, OSError, RuntimeError):
        return None


async def evaluate(
    llm: LlmSource = None, *, ocr: OcrEngine | None = None, limit: int | None = None
) -> EvalResult:
    ocr = ocr or _ocr()
    items = [(t, v) for t, v in variants() if v.mode == "pdf" or ocr is not None][:limit]
    res = EvalResult("document", len(items), mode="live" if llm else "deterministic")
    if ocr is None:
        res.notes.append(
            "Tesseract not available here: only the 13 PDF variants ran; "
            "run inside the backend container for all 60."
        )
    cls_ok = 0
    field_hits = {"clean": [0, 0], "degraded": [0, 0]}
    ungrounded = 0
    for n, (doc_type, v) in enumerate(items):
        lines, labels = TEMPLATES[doc_type]
        snap_doc = document(
            f"de{n}",
            f"{doc_type}-{v.label}.{'pdf' if v.mode == 'pdf' else 'png'}",
            lines,
            mode=v.mode,
            ocr=ocr,
            degrade=v.degrade,
            rotate=v.rotate,
            seed=n,
        )
        out = (await run_one(Case(f"de{n}", documents=[snap_doc]), "document", llm)).data or {}
        result = out["documents"][0]
        cls_ok += result["document_type"] == doc_type
        if result["document_type"] != doc_type:
            res.failures.append(f"{doc_type}/{v.label}: classified {result['document_type']}")
        accepted = {f["field_name"]: f for f in result["fields"] if f["validation_status"] != "rejected"}
        bucket = field_hits["clean" if v.clean else "degraded"]
        for name, want in labels.items():
            bucket[1] += 1
            got = accepted.get(name, {}).get("value_normalized")
            hit = same(got, want)
            bucket[0] += hit
            if not hit and v.clean:
                res.failures.append(
                    f"{doc_type}/{v.label}: {name} = {json.dumps(got)} expected {json.dumps(want)}"
                )
        page_text = "\n".join(p.text for p in snap_doc.pages)
        for f in accepted.values():
            if f.get("evidence_excerpt") and not excerpt_in_text(f["evidence_excerpt"], page_text):
                ungrounded += 1
                res.failures.append(f"{doc_type}/{v.label}: accepted ungrounded field {f['field_name']}")
    res.metrics = [
        Metric("classification_accuracy", ratio(cls_ok, len(items)), 0.95),
        Metric("field_exact_match_clean", ratio(*field_hits["clean"]), 0.90),
        Metric("field_exact_match_degraded", ratio(*field_hits["degraded"])),
        Metric("ungrounded_fields_accepted", ungrounded, 0, higher_is_better=False),
    ]
    return res
