"""Agent I/O contracts (docs/architecture/03-agent-contracts.md §1, §3). Strict, extra="forbid".

Inputs are read-only snapshots (``agents.snapshot``); these are the validated outputs. Money is
always ``{amount: Decimal-as-string, currency}``. LLM-facing schemas live with each agent and are
narrower; they are converted into these models only after validation, normalisation and grounding.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


Severity = Literal["info", "warning", "error", "critical"]


class EvidenceRef(Strict):
    kind: Literal["page", "field", "clause", "indicator", "line_item", "claim", "document"]
    id: str
    page_number: int | None = None
    excerpt: str | None = Field(default=None, max_length=500)


class Finding(Strict):
    code: str
    severity: Severity
    message: str
    evidence: list[EvidenceRef] = []


class MissingItem(Strict):
    item: str
    reason: str


class Money(Strict):
    amount: Decimal
    currency: str


class AgentResult(Strict):
    status: Literal["succeeded", "partial", "failed"]
    data: dict[str, Any] | None
    findings: list[Finding] = []
    missing: list[MissingItem] = []
    used_llm: bool = False
    escalate: bool = False  # may only ever be set to True (monotonic)
    llm_note: str | None = None  # why the model step was skipped or what it changed


# --- Intake ---------------------------------------------------------------------------------


class RequiredDocumentStatus(Strict):
    document_type: str
    mandatory: bool
    present: bool


class DuplicateClaim(Strict):
    claim_id: str
    claim_reference: str
    incident_date: date


class IntakeOutput(Strict):
    claim_reference: str
    category: str
    priority: Literal["low", "normal", "high", "urgent"]
    mandatory_fields_ok: bool
    field_problems: list[str]
    duplicates: list[DuplicateClaim]
    required_documents: list[RequiredDocumentStatus]
    missing_documents: list[str]


# --- Document intelligence ------------------------------------------------------------------

ValidationStatus = Literal["verified", "unverified", "low_confidence", "conflict", "corrected", "rejected"]


class ExtractedFieldOut(Strict):
    field_name: str
    value: str | None
    value_normalized: Any = None
    confidence: float = Field(ge=0, le=1)
    source_document_id: str
    page_number: int = Field(ge=1)
    evidence_excerpt: str | None = Field(default=None, max_length=500)
    validation_status: ValidationStatus
    source: Literal["rules", "llm", "human"]
    field_id: str | None = None


class DuplicateMatch(Strict):
    kind: str
    document_id: str
    claim_id: str | None = None
    distance: int | None = None


class RejectedCandidate(Strict):
    field_name: str
    page_number: int | None
    reason: str


class DocumentResult(Strict):
    document_id: str
    filename: str
    document_type: str | None
    classification_confidence: float | None
    classification_source: str | None
    readability: Literal["good", "degraded", "unreadable"]
    fields: list[ExtractedFieldOut]
    duplicates: list[DuplicateMatch]
    needs_human_verification: list[str]
    rejected_candidates: list[RejectedCandidate] = []


class DocumentIntelligenceOutput(Strict):
    documents: list[DocumentResult]
    injection_suspected: list[str]  # document ids whose text targets automated systems


# --- Policy verification ----------------------------------------------------------------------

CoverageStatus = Literal[
    "covered", "potentially_covered", "not_covered", "insufficient_evidence", "requires_manual_interpretation"
]


class CoverageDetermination(Strict):
    coverage_code: str
    status: CoverageStatus
    reasons: list[str]
    clause_ids: list[str] = Field(min_length=1)  # citation is mandatory


class ExclusionFinding(Strict):
    exclusion_code: str
    clause_id: str
    reason: str
    source: Literal["rule", "llm"]
    evidence_excerpt: str | None = None


class PolicyVerificationOutput(Strict):
    policy_version_id: str | None
    version_selection_reason: str
    holder_match: Literal["match", "partial", "mismatch", "unknown"]
    policy_active_on_incident_date: bool | None
    waiting_period_satisfied: bool | None
    loss_type: str
    model_perils: list[str]
    coverages: list[CoverageDetermination]
    primary_coverage: str | None
    exclusions_triggered: list[ExclusionFinding]
    exclusions_need_interpretation: list[ExclusionFinding]
    exclusions_not_triggered: list[ExclusionFinding]
    deductible: Money | None
    limit_remaining: Money | None


# --- Fraud ------------------------------------------------------------------------------------


class FraudIndicatorOut(Strict):
    rule_code: str
    source: Literal["rule", "statistical", "llm"]
    severity: Literal["low", "medium", "high"]
    weight: int
    confidence: float
    contribution: float
    description: str
    evidence: list[EvidenceRef] = Field(min_length=1)
    missing_evidence: list[str] = []


class FraudAnalysisOutput(Strict):
    risk_score: int = Field(ge=0, le=100)
    risk_level: Literal["low", "medium", "high", "critical"]
    indicators: list[FraudIndicatorOut]
    supporting_evidence: list[EvidenceRef]
    missing_evidence: list[str]
    recommended_action: Literal["continue", "review", "investigate"]
    framework_version: str
    checks_not_run: list[str]


# --- Assessment -------------------------------------------------------------------------------


class AssessedLineItemOut(Strict):
    line_no: int
    description: str
    category: str
    category_source: Literal["rules", "llm"]
    coverage_code: str | None
    claimed_amount: Decimal
    eligible_amount: Decimal
    depreciation_pct: Decimal
    status: Literal["eligible", "partially_eligible", "ineligible", "unsupported", "duplicate"]
    reason_code: str | None
    evidence: list[EvidenceRef]


class CalcStepOut(Strict):
    op: str
    inputs: dict[str, str]
    output: str
    rule_ref: str


class AssessmentOutput(Strict):
    currency: str
    calculator: Literal["motor_own_damage", "health_medical"]
    rule_set_version: str
    line_items: list[AssessedLineItemOut]
    gross_claimed: Decimal
    gross_eligible: Decimal
    depreciation_total: Decimal
    salvage: Decimal
    deductible_applied: Decimal
    co_pay_applied: Decimal
    limit_remaining_before: Decimal
    limit_capped_amount: Decimal
    eligible_payout: Decimal
    calculation_trace: list[CalcStepOut]
    status: Literal["calculated", "insufficient_inputs"]
    claimed_vs_documented: Decimal | None  # claim form amount minus documented line total


# --- Validation -------------------------------------------------------------------------------


class ValidationFindingOut(Strict):
    check_code: str
    severity: Severity
    field_name: str | None
    observed_values: list[dict[str, Any]]
    evidence: list[EvidenceRef]
    required_correction: str | None
    message: str


class CrossValidationOutput(Strict):
    findings: list[ValidationFindingOut]
    max_severity: Severity | None
    contradictions: list[str]
    checks_run: list[str]


# --- Decision ---------------------------------------------------------------------------------

RecommendationValue = Literal[
    "APPROVE_RECOMMENDED",
    "REJECT_RECOMMENDED",
    "MANUAL_REVIEW_REQUIRED",
    "MORE_INFORMATION_REQUIRED",
    "PENDING_VERIFICATION",
]


class DecisionRecommendationOutput(Strict):
    recommendation: RecommendationValue
    deterministic_outcome: RecommendationValue
    matrix_rule: str
    llm_escalated: bool
    escalation_reasons: list[str]
    explanation: str
    explanation_source: Literal["template", "llm"]
    applied_rules: list[str]
    supporting_evidence: list[EvidenceRef]
    contradictory_evidence: list[EvidenceRef]
    missing_information: list[str]
    mandatory_review_reasons: list[str]
    inputs_from: dict[str, str | None]  # agent_key -> agent_run id the input came from


# --- Compliance -------------------------------------------------------------------------------


class ComplianceCheck(Strict):
    code: str
    passed: bool
    severity: Severity
    message: str


class ComplianceOutput(Strict):
    status: Literal["compliant", "review_required", "non_compliant"]
    checks: list[ComplianceCheck]
    human_review_required: bool
    automated_approval_allowed: bool


OUTPUT_MODELS: dict[str, type[Strict]] = {
    "intake": IntakeOutput,
    "document": DocumentIntelligenceOutput,
    "policy": PolicyVerificationOutput,
    "fraud": FraudAnalysisOutput,
    "assessment": AssessmentOutput,
    "validation": CrossValidationOutput,
    "decision": DecisionRecommendationOutput,
    "compliance": ComplianceOutput,
}
