"""Cross-document validation agent (deterministic, no model in v1).

Compares what the documents say with each other and with the claim/policy record. Severity:
``critical`` = a contradiction that makes the claim untrustworthy as filed (always manual review);
``error`` = must be corrected before approval; ``warning`` = reviewer attention; ``info`` = note.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

from agents.base import Agent, AgentContext, rule_config
from agents.contracts import (
    AgentResult,
    CrossValidationOutput,
    EvidenceRef,
    Finding,
    Severity,
    ValidationFindingOut,
)
from agents.snapshot import ClaimSnap, CustomerSnap, DocumentSnap, FieldSnap, PolicySnap
from rules_engine.text import name_similarity, names_match

ORDER: dict[str, int] = {"info": 0, "warning": 1, "error": 2, "critical": 3}
AMOUNT_DOCS = ("repair_invoice", "medical_bill", "repair_estimate")
NAME_FIELDS = ("policyholder_name", "patient_name")


def _ident(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _money(value: Any) -> tuple[Decimal, str | None] | None:
    if isinstance(value, dict) and value.get("amount") is not None:
        try:
            return Decimal(str(value["amount"])), value.get("currency")
        except InvalidOperation:
            return None
    return None


def usable(field: FieldSnap) -> bool:
    return field.validation_status not in ("rejected",) and field.grounding_status != "ungrounded"


def ref(doc: DocumentSnap, field: FieldSnap) -> EvidenceRef:
    return EvidenceRef(
        kind="field",
        id=field.id,
        page_number=field.page_number,
        excerpt=(field.evidence_excerpt or "")[:500] or None,
    )


class ValidationAgent(Agent):
    key: ClassVar[str] = "validation"
    allowed_tools: ClassVar[frozenset[str]] = frozenset(
        {"claim.get", "customer.get", "policy.lookup", "claim.documents"}
    )

    async def run(self, ctx: AgentContext) -> AgentResult:
        claim: ClaimSnap = ctx.tools("claim.get")
        customer: CustomerSnap = ctx.tools("customer.get")
        policy: PolicySnap = ctx.tools("policy.lookup")
        documents: list[DocumentSnap] = ctx.tools("claim.documents")
        by_name: dict[str, list[tuple[DocumentSnap, FieldSnap]]] = defaultdict(list)
        for doc in documents:
            for f in doc.fields:
                if usable(f):
                    by_name[f.field_name].append((doc, f))
        out: list[ValidationFindingOut] = []
        checks: list[str] = []

        def add(
            code: str,
            sev: Severity,
            msg: str,
            items: list[tuple[DocumentSnap, FieldSnap]],
            field: str | None,
            fix: str | None,
        ) -> None:
            out.append(
                ValidationFindingOut(
                    check_code=code,
                    severity=sev,
                    field_name=field,
                    observed_values=[
                        {
                            "document_id": d.id,
                            "document_type": d.document_type,
                            "value": f.value_normalized or f.value_text,
                        }
                        for d, f in items
                    ],
                    evidence=[ref(d, f) for d, f in items] or [EvidenceRef(kind="claim", id=claim.id)],
                    required_correction=fix,
                    message=msg,
                )
            )

        # Incident date: documents vs claim, and documents vs each other.
        checks.append("V-INCIDENT-DATE")
        dated = [(d, f, v) for d, f in by_name.get("incident_date", []) if (v := _date(f.value_normalized))]
        for d, f, v in dated:
            gap = abs((v - claim.incident_date).days)
            if gap > 1:
                add(
                    "V-INCIDENT-DATE",
                    "critical",
                    f"{d.filename} gives incident date {v}, claim says {claim.incident_date}",
                    [(d, f)],
                    "incident_date",
                    "Confirm the incident date with the claimant",
                )
            elif gap == 1:
                add(
                    "V-INCIDENT-DATE",
                    "warning",
                    f"{d.filename} gives incident date {v}, one day from the claim",
                    [(d, f)],
                    "incident_date",
                    None,
                )
        checks.append("V-DATE-ACROSS-DOCS")
        values = {v for _, _, v in dated}
        if values and (max(values) - min(values)).days > 1:
            add(
                "V-DATE-ACROSS-DOCS",
                "critical",
                "Documents disagree on the incident date by more than a day",
                [(d, f) for d, f, _ in dated],
                "incident_date",
                "Resolve which document is correct",
            )

        checks.append("V-POLICY-NUMBER")
        for d, f in by_name.get("policy_number", []):
            if _ident(f.value_text) != _ident(policy.policy_number):
                add(
                    "V-POLICY-NUMBER",
                    "critical",
                    f"{d.filename} cites policy {f.value_text}, claim is on {policy.policy_number}",
                    [(d, f)],
                    "policy_number",
                    "Check the claim was filed on the right policy",
                )

        checks.append("V-CLAIM-REFERENCE")
        for d, f in by_name.get("claim_reference", []):
            if _ident(f.value_text) != _ident(claim.claim_reference):
                add(
                    "V-CLAIM-REFERENCE",
                    "error",
                    f"{d.filename} refers to claim {f.value_text}",
                    [(d, f)],
                    "claim_reference",
                    "Confirm the document belongs to this claim",
                )

        checks.append("V-NAME")
        for name_field in NAME_FIELDS:
            for d, f in by_name.get(name_field, []):
                if f.value_text and not names_match(f.value_text, customer.full_name):
                    sim = name_similarity(f.value_text, customer.full_name)
                    sev: Severity = "critical" if sim < 0.80 else "error"
                    add(
                        "V-NAME",
                        sev,
                        f"{d.filename} names '{f.value_text}', policyholder is "
                        f"'{customer.full_name}' (similarity {sim:.2f})",
                        [(d, f)],
                        name_field,
                        "Verify the identity of the person named",
                    )

        checks.append("V-DOB")
        if customer.date_of_birth:
            for d, f in by_name.get("date_of_birth", []):
                v = _date(f.value_normalized)
                if v and v != customer.date_of_birth:
                    add(
                        "V-DOB",
                        "error",
                        f"{d.filename} gives date of birth {v}, record says {customer.date_of_birth}",
                        [(d, f)],
                        "date_of_birth",
                        "Verify identity",
                    )

        checks.append("V-REGISTRATION")
        regs = by_name.get("vehicle_registration", [])
        if len({_ident(f.value_text) for _, f in regs}) > 1:
            add(
                "V-REGISTRATION",
                "critical",
                "Documents give different vehicle registrations",
                regs,
                "vehicle_registration",
                "Confirm which vehicle is claimed for",
            )

        checks.append("V-AMOUNT")
        totals = sorted(
            [
                (d, f, m)
                for d, f in by_name.get("total_amount", [])
                if (m := _money(f.value_normalized)) and d.document_type in AMOUNT_DOCS
            ],
            key=lambda t: AMOUNT_DOCS.index(t[0].document_type or "repair_estimate"),
        )
        if totals:
            d, f, (amount, currency) = totals[0]
            if currency and currency != claim.currency:
                add(
                    "V-AMOUNT",
                    "error",
                    f"{d.filename} is in {currency}, the claim is in {claim.currency}",
                    [(d, f)],
                    "total_amount",
                    "Restate the claim in the policy currency",
                )
            elif amount > 0:
                diff = abs(amount - claim.claimed_amount) / amount
                if diff * 100 > rule_config(ctx.snapshot).validation_amount_tolerance_pct:
                    add(
                        "V-AMOUNT",
                        "error",
                        f"Claimed {claim.claimed_amount} but {d.filename} totals {amount}",
                        [(d, f)],
                        "total_amount",
                        "Align the claimed amount with the documents",
                    )
                elif diff > Decimal("0.01"):
                    add(
                        "V-AMOUNT",
                        "warning",
                        f"Claimed {claim.claimed_amount}; {d.filename} totals {amount}",
                        [(d, f)],
                        "total_amount",
                        None,
                    )

        checks.append("V-STAY-ORDER")
        adm = [(d, f, _date(f.value_normalized)) for d, f in by_name.get("admission_date", [])]
        dis = [(d, f, _date(f.value_normalized)) for d, f in by_name.get("discharge_date", [])]
        for ad, af, av in adm:
            for dd, df, dv in dis:
                if av and dv and dv < av:
                    add(
                        "V-STAY-ORDER",
                        "critical",
                        f"Discharge {dv} is before admission {av}",
                        [(ad, af), (dd, df)],
                        "discharge_date",
                        "Correct the hospital dates",
                    )
            if av and abs((av - claim.incident_date).days) > 1 and claim.line == "health":
                add(
                    "V-STAY-ORDER",
                    "warning",
                    f"Admission {av} differs from the incident date {claim.incident_date}",
                    [(ad, af)],
                    "admission_date",
                    None,
                )

        checks.append("V-INVOICE-BEFORE-INCIDENT")
        for d, f in by_name.get("invoice_date", []):
            v = _date(f.value_normalized)
            if v and v < claim.incident_date and d.document_type in ("repair_invoice", "medical_bill"):
                add(
                    "V-INVOICE-BEFORE-INCIDENT",
                    "error",
                    f"{d.filename} is dated {v}, before the incident",
                    [(d, f)],
                    "invoice_date",
                    "Explain why the invoice predates the loss",
                )

        checks.append("V-OCR-RELIABILITY")
        for d in documents:
            low = [f for f in d.fields if f.validation_status == "low_confidence"]
            if low:
                add(
                    "V-OCR-RELIABILITY",
                    "warning",
                    f"{len(low)} low-confidence field(s) in {d.filename}",
                    [(d, f) for f in low],
                    None,
                    "Verify the highlighted fields",
                )
            text_doc = d.document_type != "photo_evidence"  # photos carry no text to read
            if text_doc and d.quality_score is not None and d.quality_score < 0.4:
                add(
                    "V-OCR-RELIABILITY",
                    "warning",
                    f"{d.filename} is poorly readable (quality {d.quality_score:.2f})",
                    [],
                    None,
                    "Request a clearer copy",
                )

        checks.append("V-CALC-INPUTS")
        if not any(d.document_type in AMOUNT_DOCS for d in documents):
            add(
                "V-CALC-INPUTS",
                "warning",
                "No estimate, invoice or bill to calculate from",
                [],
                None,
                "Request an itemised estimate or invoice",
            )

        max_sev: Severity | None = max(
            (f.severity for f in out), key=lambda s: ORDER[s or "info"], default=None
        )
        result = CrossValidationOutput(
            findings=out,
            max_severity=max_sev,
            contradictions=[f.message for f in out if f.severity == "critical"],
            checks_run=checks,
        )
        findings = [
            Finding(code=f.check_code, severity=f.severity, message=f.message, evidence=f.evidence)
            for f in out
            if f.severity != "info"
        ]
        return AgentResult(
            status="succeeded",
            data=result.model_dump(mode="json"),
            findings=findings,
            escalate=any(f.severity == "critical" for f in out),
        )
