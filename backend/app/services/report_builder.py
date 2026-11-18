"""Claim report content (Phase 11): one JSON document, rendered as PDF by ``report_pdf``.

Everything comes from persisted rows: the claim, its policy version and cited clauses, documents and
current extracted fields, the latest output of each agent, the decisions and the status history. Every
statement that rests on evidence cites entries of one ``evidence`` index (``E1``, ``E2`` ...), each of
which points at a document page, a field, a policy clause, a fraud indicator or a line item.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agents import AgentOutput, AgentRun
from app.models.catalog import Customer, InsuranceProduct, Policy, PolicyClause, PolicyVersion
from app.models.claims import Claim, ClaimStatusChange
from app.models.documents import ClaimDocument, ExtractedField
from app.models.identity import User
from app.models.rules import ClaimDecision

TEMPLATE_VERSION = "claim-report-v1"
AGENTS = ("intake", "document", "policy", "fraud", "assessment", "validation", "decision", "compliance")
TOTALS = (
    "gross_claimed",
    "gross_eligible",
    "depreciation_total",
    "deductible_applied",
    "co_pay_applied",
    "limit_capped_amount",
    "eligible_payout",
)


class Evidence:
    """Deduplicating evidence index: the same page, field or clause always gets the same label."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self._seen: dict[tuple[str, str, int | None], str] = {}

    def cite(
        self,
        kind: str,
        ref_id: str,
        *,
        page: int | None = None,
        excerpt: str | None = None,
        label: str | None = None,
    ) -> str:
        key = (kind, str(ref_id), page)
        if key not in self._seen:
            ref = f"E{len(self.items) + 1}"
            self._seen[key] = ref
            self.items.append(
                {
                    "ref": ref,
                    "kind": kind,
                    "id": str(ref_id),
                    "page": page,
                    "excerpt": (excerpt or "")[:300] or None,
                    "label": label,
                }
            )
        return self._seen[key]

    def refs(self, evidence: list[dict[str, Any]] | None) -> list[str]:
        return [
            self.cite(
                e.get("kind", "document"),
                e.get("id", "?"),
                page=e.get("page_number"),
                excerpt=e.get("excerpt"),
            )
            for e in evidence or []
        ]


def _s(value: Any) -> str | None:
    return None if value is None else str(value)


async def _latest_outputs(
    db: AsyncSession, claim_id: uuid.UUID
) -> dict[str, tuple[AgentRun, dict[str, Any]]]:
    rows = (
        await db.execute(
            select(AgentRun, AgentOutput.output)
            .join(AgentOutput, AgentOutput.agent_run_id == AgentRun.id)
            .where(AgentRun.claim_id == claim_id)
            .order_by(AgentRun.started_at)
        )
    ).all()
    return {run.agent_key: (run, dict(out)) for run, out in rows}  # later runs win


async def _fields(db: AsyncSession, ev: Evidence, documents: list[ClaimDocument]) -> list[dict[str, Any]]:
    names = {d.id: d.original_filename for d in documents}
    rows = await db.scalars(
        select(ExtractedField)
        .where(ExtractedField.document_id.in_(list(names)), ExtractedField.is_current.is_(True))
        .order_by(ExtractedField.document_id, ExtractedField.field_name)
    )
    return [
        {
            "field": f.field_name,
            "value": f.value_text,
            "document": names.get(f.document_id),
            "page": f.page_number,
            "source": f.source,
            "status": f.validation_status,
            "confidence": round(f.confidence, 3),
            "evidence": [
                ev.cite(
                    "page",
                    str(f.document_id),
                    page=f.page_number,
                    excerpt=f.evidence_excerpt,
                    label=f"{names.get(f.document_id)} p.{f.page_number}",
                )
            ],
        }
        for f in rows
    ]


def _assessment(ev: Evidence, asmt: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": asmt.get("status"),
        "calculator": asmt.get("calculator"),
        "rule_set_version": asmt.get("rule_set_version"),
        "currency": asmt.get("currency"),
        "totals": {k: _s(asmt.get(k)) for k in TOTALS},
        "line_items": [
            {
                "line": li.get("line_no"),
                "description": li.get("description"),
                "category": li.get("category"),
                "claimed": _s(li.get("claimed_amount")),
                "eligible": _s(li.get("eligible_amount")),
                "depreciation_pct": _s(li.get("depreciation_pct")),
                "status": li.get("status"),
                "evidence": ev.refs(li.get("evidence")),
            }
            for li in asmt.get("line_items", [])
        ],
        "calculation": [
            {"op": s.get("op"), "output": s.get("output"), "rule": s.get("rule_ref")}
            for s in asmt.get("calculation_trace", [])
        ],
    }


async def build(db: AsyncSession, claim_id: uuid.UUID, *, version_no: int) -> dict[str, Any]:
    claim = await db.get(Claim, claim_id)
    assert claim is not None
    ev = Evidence()
    policy = await db.get(Policy, claim.policy_id)
    version = await db.get(PolicyVersion, claim.policy_version_id) if claim.policy_version_id else None
    customer = await db.get(Customer, claim.customer_id)
    product = await db.get(InsuranceProduct, claim.product_id)
    outputs = await _latest_outputs(db, claim.id)
    out = {k: v[1] for k, v in outputs.items()}
    documents = list(
        (
            await db.scalars(
                select(ClaimDocument)
                .where(ClaimDocument.claim_id == claim.id, ClaimDocument.deleted_at.is_(None))
                .order_by(ClaimDocument.created_at)
            )
        ).all()
    )
    field_rows = await _fields(db, ev, documents)
    clauses: dict[str, PolicyClause] = {}
    if version is not None:
        clause_rows = await db.scalars(
            select(PolicyClause).where(PolicyClause.policy_version_id == version.id)
        )
        clauses = {c.clause_ref: c for c in clause_rows}

    def clause_refs(ids: list[str]) -> list[str]:
        refs = []
        for cid in ids:
            c = clauses.get(cid)
            label = f"Clause {cid}" + (f" {c.title}" if c else "")
            refs.append(ev.cite("clause", cid, excerpt=c.text if c else None, label=label))
        return refs

    pol = out.get("policy", {})
    coverage = {
        "policy_active_on_incident_date": pol.get("policy_active_on_incident_date"),
        "holder_match": pol.get("holder_match"),
        "coverages": [
            {
                "code": c.get("coverage_code"),
                "status": c.get("status"),
                "reasons": c.get("reasons", []),
                "evidence": clause_refs(c.get("clause_ids", [])),
            }
            for c in pol.get("coverages", [])
        ],
        "exclusions_triggered": [
            {
                "code": x.get("exclusion_code"),
                "reason": x.get("reason"),
                "evidence": clause_refs([x.get("clause_id", "?")]),
            }
            for x in pol.get("exclusions_triggered", [])
        ],
    }
    fr = out.get("fraud", {})
    fraud = {
        "risk_score": fr.get("risk_score"),
        "risk_level": fr.get("risk_level"),
        "recommended_action": fr.get("recommended_action"),
        "indicators": [
            {
                "code": i.get("rule_code"),
                "severity": i.get("severity"),
                "description": i.get("description"),
                "contribution": i.get("contribution"),
                "evidence": ev.refs(i.get("evidence")),
            }
            for i in fr.get("indicators", [])
        ],
    }
    assessment = _assessment(ev, out.get("assessment", {}))
    validation = [
        {
            "check": f.get("check_code"),
            "severity": f.get("severity"),
            "message": f.get("message"),
            "evidence": ev.refs(f.get("evidence")),
        }
        for f in out.get("validation", {}).get("findings", [])
    ]
    dec = out.get("decision", {})
    recommendation = {
        "recommendation": dec.get("recommendation"),
        "matrix_rule": dec.get("matrix_rule"),
        "explanation": dec.get("explanation"),
        "mandatory_review_reasons": dec.get("mandatory_review_reasons", []),
        "evidence": ev.refs(dec.get("supporting_evidence")),
        "contradicting": ev.refs(dec.get("contradictory_evidence")),
    }
    decisions = list(
        (
            await db.scalars(
                select(ClaimDecision)
                .where(ClaimDecision.claim_id == claim.id)
                .order_by(ClaimDecision.created_at)
            )
        ).all()
    )
    people_ids = [x for d in decisions for x in (d.decided_by, d.second_approver_id) if x]
    people = {u.id: u.display_name for u in await db.scalars(select(User).where(User.id.in_(people_ids)))}
    decision_rows = [
        {
            "decision": d.decision,
            "mode": d.decision_mode,
            "approved_amount": _s(d.approved_amount),
            "currency": d.currency,
            "decided_by": people.get(d.decided_by) if d.decided_by else "straight-through processing",
            "second_approver": people.get(d.second_approver_id) if d.second_approver_id else None,
            "reason": d.reason,
            "reason_code": d.reason_code,
            "override": d.override,
            "override_reason": d.override_reason,
            "evidence": clause_refs(list(d.clause_ids)),
            "failed_gates": [g.get("code") for g in d.gates if not g.get("passed")],
            "at": d.created_at.isoformat(),
        }
        for d in decisions
    ]
    changes = await db.scalars(
        select(ClaimStatusChange)
        .where(ClaimStatusChange.claim_id == claim.id)
        .order_by(ClaimStatusChange.changed_at)
    )
    history = [
        {"from": h.from_status, "to": h.to_status, "reason": h.reason, "at": h.changed_at.isoformat()}
        for h in changes
    ]
    runs = [
        {
            "agent": key,
            "run_id": str(run.id),
            "status": run.status,
            "model": run.model_slug,
            "rule_set_version": run.rule_set_version,
            "used_model": run.used_llm,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }
        for key in AGENTS
        if key in outputs
        for run in (outputs[key][0],)
    ]
    return {
        "report": {
            "template_version": TEMPLATE_VERSION,
            "version_no": version_no,
            "generated_at": datetime.now(UTC).isoformat(),
            "synthetic": claim.is_synthetic,
        },
        "claim": {
            "reference": claim.claim_reference,
            "status": claim.status,
            "product": product.name if product else None,
            "loss_type": claim.category,
            "incident_date": claim.incident_date.isoformat(),
            "reported_at": claim.reported_at.isoformat(),
            "claimed_amount": _s(claim.claimed_amount),
            "currency": claim.currency,
            "description": claim.description,
            "customer": customer.full_name if customer else None,
        },
        "policy": {
            "policy_number": policy.policy_number if policy else None,
            "version_no": version.version_no if version else None,
            "effective_from": version.effective_from.isoformat() if version else None,
            "effective_to": version.effective_to.isoformat() if version else None,
            "sum_insured": _s(version.sum_insured) if version else None,
            "deductible": _s(version.deductible_amount) if version else None,
        },
        "documents": [
            {
                "id": str(d.id),
                "filename": d.original_filename,
                "type": d.document_type,
                "pages": d.page_count,
                "quality": d.quality_score,
                "sha256": d.sha256,
            }
            for d in documents
        ],
        "fields": field_rows,
        "coverage": coverage,
        "fraud": fraud,
        "assessment": assessment,
        "validation": validation,
        "recommendation": recommendation,
        "decisions": decision_rows,
        "status_history": history,
        "agent_runs": runs,
        "evidence": ev.items,
    }
