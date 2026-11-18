"""Idempotency-Key handling for non-idempotent POSTs (docs/architecture/05-api-specification.md §1).

The stored response is written in the same transaction as the side effect, so a key is
either fully applied (and replayed afterwards) or not applied at all.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BusinessRuleError, ConflictError
from app.models.platform import IdempotencyKey
from app.security.principal import Principal

TTL = timedelta(hours=24)


def validate_key(key: str | None) -> str:
    if key is None or not (8 <= len(key) <= 100) or not key.isprintable():
        raise BusinessRuleError("Send a unique Idempotency-Key header (8-100 printable characters)")
    return key


def request_hash(body: Any) -> bytes:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).digest()


async def find_replay(
    db: AsyncSession, principal: Principal, key: str, method: str, path: str, digest: bytes
) -> tuple[int, dict[str, Any]] | None:
    stored = await db.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.organization_id == principal.organization_id,
            IdempotencyKey.user_id == principal.user_id,
            IdempotencyKey.key == key,
            IdempotencyKey.expires_at > datetime.now(UTC),
        )
    )
    if stored is None:
        return None
    if stored.method != method or stored.path != path or stored.request_hash != digest:
        raise ConflictError("This Idempotency-Key was already used for a different request")
    return stored.response_status, stored.response_body


def remember(
    db: AsyncSession,
    principal: Principal,
    key: str,
    method: str,
    path: str,
    digest: bytes,
    status: int,
    body: dict[str, Any],
) -> None:
    db.add(
        IdempotencyKey(
            organization_id=principal.organization_id,
            user_id=principal.user_id,
            key=key,
            method=method,
            path=path,
            request_hash=digest,
            response_status=status,
            response_body=body,
            expires_at=datetime.now(UTC) + TTL,
        )
    )
