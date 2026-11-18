"""Similar claims (Phase 12): deterministic text embeddings in pgvector, nearest-neighbour search.

The embedding is a local feature-hashing model (``claimos-hash-256-v1``): words, word pairs and
character trigrams of the claim's loss category, description and incident place are hashed into 256
signed dimensions and L2-normalised. It needs no external service, sends nothing anywhere, and is
reproducible; it measures *lexical* similarity (shared wording and phrasing), not meaning, so it finds
re-used or templated narratives well and paraphrases poorly. Vectors live in ``claim_embeddings`` with
an HNSW cosine index; every query is restricted to the claims the caller may read.
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections.abc import Iterable
from itertools import pairwise
from typing import Any

from sqlalchemy import Column, DateTime, Float, MetaData, String, Table, func, literal, select
from sqlalchemy.dialects.postgresql import UUID, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import UserDefinedType

from app.core.errors import NotFoundError
from app.models.claims import Claim
from app.security.principal import Principal
from app.services.claim_access import get_visible_claim, scope_claims

MODEL = "claimos-hash-256-v1"
DIMENSIONS = 256
WORD = re.compile(r"[a-z0-9]+")
STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "had",
        "has",
        "have",
        "i",
        "in",
        "is",
        "it",
        "its",
        "my",
        "of",
        "on",
        "or",
        "our",
        "the",
        "to",
        "was",
        "were",
        "with",
        "we",
    ]
)


class Vector(UserDefinedType[list[float]]):
    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return f"vector({DIMENSIONS})"

    def bind_processor(self, dialect: Any) -> Any:
        return lambda value: None if value is None else "[" + ",".join(f"{x:.6f}" for x in value) + "]"

    def result_processor(self, dialect: Any, coltype: Any) -> Any:
        return lambda value: None if value is None else [float(x) for x in str(value).strip("[]").split(",")]


embeddings = Table(
    "claim_embeddings",
    MetaData(),
    Column("claim_id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True)),
    Column("model", String(40)),
    Column("text_sha256", String(64)),
    Column("updated_at", DateTime(timezone=True)),
    Column("embedding", Vector()),
)


def _features(text: str) -> Iterable[tuple[str, float]]:
    words = [w for w in WORD.findall(text.lower()) if w not in STOP]
    for w in words:
        yield f"w:{w}", 1.0
        padded = f"^{w}$"
        for i in range(len(padded) - 2):
            yield f"c:{padded[i : i + 3]}", 0.35
    for a, b in pairwise(words):
        yield f"b:{a}_{b}", 1.5


def embed(text: str) -> list[float]:
    vec = [0.0] * DIMENSIONS
    for feature, weight in _features(text):
        h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
        vec[h % DIMENSIONS] += weight if (h >> 63) & 1 else -weight
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


def claim_text(claim: Claim) -> str:
    place = claim.incident_location or {}
    return " ".join(
        str(x) for x in (claim.category, claim.description, place.get("city"), place.get("description")) if x
    )


async def upsert(db: AsyncSession, claim: Claim) -> None:
    """(Re)compute a claim's embedding when its text changed. Called on create and on edit."""
    text = claim_text(claim)
    digest = hashlib.sha256(f"{MODEL}:{text}".encode()).hexdigest()
    stmt = insert(embeddings).values(
        claim_id=claim.id,
        organization_id=claim.organization_id,
        model=MODEL,
        text_sha256=digest,
        updated_at=func.now(),
        embedding=embed(text),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[embeddings.c.claim_id],
        set_={
            "model": MODEL,
            "text_sha256": digest,
            "updated_at": func.now(),
            "embedding": stmt.excluded.embedding,
        },
        where=embeddings.c.text_sha256 != digest,
    )
    await db.execute(stmt)


async def backfill(db: AsyncSession, organization_id: uuid.UUID, limit: int = 5000) -> int:
    missing = await db.scalars(
        select(Claim)
        .outerjoin(embeddings, embeddings.c.claim_id == Claim.id)
        .where(
            Claim.organization_id == organization_id,
            Claim.deleted_at.is_(None),
            embeddings.c.claim_id.is_(None),
        )
        .limit(limit)
    )
    count = 0
    for claim in missing:
        await upsert(db, claim)
        count += 1
    return count


async def similar(
    db: AsyncSession,
    principal: Principal,
    claim_id: uuid.UUID,
    *,
    limit: int = 10,
    min_similarity: float = 0.2,
) -> list[dict[str, Any]]:
    claim = await get_visible_claim(db, principal, claim_id)
    await upsert(db, claim)
    target = select(embeddings.c.embedding).where(embeddings.c.claim_id == claim.id).scalar_subquery()
    distance = embeddings.c.embedding.op("<=>", return_type=Float())(target)
    visible = scope_claims(select(Claim.id), principal).scalar_subquery()
    rows = (
        await db.execute(
            select(Claim, (literal(1.0) - distance).label("similarity"))
            .join(embeddings, embeddings.c.claim_id == Claim.id)
            .where(Claim.id != claim.id, Claim.id.in_(visible))
            .order_by(distance)
            .limit(min(limit, 50))
        )
    ).all()
    words = set(WORD.findall(claim_text(claim).lower())) - STOP
    out = []
    for other, score in rows:
        value = round(float(score), 4)
        if value < min_similarity:
            continue
        shared = sorted(words & (set(WORD.findall(claim_text(other).lower())) - STOP), key=len, reverse=True)
        out.append(
            {
                "claim_id": str(other.id),
                "claim_reference": other.claim_reference,
                "status": other.status,
                "category": other.category,
                "same_customer": other.customer_id == claim.customer_id,
                "incident_date": other.incident_date.isoformat(),
                "claimed_amount": str(other.claimed_amount),
                "currency": other.currency,
                "similarity": value,
                "shared_terms": shared[:8],
            }
        )
    return out


async def require_embedding(db: AsyncSession, claim_id: uuid.UUID) -> None:
    if await db.scalar(select(embeddings.c.claim_id).where(embeddings.c.claim_id == claim_id)) is None:
        raise NotFoundError("Claim has no embedding")
