"""Portfolio anomalies (Phase 12): patterns across claims that a single claim's fraud analysis cannot see.

Each detector is one query over the claims the caller may read, returns the claims involved and says in
plain words why they were flagged. Nothing here changes a claim, a score or a decision: anomalies are
leads for a person.

* ``amount_outlier``: claimed amount with a robust z-score >= 3.5 within its product (median and median
  absolute deviation over the period; needs >= 30 claims, otherwise reported as an insufficient baseline).
* ``frequent_claimant``: a customer with >= 3 claims reported within 90 days.
* ``similar_narrative``: two claims of *different* customers whose descriptions are near-identical
  (embedding cosine similarity >= 0.9): re-used or templated stories.
* ``shared_document``: the same file (SHA-256) attached to more than one claim.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Float, and_, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import InsuranceProduct
from app.models.claims import Claim
from app.models.documents import ClaimDocument
from app.services.metrics_common import Scope
from app.services.similarity import embeddings

Z_THRESHOLD = 3.5
MIN_BASELINE = 30
FREQUENT = 3
NARRATIVE_SIMILARITY = 0.9


def _ref(c: Claim) -> dict[str, str]:
    return {"claim_id": str(c.id), "claim_reference": c.claim_reference}


async def amount_outliers(db: AsyncSession, scope: Scope) -> tuple[list[dict[str, Any]], list[str]]:
    seen = scope.visible(Claim.id)
    recent = Claim.reported_at >= scope.since
    base = (
        select(
            Claim.product_id,
            func.count().label("n"),
            func.percentile_cont(0.5).within_group(Claim.claimed_amount).label("median"),
        )
        .where(seen, recent)
        .group_by(Claim.product_id)
        .subquery()
    )
    mad_q = (
        select(
            Claim.product_id,
            func.percentile_cont(0.5)
            .within_group(func.abs(Claim.claimed_amount - base.c.median))
            .label("mad"),
        )
        .join(base, base.c.product_id == Claim.product_id)
        .where(seen, recent)
        .group_by(Claim.product_id)
        .subquery()
    )
    stats = {
        pid: (int(n), float(med), float(mad or 0), name)
        for pid, n, med, mad, name in (
            await db.execute(
                select(base.c.product_id, base.c.n, base.c.median, mad_q.c.mad, InsuranceProduct.name)
                .join(mad_q, mad_q.c.product_id == base.c.product_id)
                .join(InsuranceProduct, InsuranceProduct.id == base.c.product_id)
            )
        ).all()
    }
    notes, found = [], []
    for pid, (n, med, mad, name) in stats.items():
        if n < MIN_BASELINE:
            notes.append(
                f"{name}: {n} claims in the period, below the {MIN_BASELINE} needed for an amount baseline"
            )
            continue
        if mad == 0:
            continue
        rows = await db.scalars(select(Claim).where(seen, recent, Claim.product_id == pid))
        for c in rows:
            z = 0.6745 * (float(c.claimed_amount) - med) / mad
            if abs(z) >= Z_THRESHOLD:
                found.append(
                    {
                        "type": "amount_outlier",
                        "severity": "high" if abs(z) >= 2 * Z_THRESHOLD else "medium",
                        "score": round(z, 2),
                        "claims": [_ref(c)],
                        "explanation": f"Claimed {c.claimed_amount} {c.currency} against a median of "
                        f"{med:.2f} in "
                        f"{name} (robust z-score {z:.1f}, threshold {Z_THRESHOLD}).",
                    }
                )
    return found, notes


async def frequent_claimants(db: AsyncSession, scope: Scope) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(days=90)  # "within 90 days", whatever the dashboard period
    rows = (
        await db.execute(
            select(Claim.customer_id, func.count(), func.array_agg(Claim.id))
            .where(scope.visible(Claim.id), Claim.reported_at >= since)
            .group_by(Claim.customer_id)
            .having(func.count() >= FREQUENT)
        )
    ).all()
    out = []
    for _, n, ids in rows:
        claims = (await db.scalars(select(Claim).where(Claim.id.in_(ids)).order_by(Claim.reported_at))).all()
        out.append(
            {
                "type": "frequent_claimant",
                "severity": "high" if n >= 2 * FREQUENT else "medium",
                "score": int(n),
                "claims": [_ref(c) for c in claims],
                "explanation": f"The same customer reported {n} claims within 90 days.",
            }
        )
    return out


async def similar_narratives(db: AsyncSession, scope: Scope, limit: int = 50) -> list[dict[str, Any]]:
    ea, eb = embeddings.alias("ea"), embeddings.alias("eb")
    distance = ea.c.embedding.op("<=>", return_type=Float())(eb.c.embedding)
    visible = scope.claims()
    recent = select(Claim.id).where(Claim.reported_at >= scope.since)
    pairs = (
        await db.execute(
            select(ea.c.claim_id, eb.c.claim_id, (literal(1.0) - distance).label("similarity"))
            .join(eb, and_(eb.c.organization_id == ea.c.organization_id, ea.c.claim_id < eb.c.claim_id))
            .where(
                ea.c.claim_id.in_(visible),
                eb.c.claim_id.in_(visible),
                ea.c.claim_id.in_(recent),
                distance <= 1 - NARRATIVE_SIMILARITY,
            )
            .order_by(distance)
            .limit(limit * 4)
        )
    ).all()
    ids = {i for a, b, _ in pairs for i in (a, b)}
    claims = {c.id: c for c in await db.scalars(select(Claim).where(Claim.id.in_(ids)))} if ids else {}
    out = []
    for a, b, sim in pairs:
        x, y = claims[a], claims[b]
        if x.customer_id == y.customer_id:  # the same customer re-using their wording is not this pattern
            continue
        out.append(
            {
                "type": "similar_narrative",
                "severity": "high" if float(sim) >= 0.97 else "medium",
                "score": round(float(sim), 4),
                "claims": [_ref(x), _ref(y)],
                "explanation": f"Near-identical descriptions (similarity {float(sim):.2f}) on claims of two "
                "different customers.",
            }
        )
    return out[:limit]


async def shared_documents(db: AsyncSession, scope: Scope) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(ClaimDocument.sha256, func.array_agg(func.distinct(ClaimDocument.claim_id)))
            .where(scope.visible(ClaimDocument.claim_id), ClaimDocument.deleted_at.is_(None))
            .group_by(ClaimDocument.sha256)
            .having(func.count(func.distinct(ClaimDocument.claim_id)) > 1)
        )
    ).all()
    out = []
    for sha, ids in rows:
        claims = (await db.scalars(select(Claim).where(Claim.id.in_(ids)))).all()
        out.append(
            {
                "type": "shared_document",
                "severity": "high",
                "score": len(claims),
                "claims": [_ref(c) for c in claims],
                "explanation": f"The same file (SHA-256 {sha[:12]}…) is attached to {len(claims)} claims.",
            }
        )
    return out


async def detect(db: AsyncSession, scope: Scope) -> dict[str, Any]:
    outliers, notes = await amount_outliers(db, scope)
    found = [
        *outliers,
        *await frequent_claimants(db, scope),
        *await similar_narratives(db, scope),
        *await shared_documents(db, scope),
    ]
    order = {"high": 0, "medium": 1}
    found.sort(key=lambda x: (order.get(x["severity"], 2), -abs(float(x["score"]))))
    return {
        "days": scope.days,
        "definition": "Patterns across claims, each from one query over the claims you can see: robust "
        f"amount z-score >= {Z_THRESHOLD} per product (needs {MIN_BASELINE} claims), >= {FREQUENT} claims "
        "by one "
        f"customer in 90 days, near-identical narratives (similarity >= {NARRATIVE_SIMILARITY}) across "
        "customers, the same file on several claims. Leads for a person; nothing is changed automatically.",
        "anomalies": found,
        "notes": notes,
    }
