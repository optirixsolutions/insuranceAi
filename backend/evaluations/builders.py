"""Builders for synthetic, labelled evaluation snapshots.

Documents go through the real Phase 4 pipeline: a text-layer PDF (pypdfium2) or a rendered scan
read by Tesseract, then the real classifier and rule extractor. Nothing is mocked; only the data
is synthetic (fictional people, policies and garages).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal

from agents.snapshot import (
    ClaimRef,
    ClaimSnap,
    ClaimSnapshot,
    ClauseSnap,
    CoverageSnap,
    CustomerSnap,
    DocumentSnap,
    ExclusionSnap,
    FieldSnap,
    InvoiceRef,
    PageSnap,
    PolicySnap,
    PriorOutput,
    RequiredDoc,
    VersionSnap,
)
from document_processing import detect
from document_processing.classify import classify
from document_processing.extract import extract_fields
from document_processing.ocr import OcrEngine
from document_processing.pages import extract_pages
from evaluations.renderers import scan_image, text_pdf

TODAY = date(2026, 11, 5)
NS = uuid.UUID("6f1c2d1e-7a51-4c1e-9a0b-0c1a1e5a6b00")
Mode = Literal["text", "pdf", "scan"]


def uid(*parts: object) -> str:
    return str(uuid.uuid5(NS, "/".join(str(p) for p in parts)))


def document(
    key: str,
    filename: str,
    lines: list[str],
    *,
    mode: Mode = "text",
    ocr: OcrEngine | None = None,
    degrade: str | None = None,
    rotate: int = 0,
    seed: int = 1,
    scan_status: str = "clean",
    duplicates: list[dict[str, Any]] | None = None,
) -> DocumentSnap:
    """Run synthetic content through the real page, classification and extraction pipeline."""
    doc_id = uid("doc", key)
    is_image = mode == "scan"
    raw: list[tuple[int, str, list[Any], float | None, float]]
    if mode == "text":
        raw = [(1, "\n".join(lines), [], None, 1.0)]
    else:
        if mode == "pdf":
            pages = extract_pages(text_pdf(lines), detect.PDF, ocr, max_pages=5)
        else:
            if ocr is None:
                raise RuntimeError("scan mode needs an OCR engine (run inside the backend container)")
            data = scan_image(lines, degrade=degrade, rotate=rotate, seed=seed)
            pages = extract_pages(data, "image/png", ocr, max_pages=5)
        raw = [(p.page_number, p.text, list(p.words), p.ocr_confidence, p.score) for p in pages]
    text = "\n".join(r[1] for r in raw)
    cls = classify(text, is_image=is_image)
    candidates = extract_fields([(n, t, w, c) for n, t, w, c, _ in raw])
    quality = min((r[4] for r in raw), default=0.0)
    fields = [
        FieldSnap(
            id=uid("field", key, c.field_name, i),
            field_name=c.field_name,
            value_text=c.value_text,
            value_normalized=c.value_normalized,
            value_type=c.value_type,
            confidence=c.confidence,
            page_number=c.page_number,
            evidence_excerpt=c.evidence_excerpt,
        )
        for i, c in enumerate(candidates)
    ]
    return DocumentSnap(
        id=doc_id,
        filename=filename,
        document_type=cls.document_type,
        classification_confidence=cls.confidence,
        classification_source="rules",
        scan_status=scan_status,
        quality_score=quality,
        duplicates=duplicates or [],
        pages=[PageSnap(page_number=n, text=t, ocr_confidence=c, quality_score=s) for n, t, _, c, s in raw],
        fields=fields,
    )


# --- policies --------------------------------------------------------------------------------


def motor_version(
    key: str,
    start: date,
    *,
    no: int = 1,
    status: str = "published",
    years: int = 1,
    sum_insured: str = "18000.00",
    deductible: str = "500.00",
    waiting: int = 0,
    vehicle_year: int | None = 2021,
) -> VersionSnap:
    vid = uid("version", key, no)
    clauses = [
        ClauseSnap(id=uid("clause", key, no, ref), clause_ref=ref, title=title, text=text)
        for ref, title, text in (
            (
                "2.1",
                "Own damage",
                "We cover accidental damage to the insured vehicle by collision, fire, theft, weather, "
                "vandalism or glass breakage.",
            ),
            (
                "4.2",
                "Unlicensed driver",
                "We do not cover loss while the vehicle is driven by a person without a valid "
                "driving licence.",
            ),
            (
                "4.5",
                "Wear and tear",
                "We do not cover wear and tear, rust, mechanical or electrical breakdown.",
            ),
        )
    ]
    return VersionSnap(
        id=vid,
        version_no=no,
        status=status,
        effective_from=start,
        effective_to=start + timedelta(days=365 * years - 1),
        currency="GBP",
        sum_insured=Decimal(sum_insured),
        deductible_amount=Decimal(deductible),
        waiting_period_days=waiting,
        endorsements=[{"vehicle_year": vehicle_year}] if vehicle_year else [],
        coverages=[
            CoverageSnap(
                coverage_code="own_damage",
                perils=["collision", "fire", "theft", "weather", "vandalism", "glass"],
                limit_amount=Decimal(sum_insured),
                clause_ref="2.1",
            )
        ],
        exclusions=[
            ExclusionSnap(
                exclusion_code="unlicensed_driver",
                applies_to=["own_damage"],
                description="Unlicensed driver",
                clause_ref="4.2",
            ),
            ExclusionSnap(
                exclusion_code="wear_and_tear",
                applies_to=["own_damage"],
                description="Wear and tear",
                clause_ref="4.5",
            ),
        ],
        clauses=clauses,
    )


def health_version(
    key: str,
    start: date,
    *,
    no: int = 1,
    status: str = "published",
    waiting: int = 30,
    aggregate: str = "50000.00",
    deductible: str = "250.00",
    co_pay: str = "10.00",
    sub_limits: dict[str, str] | None = None,
) -> VersionSnap:
    vid = uid("version", key, no)
    clauses = [
        ClauseSnap(id=uid("clause", key, no, ref), clause_ref=ref, title=title, text=text)
        for ref, title, text in (
            (
                "3.1",
                "Medical expenses",
                "We pay reasonable and customary medical expenses for inpatient, outpatient, emergency "
                "and day-care treatment.",
            ),
            (
                "6.1",
                "Pre-existing conditions",
                "We do not pay for conditions diagnosed or treated before cover started.",
            ),
        )
    ]
    return VersionSnap(
        id=vid,
        version_no=no,
        status=status,
        effective_from=start,
        effective_to=start + timedelta(days=364),
        currency="USD",
        sum_insured=Decimal(aggregate),
        deductible_amount=Decimal(deductible),
        annual_aggregate_limit=Decimal(aggregate),
        waiting_period_days=waiting,
        endorsements=[
            {"type": "sub_limit", "category": k, "amount": v} for k, v in (sub_limits or {}).items()
        ],
        coverages=[
            CoverageSnap(
                coverage_code="medical_expenses",
                perils=["inpatient", "outpatient", "emergency", "day_care"],
                limit_amount=Decimal(aggregate),
                co_pay_pct=Decimal(co_pay),
                clause_ref="3.1",
            )
        ],
        exclusions=[
            ExclusionSnap(
                exclusion_code="pre_existing",
                applies_to=["medical_expenses"],
                description="Pre-existing conditions",
                clause_ref="6.1",
            )
        ],
        clauses=clauses,
    )


@dataclass
class Case:
    """One synthetic claim; fields not given get realistic defaults for the line of business."""

    key: str
    line: Literal["motor", "health"] = "motor"
    category: str | None = None
    incident: date = date(2026, 10, 8)
    reported: date | None = None
    amount: str = "1475.00"
    description: str = "Rear-ended at a junction in Leeds by a delivery van while stationary at a red light."
    holder: str = "Rohan Tanaka"
    dob: date | None = date(1988, 4, 12)
    policy_status: str = "active"
    versions: list[VersionSnap] | None = None
    documents: list[DocumentSnap] = field(default_factory=list)
    required: list[tuple[str, bool]] | None = None
    policy_claims: list[ClaimRef] = field(default_factory=list)
    customer_claims: list[ClaimRef] = field(default_factory=list)
    invoices: list[InvoiceRef] = field(default_factory=list)
    baseline: list[Decimal] = field(default_factory=list)
    usage: str = "0"
    prior: dict[str, PriorOutput] = field(default_factory=dict)
    synthetic: bool = True

    def snapshot(self) -> ClaimSnapshot:
        currency = "GBP" if self.line == "motor" else "USD"
        versions = self.versions or [
            motor_version(self.key, date(2026, 1, 1))
            if self.line == "motor"
            else health_version(self.key, date(2026, 1, 1))
        ]
        required = self.required or (
            [("claim_form", True), ("repair_estimate", True), ("driving_licence", False)]
            if self.line == "motor"
            else [("claim_form", True), ("medical_bill", True), ("hospital_report", False)]
        )
        reported = self.reported or self.incident + timedelta(days=2)
        claim = ClaimSnap(
            id=uid("claim", self.key),
            claim_reference=f"CLM-2026-{int(uid('ref', self.key)[:8], 16) % 900000 + 100000:06d}",
            line=self.line,
            category=self.category or ("collision" if self.line == "motor" else "inpatient"),
            status="SUBMITTED",
            priority="normal",
            incident_date=self.incident,
            reported_at=datetime.combine(reported, time(10, 0), tzinfo=UTC),
            description=self.description,
            claimed_amount=Decimal(self.amount),
            currency=currency,
            is_synthetic=self.synthetic,
        )
        return ClaimSnapshot(
            organization_id=uid("org"),
            claim=claim,
            customer=CustomerSnap(
                id=uid("customer", self.key), full_name=self.holder, date_of_birth=self.dob
            ),
            policy=PolicySnap(
                id=uid("policy", self.key),
                policy_number="NWM-M-24001" if self.line == "motor" else "NWM-H-24000",
                status=self.policy_status,
                versions=versions,
            ),
            required_documents=[RequiredDoc(document_type=t, mandatory=m) for t, m in required],
            documents=self.documents,
            customer_claims=self.customer_claims,
            policy_claims=self.policy_claims,
            other_invoices=self.invoices,
            amount_baseline=self.baseline,
            usage_to_date=Decimal(self.usage),
            prior_outputs=self.prior,
            today=TODAY,
        )


def other_claim(
    key: str, incident: date, amount: str = "1475.00", currency: str = "GBP", status: str = "SUBMITTED"
) -> ClaimRef:
    return ClaimRef(
        id=uid("claim", key),
        claim_reference=f"CLM-2026-{int(uid('ref', key)[:8], 16) % 900000 + 100000:06d}",
        policy_id=uid("policy", key),
        incident_date=incident,
        claimed_amount=Decimal(amount),
        currency=currency,
        status=status,
        created_at=datetime.combine(incident, time(9), tzinfo=UTC),
    )


# --- standard synthetic documents ------------------------------------------------------------


def motor_estimate(
    holder: str = "Rohan Tanaka",
    loss: str = "08 Oct 2026",
    parts: str = "640.00",
    labour: str = "455.00",
    paint: str = "380.00",
    total: str | None = None,
    reg: str = "LK21 ABC",
    number: str = "EST-55210",
    policy: str = "NWM-M-24001",
    extra: list[str] | None = None,
) -> list[str]:
    total = total or f"{Decimal(parts) + Decimal(labour) + Decimal(paint):,.2f}"
    return [
        "BRIDGEWATER AUTO BODY - REPAIR ESTIMATE",
        f"Estimate No: {number}",
        "Invoice Date: 22/10/2026",
        f"Policyholder: {holder}",
        f"Policy Number: {policy}",
        f"Registration: {reg}",
        f"Date of loss: {loss}",
        "Year of manufacture: 2021",
        f"Rear bumper replacement - parts {parts}",
        f"Labour 6.5 hours at 70.00 - {labour}",
        f"Paint and panel work - {paint}",
        *(extra or []),
        f"Total due: GBP {total}",
    ]


def motor_claim_form(
    holder: str = "Rohan Tanaka",
    loss: str = "08 Oct 2026",
    policy: str = "NWM-M-24001",
    reg: str = "LK21 ABC",
) -> list[str]:
    return [
        "MOTOR CLAIM FORM",
        f"Policy Number: {policy}",
        f"Policyholder: {holder}",
        f"Date of loss: {loss}",
        f"Registration: {reg}",
        "Declaration: I confirm the information above is true.",
        "Signature: R Tanaka",
    ]


def driving_licence(
    holder: str = "Rohan Tanaka", expiry: str = "14 Mar 2031", dob: str = "12 Apr 1988"
) -> list[str]:
    return [
        "DRIVING LICENCE",
        f"Licence holder: {holder}",
        "Licence No: TANAK804128R99XY",
        f"Date of birth: {dob}",
        f"Licence expiry: {expiry}",
        "Categories: B, BE",
    ]


def hospital_bill(
    patient: str = "Amelia Hartley",
    admitted: str = "2026-09-14",
    discharged: str = "2026-09-17",
    ward: str = "2,250.00",
    procedure: str = "4,100.00",
    pharmacy: str = "310.50",
    total: str | None = None,
    number: str = "INV-778812",
    policy: str = "NWM-H-24000",
) -> list[str]:
    total = total or f"{sum(Decimal(x.replace(',', '')) for x in (ward, procedure, pharmacy)):,.2f}"
    return [
        "ST. ANNE'S HOSPITAL - PATIENT INVOICE",
        f"Invoice No: {number}",
        f"Patient: {patient}",
        f"Policy No: {policy}",
        f"Admission date: {admitted}",
        f"Discharge date: {discharged}",
        f"Ward charges 3 nights - {ward}",
        f"Procedure: laparoscopic appendectomy - {procedure}",
        f"Pharmacy - {pharmacy}",
        f"Amount due: ${total}",
    ]


def health_claim_form(
    patient: str = "Amelia Hartley", incident: str = "14 Sep 2026", policy: str = "NWM-H-24000"
) -> list[str]:
    return [
        "HEALTH CLAIM FORM",
        f"Policy Number: {policy}",
        f"Claimant: {patient}",
        f"Date of incident: {incident}",
        "Declaration: I confirm the information above is true.",
        "Signature: A Hartley",
    ]
