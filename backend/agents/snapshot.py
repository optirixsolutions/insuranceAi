"""Read-only claim snapshot and the scoped toolbox agents read it through.

The service layer loads one ``ClaimSnapshot`` per run (bound to one organisation and one claim,
under RLS). Agents never see the snapshot directly: they call named tools, and ``ScopedToolbox``
refuses any tool outside the agent's ``allowed_tools``. No tool writes. The evaluation runner
builds snapshots in memory, so agents run identically against the database and against eval sets.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict


class Snap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PageSnap(Snap):
    page_number: int
    text: str
    ocr_confidence: float | None = None
    quality_score: float = 1.0


class FieldSnap(Snap):
    id: str
    field_name: str
    value_text: str | None
    value_normalized: Any = None
    value_type: str = "string"
    confidence: float = 1.0
    page_number: int | None = None
    evidence_excerpt: str | None = None
    validation_status: str = "unverified"
    grounding_status: str = "grounded"
    source: str = "rules"


class DocumentSnap(Snap):
    id: str
    filename: str
    document_type: str | None
    classification_confidence: float | None = None
    classification_source: str | None = None
    scan_status: str = "clean"
    processing_status: str = "processed"
    quality_score: float | None = 1.0
    review_reasons: list[str] = []
    duplicates: list[dict[str, Any]] = []
    pages: list[PageSnap] = []
    fields: list[FieldSnap] = []


class ClauseSnap(Snap):
    id: str
    clause_ref: str
    section: str = ""
    title: str = ""
    text: str = ""


class CoverageSnap(Snap):
    coverage_code: str
    perils: list[str]
    limit_amount: Decimal
    deductible_override: Decimal | None = None
    co_pay_pct: Decimal | None = None
    clause_ref: str


class ExclusionSnap(Snap):
    exclusion_code: str
    applies_to: list[str]
    description: str = ""
    clause_ref: str


class VersionSnap(Snap):
    id: str
    version_no: int
    status: str
    effective_from: date
    effective_to: date
    currency: str
    sum_insured: Decimal
    deductible_amount: Decimal
    annual_aggregate_limit: Decimal | None = None
    waiting_period_days: int = 0
    endorsements: list[dict[str, Any]] = []
    coverages: list[CoverageSnap] = []
    exclusions: list[ExclusionSnap] = []
    clauses: list[ClauseSnap] = []


class PolicySnap(Snap):
    id: str
    policy_number: str
    status: str
    versions: list[VersionSnap] = []


class CustomerSnap(Snap):
    id: str
    full_name: str
    date_of_birth: date | None = None


class ClaimRef(Snap):
    id: str
    claim_reference: str
    policy_id: str
    incident_date: date
    claimed_amount: Decimal
    currency: str
    status: str
    created_at: datetime


class InvoiceRef(Snap):
    claim_id: str
    claim_reference: str
    invoice_number: str
    vendor: str | None = None


class ClaimSnap(Snap):
    id: str
    claim_reference: str
    line: str  # motor | health | property | travel
    category: str  # loss type
    status: str
    priority: str
    incident_date: date
    reported_at: datetime
    description: str
    claimed_amount: Decimal
    currency: str
    is_synthetic: bool
    policy_version_id: str | None = None


class RequiredDoc(Snap):
    document_type: str
    mandatory: bool


class PriorOutput(Snap):
    agent_run_id: str
    status: str
    data: dict[str, Any] | None
    escalate: bool = False
    used_llm: bool = False


class ClaimSnapshot(Snap):
    organization_id: str
    claim: ClaimSnap
    customer: CustomerSnap
    policy: PolicySnap
    required_documents: list[RequiredDoc] = []
    documents: list[DocumentSnap] = []
    customer_claims: list[ClaimRef] = []  # other claims by the same customer (any policy)
    policy_claims: list[ClaimRef] = []  # other claims on the same policy
    other_invoices: list[InvoiceRef] = []  # invoice numbers on the organisation's other claims
    amount_baseline: list[Decimal] = []  # claimed amounts of other claims on the same product
    usage_to_date: Decimal = Decimal("0")  # amounts already paid against the policy year
    prior_outputs: dict[str, PriorOutput] = {}  # latest successful output per agent on this claim
    org_allows_real_data: bool = False
    confidence_threshold: float = 0.85
    today: date
    rule_set_id: str | None = None  # published rule set for the product (None = built-in defaults)
    rule_set_version: int | None = None
    rule_set_config: dict[str, Any] | None = None


class ToolNotAllowedError(PermissionError):
    pass


def _version_for_date(s: ClaimSnapshot) -> dict[str, Any]:
    return {"incident_date": s.claim.incident_date, "versions": s.policy.versions, "status": s.policy.status}


TOOLS: dict[str, Callable[[ClaimSnapshot], Any]] = {
    "claim.get": lambda s: s.claim,
    "customer.get": lambda s: s.customer,
    "policy.lookup": lambda s: s.policy,
    "policy.version_for_date": _version_for_date,
    "policy.usage_to_date": lambda s: s.usage_to_date,
    "product.document_requirements": lambda s: s.required_documents,
    "claims.find_similar_recent": lambda s: s.policy_claims,
    "claims.history_for_customer": lambda s: s.customer_claims,
    "claims.history_for_policy": lambda s: s.policy_claims,
    "claims.invoice_numbers": lambda s: s.other_invoices,
    "claim.documents": lambda s: s.documents,
    "document.pages": lambda s: [(d.id, d.pages) for d in s.documents],
    "documents.find_by_hash": lambda s: [(d.id, d.duplicates) for d in s.documents],
    "stats.amount_baseline": lambda s: s.amount_baseline,
    "agents.prior_outputs": lambda s: s.prior_outputs,
    "org.ai_data_approval": lambda s: s.org_allows_real_data,
    "clock.today": lambda s: s.today,
}


class ScopedToolbox:
    """Only the agent's allowed tools, pre-bound to this organisation and claim."""

    def __init__(self, snapshot: ClaimSnapshot, allowed: frozenset[str]) -> None:
        unknown = allowed - TOOLS.keys()
        if unknown:
            raise ValueError(f"Unknown tools: {sorted(unknown)}")
        self._snapshot = snapshot
        self._allowed = allowed
        self.calls: list[str] = []

    def __call__(self, name: str) -> Any:
        if name not in self._allowed:
            raise ToolNotAllowedError(f"Tool '{name}' is not allowed for this agent")
        self.calls.append(name)
        return TOOLS[name](self._snapshot)
