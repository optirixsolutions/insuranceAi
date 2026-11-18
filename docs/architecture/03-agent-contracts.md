# 03 — Agent Contracts, Rules Engine & Scoring

## 1. Common agent contract

```python
class Agent(Protocol[In, Out]):
    key: str                                   # "document", "policy", ...
    input_model: type[In]                      # Pydantic, strict
    output_model: type[Out]                    # Pydantic, strict, extra="forbid"
    allowed_tools: frozenset[str]              # checked by ToolRegistry at call time
    async def run(self, inp: In, ctx: AgentContext) -> Out: ...

class AgentContext:   # injected by the workflow node, immutable
    tenant: TenantContext          # org id, data classification, AI data approval flag
    execution_id: UUID; attempt: int
    tools: ScopedToolbox           # only allowed_tools, pre-bound to org + claim
    llm: ModelGatewayClient        # resolves agent_configs → model; enforces budget/data policy
    rules: RuleSetSnapshot         # published rule set version for the product
    clock: Clock; logger: BoundLogger; deadline: datetime
```

Every run produces an `agent_runs` row (model, prompt version, rule-set version, tokens, latency, status) and,
if successful, an `agent_outputs` row with the validated JSON. Agents **return** results; the workflow node
persists them through services.

Common output envelope:

```python
class AgentResult(BaseModel, Generic[T]):
    status: Literal["succeeded", "partial", "failed"]
    data: T | None
    findings: list[Finding] = []        # code, severity, message, evidence refs
    missing: list[MissingItem] = []
    used_llm: bool
    escalate: bool = False              # may only be set True (monotonic)
```

`EvidenceRef = {kind: "page"|"field"|"clause"|"indicator"|"line_item", id: UUID, page_number?: int, excerpt?: str}`

## 2. Responsibility matrix

| Agent | Responsibility | LLM use | Deterministic core | Allowed tools (read-only unless noted) | Output schema |
|-------|----------------|---------|--------------------|----------------------------------------|---------------|
| **Intake** | mandatory fields, reference, duplicates, required docs, category, priority | optional: category from free-text description (`fast`) | field validation, duplicate rules (same policy + incident date ± 3 days + similar amount), required-doc lookup, priority score | `customer.get`, `policy.lookup_by_number`, `claims.find_similar_recent`, `product.document_requirements` | `IntakeResult` |
| **Document Intelligence** | classify, extract, flag unreadable/duplicate, page evidence | classification + field extraction (`fast` text / `vision` if verified) | OCR pipeline, hashing, grounding check, normalisation (dates, money, IDs), confidence calibration | `document.pages`, `document.page_image` (vision only), `product.extraction_schema`, `documents.find_by_hash` | `DocumentIntelligenceResult` |
| **Policy Verification** | correct version, holder identity, dates/status, coverages, deductibles, exclusions, limits, waiting periods, endorsements | maps loss description + extracted facts to `perils` and flags exclusions *needing interpretation* (`reasoning`) | version selection by incident date, validity/status/waiting period checks, machine-checkable exclusion predicates, limit lookup | `policy.version_for_date`, `policy.clauses`, `policy.usage_to_date` | `PolicyVerificationResult` |
| **Fraud Investigation** | explainable indicators + score | interprets narrative contradictions, cites evidence (`reasoning`), weight-capped | rule indicators, robust z-score anomalies, frequency, duplicate hashes, scoring framework | `claims.history_for_customer`, `claims.history_for_policy`, `documents.find_by_hash/phash/simhash`, `stats.amount_baseline` | `FraudAnalysisResult` |
| **Claim Assessment** | line items, unsupported/duplicate charges, limits, deductible, depreciation, payout | line-item normalisation and category/coverage mapping (`fast`) | **all arithmetic** in `rules_engine.money` | `assessment.line_item_candidates` (from extracted fields), `policy.usage_to_date`, `rules.depreciation_table` | `AssessmentResult` |
| **Validation** | cross-document consistency | none in v1 | date/name/ID/amount comparisons, fuzzy name match (Jaro-Winkler ≥ 0.92 + token rules), OCR reliability, calculation input completeness | `claim.fields`, `claim.documents` | `CrossValidationResult` |
| **Decision Recommendation** | recommendation + explanation + citations + contradictions | explanation text and escalation only (`reasoning`) | decision matrix (§5) | `claim.evidence_index` | `DecisionRecommendationResult` |
| **Compliance** | review requirements, evidence completeness, policy version, permissions, explanation completeness, audit completeness, retention/privacy flags | none | compliance profile rules | `audit.events_for_execution`, `compliance.profile`, `users.authorities` | `ComplianceResult` |
| **Supervisor** | routing, parallelism, validation, retries, contradiction detection, interrupts | none (ADR-001) | routing policy | none (orchestration only) | updates `ClaimState` |

No agent has a write tool. Tool arguments are validated by Pydantic; tools are bound to the current
`organization_id` and `claim_id` (e.g. `claims.history_for_customer` can only query the claim's own customer).

## 3. Key I/O schemas (abridged)

```python
class ExtractedField(BaseModel):
    field_name: str; value: str | None; value_normalized: JsonValue | None
    confidence: confloat(ge=0, le=1)
    source_document_id: UUID; page_number: PositiveInt
    evidence_excerpt: constr(max_length=500) | None
    validation_status: Literal["verified","unverified","low_confidence","conflict","corrected","rejected"]

class DocumentIntelligenceResult(BaseModel):
    document_id: UUID
    document_type: DocumentType; classification_confidence: float
    readability: Literal["good","degraded","unreadable"]
    fields: list[ExtractedField]
    duplicates: list[DuplicateMatch]     # exact sha256 / phash / simhash with distance
    dates: list[ExtractedField]; identifiers: list[ExtractedField]
    needs_human_verification: list[str]  # field names

class CoverageDetermination(BaseModel):
    coverage_code: str
    status: Literal["covered","potentially_covered","not_covered","insufficient_evidence","requires_manual_interpretation"]
    reasons: list[ReasonCode]; clause_ids: conlist(UUID, min_length=1)   # citation is mandatory

class PolicyVerificationResult(BaseModel):
    policy_version_id: UUID | None; version_selection_reason: str
    holder_match: Literal["match","partial","mismatch","unknown"]
    policy_active_on_incident_date: bool | None
    waiting_period_satisfied: bool | None
    coverages: list[CoverageDetermination]
    exclusions_triggered: list[ExclusionFinding]; exclusions_need_interpretation: list[ExclusionFinding]
    deductible: Money | None; limit_remaining: Money | None

class FraudIndicator(BaseModel):
    rule_code: str; source: Literal["rule","statistical","llm"]
    severity: Literal["low","medium","high"]; weight: int; confidence: float; contribution: float
    description: str; evidence: conlist(EvidenceRef, min_length=1); missing_evidence: list[str]

class FraudAnalysisResult(BaseModel):
    risk_score: conint(ge=0, le=100)
    risk_level: Literal["low","medium","high","critical"]
    indicators: list[FraudIndicator]
    supporting_evidence: list[EvidenceRef]; missing_evidence: list[str]
    recommended_action: Literal["continue","review","investigate"]
    framework_version: str

class AssessmentResult(BaseModel):
    currency: CurrencyCode
    line_items: list[AssessedLineItem]
    gross_claimed: Decimal; gross_eligible: Decimal; depreciation_total: Decimal
    deductible_applied: Decimal; co_pay_applied: Decimal
    limit_remaining_before: Decimal; eligible_payout: Decimal
    calculation_trace: list[CalcStep]    # op, inputs, output, rule ref
    status: Literal["calculated","insufficient_inputs"]

class DecisionRecommendationResult(BaseModel):
    recommendation: Recommendation; deterministic_outcome: Recommendation; llm_escalated: bool
    explanation: str; applied_rules: list[str]
    supporting_evidence: list[EvidenceRef]; contradictory_evidence: list[EvidenceRef]
    missing_information: list[str]; mandatory_review_reasons: list[str]
```

LLM-facing schemas are separate, narrower models (e.g. `LlmFieldCandidates`) converted into the domain models
only after validation, normalisation and grounding.

## 4. Extraction safety pipeline

1. LLM returns candidates `{field_name, value, page_number, evidence_excerpt, self_confidence}` as JSON.
2. Schema validation (strict). Invalid → repair prompt (max 2) → fallback model → fail.
3. **Grounding:** normalised `evidence_excerpt` must occur in the cited page's stored text (exact, or token-set
   ratio ≥ 0.90 for OCR noise) and `value` must be derivable from the excerpt. Otherwise `ungrounded` → discarded
   and reported as missing.
4. Normalisation by code (dates → ISO with locale hints from org settings; money → Decimal + currency; IDs → regex).
5. **Calibrated confidence** = min(self_confidence, OCR page confidence, grounding score, normaliser certainty).
   The UI labels this "model confidence", never "accuracy".
6. Below `agent_configs.confidence_threshold` (default 0.85) → `low_confidence` → reprocess once with the
   alternate OCR engine/model, then human verification.

## 5. Rules engine

Pure, side-effect-free package `rules_engine/` operating on typed snapshots. Every function returns
`(result, trace_steps)`.

| Module | Functions |
|--------|-----------|
| `money` | `Money`, `add/sub/mul_pct/min/cap`, `round_final(money, policy)`; ISO-4217 exponents; cross-currency raises |
| `policy_validity` | `select_version(versions, incident_date)`, `is_active(version, date)`, `waiting_period_ok(...)` |
| `coverage` | `evaluate_coverage(coverage, perils, facts)`, `evaluate_exclusion(predicate, facts)` → `triggered/not_triggered/needs_interpretation` |
| `calculation` | product calculators: `MotorOwnDamageCalculator`, `HealthMedicalCalculator` (config-driven order of operations) |
| `gates` | mandatory review conditions, STP eligibility |
| `fraud_scoring` | §6 |

**Motor own-damage order of operations (rule set v1):** eligible line items → depreciation on parts per age band
→ sum → subtract salvage → cap at sum insured remaining → subtract deductible (floor 0) → round once.
Example: gross eligible 5,000.00; deductible 500.00; limit remaining 10,000.00 → payout **4,500.00**.

**Health order:** eligible items capped by per-item sub-limits → sum → cap at annual aggregate remaining →
co-pay % → deductible → round once.

### 5.1 Decision matrix (deterministic outcome)

Evaluated top-down; first match wins.

| # | Condition | Outcome |
|---|-----------|---------|
| 1 | Mandatory inputs missing (required docs, policy version unresolved, assessment `insufficient_inputs`) | `MORE_INFORMATION_REQUIRED` |
| 2 | Low-confidence fields awaiting verification, or AV scan pending | `PENDING_VERIFICATION` |
| 3 | Any coverage `requires_manual_interpretation`, exclusion `needs_interpretation`, validation finding `critical`, fraud level `high/critical`, or agent contradiction detected | `MANUAL_REVIEW_REQUIRED` |
| 4 | Policy not active on incident date, or all claimed coverages `not_covered` with cited clauses, or exclusion `triggered` with cited clause | `REJECT_RECOMMENDED` (always human-reviewed) |
| 5 | Primary coverage `covered`, validation no `error`+, fraud `low`, payout > 0 | `APPROVE_RECOMMENDED` |
| 6 | Otherwise | `MANUAL_REVIEW_REQUIRED` |

The LLM step may change the outcome only along: `APPROVE_RECOMMENDED → MANUAL_REVIEW_REQUIRED` or
`REJECT_RECOMMENDED → MANUAL_REVIEW_REQUIRED`, and must cite the evidence for the escalation.

### 5.2 STP gate (auto-approval eligibility)

All required: org setting `stp.enabled`; compliance profile `automated_approval_allowed`; outcome
`APPROVE_RECOMMENDED` with no LLM escalation; fraud score < 25; payout ≤ `stp.max_amount` for the product;
all fields `verified`; no validation warnings above `info`; claimant not flagged in the last N months;
claim `is_synthetic` or AI data approval present. Re-evaluated from DB at `AUTO_APPROVAL_ELIGIBILITY`.

## 6. Fraud scoring framework v1

`score = min(100, round(Σ weight_i × confidence_i))`, where rule/statistical indicators have confidence 1.0 unless
the rule defines otherwise, and the **LLM-sourced contribution is capped at 15 points in total**. Weights live in the
rule set (versioned) and are documented in the UI next to each indicator.

| Code | Indicator | Source | Default weight |
|------|-----------|--------|----------------|
| F-DUP-CLAIM | Same policy, incident date ±3 days, open/closed claim exists | rule | 30 |
| F-DUP-DOC | Document SHA-256 or pHash (distance ≤ 6) matches a document on another claim | rule | 25 |
| F-DUP-INV | Invoice number + vendor reused across claims | rule | 25 |
| F-DATE-CONFLICT | Incident date differs across documents by > 1 day | rule | 15 |
| F-EARLY-CLAIM | Incident within 30 days of inception or reinstatement | rule | 10 |
| F-POST-EXPIRY-REPORT | Reported > 90 days after incident | rule | 5 |
| F-ID-MISMATCH | Name/ID/DOB mismatch between ID document and policy | rule | 20 |
| F-FREQ | ≥ 3 claims by the customer in 12 months | rule | 10 |
| F-AMOUNT-ANOM | Robust z-score (median/MAD) of claimed amount within product ≥ 3.5 (requires ≥ 30 baseline claims, else "insufficient baseline" info) | statistical | 15 |
| F-ESTIMATE-OUTLIER | Labour hours or part price above product P95 benchmark | statistical | 10 |
| F-DUP-LINE | Duplicate line items within/between invoices | rule | 10 |
| F-INJECTION | Document contains instruction-like text targeting automated systems | rule | 10 |
| F-NARRATIVE | Narrative inconsistency between description, reports and photos | llm | ≤ 15 (cap) |

Levels: `low < 25 ≤ medium < 50 ≤ high < 75 ≤ critical`. `medium` → reviewer sees indicators; `high/critical`
→ mandatory manual review and fraud investigation record. **No score leads to automated rejection.**
False-positive rate is measured from reviewer outcomes (`fraud_investigations.outcome_label`).

## 7. Per-agent quality gates (Phase 6 acceptance)

| Agent | Evaluation set | Acceptance (synthetic set) |
|-------|----------------|----------------------------|
| Document | 60 synthetic docs (13 types, clean/degraded/rotated/handwritten-lite) with labelled fields | classification ≥ 95%; field exact-match ≥ 90% on clean, reported separately on degraded; 0 ungrounded fields accepted |
| Policy | 40 scenarios (expired, waiting period, exclusion, multiple versions) | 100% correct version selection; 100% clause citations valid; 0 "covered" without clause |
| Fraud | 50 labelled scenarios | every planted indicator detected; score reproducible |
| Assessment | 80 calculation cases + property-based tests | 100% exact Decimal match |
| Validation | 40 contradiction cases | all critical contradictions detected |
| Decision | 60 scenarios | deterministic outcome 100% matches matrix; escalation never upgrades |
| Injection | 25 adversarial docs | 0 instruction-following; outcomes unchanged or escalated |
