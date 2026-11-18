"""Document intelligence agent (03 §4 extraction safety pipeline).

Rules (Phase 4, grounded by construction) come first. The model (``fast``) proposes candidates
only for expected fields the rules did not find, and classifies documents the rules could not.
A candidate survives only if (1) its excerpt occurs on the cited page (exact or token-set
>= 0.90), (2) its value is derivable from that excerpt and (3) code can normalise it. Confidence
is calibrated as min(model self-confidence, OCR confidence, grounding score, normaliser
certainty); anything under the threshold is ``low_confidence`` and goes to a human.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from agents.base import Agent, AgentContext
from agents.contracts import (
    AgentResult,
    DocumentIntelligenceOutput,
    DocumentResult,
    DuplicateMatch,
    ExtractedFieldOut,
    Finding,
    MissingItem,
    RejectedCandidate,
)
from agents.injection import detect
from agents.snapshot import ClaimSnap, DocumentSnap, FieldSnap, PageSnap
from document_processing.classify import DOCUMENT_TYPES
from document_processing.extract import RULES, normalise_date, normalise_money
from document_processing.grounding import amounts_in, dates_in
from rules_engine.text import excerpt_in_text, normalise

VALUE_TYPES: dict[str, str] = {r.name: r.value_type for r in RULES}
COMMON = ["policy_number", "policyholder_name", "incident_date"]
EXTRACTION_SCHEMA: dict[str, list[str]] = {
    "claim_form": [*COMMON, "claim_reference", "vehicle_registration"],
    "repair_estimate": [
        "estimate_number",
        "invoice_date",
        *COMMON,
        "vehicle_registration",
        "total_amount",
        "provider_name",
        "vehicle_year",
    ],
    "repair_invoice": [
        "invoice_number",
        "invoice_date",
        "policyholder_name",
        "vehicle_registration",
        "total_amount",
        "provider_name",
    ],
    "medical_bill": [
        "invoice_number",
        "invoice_date",
        "patient_name",
        "policy_number",
        "admission_date",
        "discharge_date",
        "total_amount",
        "provider_name",
    ],
    "hospital_report": [
        "patient_name",
        "admission_date",
        "discharge_date",
        "diagnosis",
        "provider_name",
        "date_of_birth",
    ],
    "police_report": ["crime_reference", "incident_date", "vehicle_registration"],
    "driving_licence": ["policyholder_name", "date_of_birth", "licence_expiry"],
    "identity_document": ["policyholder_name", "date_of_birth"],
}
MAX_PAGE_CHARS = 4000
CLASSIFY_BELOW = 0.6  # ask the model to classify below this
CLASSIFICATION_REVIEW_BELOW = 0.7  # same threshold as Phase 4 document review


class LlmFieldCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_name: str
    value: str
    page_number: int
    evidence_excerpt: str = Field(description="The exact line of page text the value was read from")
    self_confidence: float = Field(ge=0, le=1)


class LlmFieldCandidates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_type: str | None = Field(description="Only when asked to classify; else null")
    fields: list[LlmFieldCandidate]


def readability(doc: DocumentSnap) -> Literal["good", "degraded", "unreadable"]:
    text = sum(len(p.text.strip()) for p in doc.pages)
    if doc.document_type == "photo_evidence":
        return "good" if (doc.quality_score or 0) >= 0.4 else "degraded"
    if not doc.pages or text < 20:
        return "unreadable"
    q = doc.quality_score if doc.quality_score is not None else 1.0
    return "good" if q >= 0.7 else "degraded" if q >= 0.4 else "unreadable"


def rules_field(doc: DocumentSnap, f: FieldSnap, threshold: float) -> ExtractedFieldOut:
    if f.validation_status == "rejected" or f.grounding_status == "ungrounded":
        status = "rejected"
    elif f.source == "human":
        status = "corrected"
    elif f.validation_status == "verified":
        status = "verified"
    else:
        status = "low_confidence" if f.confidence < threshold else "unverified"
    return ExtractedFieldOut(
        field_name=f.field_name,
        value=f.value_text,
        value_normalized=f.value_normalized,
        confidence=round(f.confidence, 4),
        source_document_id=doc.id,
        page_number=f.page_number or 1,
        evidence_excerpt=(f.evidence_excerpt or None) and f.evidence_excerpt[:500],
        validation_status=status,
        source="human" if f.source == "human" else "rules",
        field_id=f.id,
    )


def normalise_candidate(name: str, value: str, excerpt: str) -> tuple[Any, float] | None:
    """(normalised value, normaliser certainty) if the value is derivable from the excerpt."""
    kind = VALUE_TYPES.get(name, "string")
    if kind == "date":
        iso, ambiguous = normalise_date(value)
        if iso is None or iso not in dates_in(excerpt):
            return None
        return iso, 0.8 if ambiguous else 1.0
    if kind == "money":
        money = normalise_money(value.strip())
        if not money or money["amount"] is None:
            return None
        from decimal import Decimal

        if Decimal(money["amount"]) not in amounts_in(excerpt):
            return None
        return money, 1.0 if money["currency"] else 0.85
    if not normalise(value) or normalise(value) not in normalise(excerpt):
        return None
    if kind == "identifier":
        return value.strip().upper(), 1.0
    return " ".join(value.split()), 1.0


def ground(
    doc: DocumentSnap, c: LlmFieldCandidate, threshold: float
) -> ExtractedFieldOut | RejectedCandidate:
    page: PageSnap | None = next((p for p in doc.pages if p.page_number == c.page_number), None)
    if page is None:
        return RejectedCandidate(
            field_name=c.field_name, page_number=c.page_number, reason="cited page does not exist"
        )
    score = excerpt_in_text(c.evidence_excerpt, page.text)
    if not score:
        return RejectedCandidate(
            field_name=c.field_name,
            page_number=c.page_number,
            reason="ungrounded: excerpt not found on the cited page",
        )
    normalised = normalise_candidate(c.field_name, c.value, c.evidence_excerpt)
    if normalised is None:
        return RejectedCandidate(
            field_name=c.field_name,
            page_number=c.page_number,
            reason="value is not derivable from the excerpt",
        )
    value, certainty = normalised
    calibrated = min(
        c.self_confidence, page.ocr_confidence if page.ocr_confidence is not None else 1.0, score, certainty
    )
    return ExtractedFieldOut(
        field_name=c.field_name,
        value=c.value.strip()[:500],
        value_normalized=value,
        confidence=round(calibrated, 4),
        source_document_id=doc.id,
        page_number=c.page_number,
        evidence_excerpt=c.evidence_excerpt[:500],
        validation_status="low_confidence" if calibrated < threshold else "unverified",
        source="llm",
    )


def candidate_validator(
    allowed: set[str], pages: set[int], classify: bool
) -> Callable[[LlmFieldCandidates], list[str]]:
    """Schema-level checks the gateway can repair (wrong field, missing page, unknown type)."""

    def validate(r: LlmFieldCandidates) -> list[str]:
        problems = [
            f"field '{c.field_name}' was not requested" for c in r.fields if c.field_name not in allowed
        ]
        problems += [f"page {c.page_number} does not exist" for c in r.fields if c.page_number not in pages]
        if classify and r.document_type is not None and r.document_type not in DOCUMENT_TYPES:
            problems.append(f"document_type '{r.document_type}' is not an allowed type")
        return problems

    return validate


class DocumentAgent(Agent):
    key: ClassVar[str] = "document"
    uses_llm: ClassVar[bool] = True
    allowed_tools: ClassVar[frozenset[str]] = frozenset({"claim.get", "claim.documents"})

    async def run(self, ctx: AgentContext) -> AgentResult:
        claim: ClaimSnap = ctx.tools("claim.get")
        documents: list[DocumentSnap] = ctx.tools("claim.documents")
        results: list[DocumentResult] = []
        findings: list[Finding] = []
        injected: list[str] = []
        used_llm = False
        for doc in documents:
            fields = [rules_field(doc, f, ctx.threshold) for f in doc.fields]
            doc_type, confidence, source = (
                doc.document_type,
                doc.classification_confidence,
                doc.classification_source,
            )
            rejected: list[RejectedCandidate] = []
            readable = readability(doc)
            if any(detect(p.text) for p in doc.pages):
                injected.append(doc.id)
            classify = (
                readable != "unreadable"
                and doc.document_type != "photo_evidence"
                and (doc_type is None or (confidence or 0) < CLASSIFY_BELOW)
            )
            have = {f.field_name for f in fields if f.validation_status != "rejected"}
            wanted = [n for n in EXTRACTION_SCHEMA.get(doc_type or "", COMMON) if n not in have]
            if readable != "unreadable" and doc.document_type != "photo_evidence" and (wanted or classify):
                pages = "\n".join(
                    f'<page number="{p.page_number}">\n{p.text[:MAX_PAGE_CHARS]}\n</page>' for p in doc.pages
                )
                ask = {
                    "classify": classify,
                    "allowed_document_types": list(DOCUMENT_TYPES) if classify else [],
                    "fields_to_find": wanted or COMMON,
                }
                validate = candidate_validator(
                    set(wanted or COMMON), {p.page_number for p in doc.pages}, classify
                )
                user = (
                    f"<request>{json.dumps(ask)}</request>\n"
                    f'<document name="{doc.filename}">\n{pages}\n</document>'
                )
                answer = await self.ask(ctx, LlmFieldCandidates, user, validate)
                if answer is not None:
                    used_llm = True
                    if classify and answer.document_type in DOCUMENT_TYPES:
                        doc_type, confidence, source = answer.document_type, 0.7, "llm"
                    for cand in answer.fields:
                        grounded = ground(doc, cand, ctx.threshold)
                        if isinstance(grounded, RejectedCandidate):
                            rejected.append(grounded)
                        else:
                            fields.append(grounded)
            # Conflicts: two accepted values for one field that normalise differently.
            by_name: dict[str, list[ExtractedFieldOut]] = {}
            for f in fields:
                if f.validation_status != "rejected":
                    by_name.setdefault(f.field_name, []).append(f)
            for name, group in by_name.items():
                if len({json.dumps(g.value_normalized, sort_keys=True, default=str) for g in group}) > 1:
                    for g in group:
                        if g.validation_status not in ("verified", "corrected"):
                            g.validation_status = "conflict"
                    findings.append(
                        Finding(
                            code="FIELD_CONFLICT",
                            severity="warning",
                            message=f"{doc.filename}: '{name}' has conflicting values",
                        )
                    )
            verify = sorted(
                {f.field_name for f in fields if f.validation_status in ("low_confidence", "conflict")}
            )
            if (
                source != "human"
                and (confidence or 0) < CLASSIFICATION_REVIEW_BELOW
                and doc_type != "photo_evidence"
            ):
                verify.append("document_type")
            results.append(
                DocumentResult(
                    document_id=doc.id,
                    filename=doc.filename,
                    document_type=doc_type,
                    classification_confidence=confidence,
                    classification_source=source,
                    readability=readable,
                    fields=fields,
                    duplicates=[
                        DuplicateMatch(
                            kind=str(m.get("method")),
                            document_id=str(m.get("document_id")),
                            claim_id=m.get("claim_id"),
                            distance=m.get("distance"),
                        )
                        for m in doc.duplicates
                    ],
                    needs_human_verification=verify,
                    rejected_candidates=rejected,
                )
            )
            if readable == "unreadable":
                findings.append(
                    Finding(
                        code="UNREADABLE",
                        severity="warning",
                        message=f"{doc.filename} is unreadable; request a clearer copy",
                    )
                )
            if rejected:
                findings.append(
                    Finding(
                        code="UNGROUNDED_DISCARDED",
                        severity="info",
                        message=f"{doc.filename}: {len(rejected)} model candidate(s) discarded",
                    )
                )
        if injected:
            findings.append(
                Finding(
                    code="INJECTION_SUSPECTED",
                    severity="warning",
                    message=f"{len(injected)} document(s) contain text addressed to automated systems",
                )
            )
        out = DocumentIntelligenceOutput(documents=results, injection_suspected=injected)
        missing = [
            MissingItem(item=f"{r.filename}: {n}", reason="needs human verification")
            for r in results
            for n in r.needs_human_verification
        ]
        status = "succeeded" if documents else "partial"
        if not documents:
            missing.append(
                MissingItem(item="documents", reason=f"no documents on claim {claim.claim_reference}")
            )
        return AgentResult(
            status=status,
            data=out.model_dump(mode="json"),
            findings=findings,
            missing=missing,
            used_llm=used_llm,
            escalate=bool(injected),
        )
