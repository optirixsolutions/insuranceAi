"""Claim assessment agent.

Line items come from the itemised document text (grounded by construction). The model (``fast``)
may only re-categorise items the rules left as ``other``; it never sees or returns amounts. All
arithmetic is in ``rules_engine.calculation`` (Decimal, single rounding, full trace).
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from agents.base import Agent, AgentContext, rule_config, rule_label
from agents.contracts import (
    AgentResult,
    AssessedLineItemOut,
    AssessmentOutput,
    CalcStepOut,
    EvidenceRef,
    Finding,
    MissingItem,
)
from agents.injection import detect
from agents.snapshot import ClaimSnap, DocumentSnap, PolicySnap, PriorOutput
from document_processing.line_items import CATEGORIES, ParsedLine, parse_line_items
from rules_engine.calculation import (
    AssessedItem,
    ItemStatus,
    LineItem,
    health_medical,
    motor_own_damage,
)

ITEMISED = ("repair_invoice", "medical_bill", "repair_estimate")


class LlmLineCategory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_no: int
    category: str


class LlmLineCategories(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[LlmLineCategory]


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        return None


def itemised_documents(documents: list[DocumentSnap]) -> list[DocumentSnap]:
    """Invoices/bills supersede estimates: if an invoice exists, estimates are not double-counted."""
    docs = [d for d in documents if d.document_type in ITEMISED and d.scan_status in ("clean", "skipped")]
    final = [d for d in docs if d.document_type in ("repair_invoice", "medical_bill")]
    return final or docs


def vehicle_age(
    claim: ClaimSnap, documents: list[DocumentSnap], endorsements: list[dict[str, Any]]
) -> int | None:
    year: int | None = None
    for d in documents:
        for f in d.fields:
            if f.field_name == "vehicle_year" and f.value_text and f.value_text.isdigit():
                year = int(f.value_text)
                break
    if year is None:
        for e in endorsements:
            if str(e.get("vehicle_year", "")).isdigit():
                year = int(e["vehicle_year"])
    if year is None or year > claim.incident_date.year:
        return None
    return claim.incident_date.year - year


class AssessmentAgent(Agent):
    key: ClassVar[str] = "assessment"
    uses_llm: ClassVar[bool] = True
    allowed_tools: ClassVar[frozenset[str]] = frozenset(
        {"claim.get", "policy.lookup", "claim.documents", "agents.prior_outputs"}
    )

    async def run(self, ctx: AgentContext) -> AgentResult:
        claim: ClaimSnap = ctx.tools("claim.get")
        config = rule_config(ctx.snapshot)
        policy: PolicySnap = ctx.tools("policy.lookup")
        documents: list[DocumentSnap] = ctx.tools("claim.documents")
        prior: dict[str, PriorOutput] = ctx.tools("agents.prior_outputs")
        pv = prior.get("policy")
        pdata = (pv.data if pv else None) or {}
        version = next((v for v in policy.versions if v.id == pdata.get("policy_version_id")), None)
        missing: list[MissingItem] = []
        if not pdata or version is None:
            missing.append(
                MissingItem(
                    item="policy verification",
                    reason="deductible, limits and coverage come from the verified policy version",
                )
            )

        parsed: list[tuple[DocumentSnap, ParsedLine]] = []
        for d in itemised_documents(documents):
            parsed += [
                (d, p) for p in parse_line_items([(pg.page_number, pg.text) for pg in d.pages], claim.line)
            ]
        if not parsed:
            missing.append(
                MissingItem(
                    item="itemised estimate, invoice or bill",
                    reason="line items are calculated from document text only",
                )
            )

        categories = {(d.id, p.line_no): p.category for d, p in parsed}
        sources = {k: "rules" for k in categories}
        allowed = CATEGORIES.get(claim.line, CATEGORIES["motor"])
        unknown = [(i, d, p) for i, (d, p) in enumerate(parsed, start=1) if p.category == "other"]
        used_llm = False
        if unknown:
            numbers = {i for i, _, _ in unknown}

            def validate(r: LlmLineCategories) -> list[str]:
                return [
                    f"line {x.line_no} unknown or category '{x.category}' not allowed"
                    for x in r.items
                    if x.line_no not in numbers or x.category not in allowed
                ]

            user = (
                "<line_items>\n"
                + json.dumps([{"line_no": i, "description": p.description} for i, _, p in unknown])
                + f"\n</line_items>\n<allowed_categories>{json.dumps(list(allowed))}</allowed_categories>"
            )
            answer = await self.ask(ctx, LlmLineCategories, user, validate)
            if answer:
                used_llm = True
                by_no = {x.line_no: x.category for x in answer.items}
                for i, d, p in unknown:
                    if i in by_no:
                        categories[(d.id, p.line_no)], sources[(d.id, p.line_no)] = by_no[i], "llm"

        primary = pdata.get("primary_coverage")
        seen: set[tuple[str, Decimal]] = set()
        items: list[LineItem] = []
        for n, (d, p) in enumerate(parsed, start=1):
            key = (" ".join(p.description.lower().split()), p.amount)
            status: ItemStatus = "eligible"
            reason: str | None = None
            if detect(p.excerpt):  # text addressed to automated systems is never a payable item
                status, reason = "unsupported", "INSTRUCTION_LIKE_TEXT"
            elif key in seen:
                status, reason = "duplicate", "DUPLICATE_LINE"
            elif p.currency and p.currency != claim.currency:
                status, reason = "unsupported", "CURRENCY_MISMATCH"
            elif primary is None:
                status, reason = "ineligible", "NO_APPLICABLE_COVERAGE"
            seen.add(key)
            items.append(
                LineItem(
                    n,
                    p.description,
                    categories[(d.id, p.line_no)],
                    p.amount,
                    status,
                    reason,
                    primary,
                    (d.id,),
                )
            )

        deductible = _decimal((pdata.get("deductible") or {}).get("amount")) or Decimal("0")
        limit = _decimal((pdata.get("limit_remaining") or {}).get("amount"))
        if missing:
            limit = None
        endorsements = version.endorsements if version else []
        if claim.line == "health":
            cov = next(
                (c for c in (version.coverages if version else []) if c.coverage_code == primary), None
            )
            sub_limits = {**config.health_sub_limits} | {
                str(e["category"]): amt
                for e in endorsements
                if e.get("type") == "sub_limit" and (amt := _decimal(e.get("amount"))) is not None
            }
            calc = health_medical(
                currency=claim.currency,
                items=items,
                sub_limits=sub_limits,
                aggregate_remaining=limit,
                co_pay_pct=(cov.co_pay_pct if cov and cov.co_pay_pct is not None else Decimal("0")),
                deductible=deductible,
            )
            calculator = "health_medical"
        else:
            calc = motor_own_damage(
                currency=claim.currency,
                items=items,
                vehicle_age_years=vehicle_age(claim, documents, endorsements),
                salvage=Decimal("0"),
                limit_remaining=limit,
                deductible=deductible,
                depreciation=config.depreciation_table(),
                rule_ref=rule_label(ctx.snapshot),
            )
            calculator = "motor_own_damage"
        missing += [
            MissingItem(item=m, reason="required by the calculator")
            for m in calc.missing
            if not any(m in x.item for x in missing)
        ]

        line_out = []
        assessed_items: list[AssessedItem | None] = [*calc.items] or [None] * len(parsed)
        for (d, p), assessed in zip(parsed, assessed_items, strict=False):
            item = assessed.item if assessed else None
            line_out.append(
                AssessedLineItemOut(
                    line_no=item.line_no if item else p.line_no,
                    description=p.description,
                    category=categories[(d.id, p.line_no)],
                    category_source=sources[(d.id, p.line_no)],
                    coverage_code=primary,
                    claimed_amount=p.amount,
                    eligible_amount=assessed.eligible if assessed else Decimal("0"),
                    depreciation_pct=assessed.depreciation_pct if assessed else Decimal("0"),
                    status=item.status if item else "eligible",
                    reason_code=item.reason_code if item else None,
                    evidence=[
                        EvidenceRef(kind="page", id=d.id, page_number=p.page_number, excerpt=p.excerpt)
                    ],
                )
            )
        documented = sum((p.amount for _, p in parsed), Decimal("0"))
        out = AssessmentOutput(
            currency=claim.currency,
            calculator=calculator,
            rule_set_version=rule_label(ctx.snapshot),
            line_items=line_out,
            gross_claimed=calc.gross_claimed,
            gross_eligible=calc.gross_eligible,
            depreciation_total=calc.depreciation_total,
            salvage=calc.salvage,
            deductible_applied=calc.deductible_applied,
            co_pay_applied=calc.co_pay_applied,
            limit_remaining_before=calc.limit_remaining_before,
            limit_capped_amount=calc.limit_capped_amount,
            eligible_payout=calc.eligible_payout,
            calculation_trace=[CalcStepOut(**s.as_dict()) for s in calc.trace],
            status=calc.status if not missing else "insufficient_inputs",
            claimed_vs_documented=(claim.claimed_amount - documented) if parsed else None,
        )
        findings: list[Finding] = []
        if any(i.status == "duplicate" for i in items):
            findings.append(
                Finding(
                    code="DUPLICATE_LINE", severity="warning", message="Duplicate line items were excluded"
                )
            )
        if parsed and abs(claim.claimed_amount - documented) > Decimal("0.01"):
            findings.append(
                Finding(
                    code="CLAIMED_VS_DOCUMENTED",
                    severity="warning",
                    message=f"Claimed {claim.claimed_amount} vs itemised {documented}",
                )
            )
        run_status: Literal["succeeded", "partial"] = "succeeded" if out.status == "calculated" else "partial"
        return AgentResult(
            status=run_status,
            data=out.model_dump(mode="json"),
            findings=findings,
            missing=missing,
            used_llm=used_llm,
        )
