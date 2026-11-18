"""Fraud investigation agent: rule and statistical indicators, plus a capped narrative review.

Every indicator cites evidence. The model (``reasoning``) may add F-NARRATIVE indicators only
with a verbatim excerpt; their total contribution is capped at 15 points by the framework.
Checks that cannot run (no baseline, no benchmarks) are listed, never silently passed.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from agents.base import Agent, AgentContext, rule_config
from agents.contracts import AgentResult, EvidenceRef, Finding, FraudAnalysisOutput, FraudIndicatorOut
from agents.injection import detect
from agents.intake.agent import version_terms
from agents.snapshot import ClaimRef, ClaimSnap, CustomerSnap, DocumentSnap, InvoiceRef, PolicySnap
from agents.validation.agent import _date, _ident
from document_processing.line_items import parse_line_items
from rules_engine import fraud_scoring as fs
from rules_engine.intake import DUPLICATE_WINDOW_DAYS, LATE_REPORT_DAYS
from rules_engine.policy_validity import inception
from rules_engine.text import excerpt_in_text, names_match

MIN_BASELINE = 30
Z_THRESHOLD = Decimal("3.5")
EARLY_DAYS = 30
FREQ_COUNT = 3


class LlmInconsistency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str
    evidence_excerpt: str = Field(description="Verbatim text from the description or a document")
    confidence: float = Field(ge=0, le=1)


class LlmNarrativeReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inconsistencies: list[LlmInconsistency]


def robust_z(value: Decimal, baseline: list[Decimal]) -> Decimal | None:
    median = Decimal(statistics.median(baseline))
    mad = Decimal(statistics.median([abs(b - median) for b in baseline]))
    if mad == 0:
        return None
    return Decimal("0.6745") * (value - median) / mad


class FraudAgent(Agent):
    key: ClassVar[str] = "fraud"
    uses_llm: ClassVar[bool] = True
    allowed_tools: ClassVar[frozenset[str]] = frozenset(
        {
            "claim.get",
            "customer.get",
            "policy.lookup",
            "claims.history_for_customer",
            "claims.history_for_policy",
            "claims.invoice_numbers",
            "claim.documents",
            "stats.amount_baseline",
        }
    )

    async def run(self, ctx: AgentContext) -> AgentResult:
        claim: ClaimSnap = ctx.tools("claim.get")
        customer: CustomerSnap = ctx.tools("customer.get")
        policy: PolicySnap = ctx.tools("policy.lookup")
        by_customer: list[ClaimRef] = ctx.tools("claims.history_for_customer")
        by_policy: list[ClaimRef] = ctx.tools("claims.history_for_policy")
        invoices: list[InvoiceRef] = ctx.tools("claims.invoice_numbers")
        documents: list[DocumentSnap] = ctx.tools("claim.documents")
        baseline: list[Decimal] = ctx.tools("stats.amount_baseline")
        claim_ev = EvidenceRef(kind="claim", id=claim.id)
        found: list[fs.Indicator] = []
        not_run: list[str] = []

        def add(
            code: str,
            description: str,
            evidence: list[EvidenceRef],
            confidence: Decimal = Decimal("1"),
            missing: tuple[str, ...] = (),
        ) -> None:
            found.append(
                fs.Indicator(
                    code,
                    description,
                    tuple(e.model_dump() for e in evidence) or (claim_ev.model_dump(),),
                    confidence,
                    missing,
                )
            )

        window = timedelta(days=DUPLICATE_WINDOW_DAYS)
        dups = [
            c for c in by_policy if c.id != claim.id and abs(c.incident_date - claim.incident_date) <= window
        ]
        if dups:
            add(
                "F-DUP-CLAIM",
                f"Other claim(s) on this policy within ±3 days: {', '.join(c.claim_reference for c in dups)}",
                [EvidenceRef(kind="claim", id=c.id, excerpt=c.claim_reference) for c in dups],
            )

        cross = [
            (d, m)
            for d in documents
            for m in d.duplicates
            if not m.get("same_claim") and m.get("claim_id") != claim.id
        ]
        if cross:
            add(
                "F-DUP-DOC",
                "Document(s) match documents filed on other claims: "
                + ", ".join(sorted({str(m.get("claim_reference")) for _, m in cross})),
                [
                    EvidenceRef(
                        kind="document",
                        id=d.id,
                        excerpt=(
                            f"{m.get('method')} match with {m.get('claim_reference')} "
                            f"(distance {m.get('distance')})"
                        ),
                    )
                    for d, m in cross
                ],
            )

        mine = [
            (d, f)
            for d in documents
            for f in d.fields
            if f.field_name in ("invoice_number", "estimate_number") and f.value_text
        ]
        reused = [
            (d, f, o)
            for d, f in mine
            for o in invoices
            if o.claim_id != claim.id and _ident(o.invoice_number) == _ident(f.value_text)
        ]
        if reused:
            add(
                "F-DUP-INV",
                "Invoice or estimate number(s) already used on other claims: "
                + ", ".join(sorted({o.claim_reference for _, _, o in reused})),
                [
                    EvidenceRef(kind="field", id=f.id, page_number=f.page_number, excerpt=f.evidence_excerpt)
                    for _, f, _ in reused
                ],
            )

        dated = [
            (d, f, v)
            for d in documents
            for f in d.fields
            if f.field_name == "incident_date" and (v := _date(f.value_normalized))
        ]
        conflicts = [(d, f, v) for d, f, v in dated if abs((v - claim.incident_date).days) > 1]
        if conflicts or (dated and (max(v for *_, v in dated) - min(v for *_, v in dated)).days > 1):
            add(
                "F-DATE-CONFLICT",
                "Incident date differs across documents or from the claim by more than a day",
                [
                    EvidenceRef(kind="field", id=f.id, page_number=f.page_number, excerpt=f.evidence_excerpt)
                    for _, f, _ in (conflicts or dated)
                ],
            )

        start = inception(version_terms(policy))
        if start and timedelta(0) <= claim.incident_date - start <= timedelta(days=EARLY_DAYS):
            add(
                "F-EARLY-CLAIM",
                f"Incident {(claim.incident_date - start).days} day(s) after cover started ({start})",
                [claim_ev],
            )

        if (claim.reported_at.date() - claim.incident_date).days > LATE_REPORT_DAYS:
            add(
                "F-POST-EXPIRY-REPORT",
                f"Reported {(claim.reported_at.date() - claim.incident_date).days} days after the incident",
                [claim_ev],
            )

        mismatched = [
            (d, f)
            for d in documents
            for f in d.fields
            if (
                f.field_name in ("policyholder_name", "patient_name")
                and f.value_text
                and not names_match(f.value_text, customer.full_name)
            )
            or (
                f.field_name == "date_of_birth"
                and customer.date_of_birth
                and _date(f.value_normalized) not in (None, customer.date_of_birth)
            )
        ]
        if mismatched:
            add(
                "F-ID-MISMATCH",
                "Name or date of birth on the documents does not match the policyholder",
                [
                    EvidenceRef(kind="field", id=f.id, page_number=f.page_number, excerpt=f.evidence_excerpt)
                    for _, f in mismatched
                ],
            )

        year = [
            c
            for c in by_customer
            if c.id != claim.id
            and timedelta(0) <= claim.incident_date - c.incident_date <= timedelta(days=365)
        ]
        if len(year) + 1 >= FREQ_COUNT:
            add(
                "F-FREQ",
                f"{len(year) + 1} claims by this customer in 12 months",
                [EvidenceRef(kind="claim", id=c.id, excerpt=c.claim_reference) for c in year],
            )

        if len(baseline) >= MIN_BASELINE:
            z = robust_z(claim.claimed_amount, baseline)
            if z is not None and z >= Z_THRESHOLD:
                add(
                    "F-AMOUNT-ANOM",
                    f"Claimed amount is {z:.1f} robust standard deviations above the product median",
                    [claim_ev],
                )
        else:
            not_run.append(f"F-AMOUNT-ANOM: insufficient baseline ({len(baseline)} < {MIN_BASELINE} claims)")
        not_run.append("F-ESTIMATE-OUTLIER: no labour/parts benchmarks configured for this product")

        lines = [
            (d, p)
            for d in documents
            for p in parse_line_items([(pg.page_number, pg.text) for pg in d.pages], claim.line)
        ]
        counts = Counter((" ".join(p.description.lower().split()), p.amount) for _, p in lines)
        repeated = [
            (d, p) for d, p in lines if counts[(" ".join(p.description.lower().split()), p.amount)] > 1
        ]
        if repeated:
            add(
                "F-DUP-LINE",
                "Line items repeated within or between invoices",
                [
                    EvidenceRef(kind="line_item", id=d.id, page_number=p.page_number, excerpt=p.excerpt)
                    for d, p in repeated
                ],
            )

        hits = [(d, pg, h) for d in documents for pg in d.pages for h in detect(pg.text)]
        desc_hits = detect(claim.description)
        if hits or desc_hits:
            add(
                "F-INJECTION",
                "Text addressed to automated systems was found (treated as data, flagged for review)",
                [
                    EvidenceRef(kind="page", id=d.id, page_number=pg.page_number, excerpt=h.excerpt)
                    for d, pg, h in hits
                ]
                + [EvidenceRef(kind="claim", id=claim.id, excerpt=h.excerpt) for h in desc_hits],
            )

        source_text = "\n".join([claim.description, *(pg.text for d in documents for pg in d.pages)])

        def validate(r: LlmNarrativeReview) -> list[str]:
            return [
                f"evidence '{i.evidence_excerpt[:60]}' is not verbatim text from the claim"
                for i in r.inconsistencies
                if not excerpt_in_text(i.evidence_excerpt, source_text)
            ]

        narrative = None
        if documents:
            docs_text = "\n\n".join(
                f'<document name="{d.filename}" type="{d.document_type}">\n'
                f"{' '.join(p.text for p in d.pages)[:1500]}\n</document>"
                for d in documents
            )
            user = (
                "<claim>"
                + json.dumps({"loss_type": claim.category, "incident_date": claim.incident_date.isoformat()})
                + "</claim>\n"
                + f"<loss_description>\n{claim.description}\n</loss_description>\n"
                + f"<documents>\n{docs_text}\n</documents>"
            )
            narrative = await self.ask(ctx, LlmNarrativeReview, user, validate)
        for inc in (narrative.inconsistencies if narrative else [])[:5]:
            add(
                "F-NARRATIVE",
                inc.description[:300],
                [EvidenceRef(kind="claim", id=claim.id, excerpt=inc.evidence_excerpt[:500])],
                Decimal(str(inc.confidence)),
            )

        config = rule_config(ctx.snapshot)
        weights = {i.rule_code: config.weight(i.rule_code) for i in found}
        result = fs.score(found, weights, Decimal(config.fraud_llm_cap))
        indicators = [
            FraudIndicatorOut(
                rule_code=i.rule_code,
                source=i.source,
                severity=i.severity,
                weight=weights[i.rule_code],
                confidence=float(i.confidence),
                contribution=float(result.contributions[n]),
                description=i.description,
                evidence=[EvidenceRef(**e) for e in i.evidence],
                missing_evidence=list(i.missing_evidence),
            )
            for n, i in enumerate(found)
        ]
        out = FraudAnalysisOutput(
            risk_score=result.score,
            risk_level=result.level,
            indicators=indicators,
            supporting_evidence=[e for i in indicators for e in i.evidence][:50],
            missing_evidence=[],
            recommended_action=result.recommended_action,
            framework_version=result.framework_version,
            checks_not_run=not_run,
        )
        findings = [
            Finding(
                code=i.rule_code,
                severity="warning" if i.severity != "high" else "error",
                message=i.description,
                evidence=i.evidence[:5],
            )
            for i in indicators
        ]
        return AgentResult(
            status="succeeded",
            data=out.model_dump(mode="json"),
            findings=findings,
            used_llm=narrative is not None,
            escalate=result.level in ("high", "critical")
            or any(i.rule_code == "F-INJECTION" for i in indicators),
        )
