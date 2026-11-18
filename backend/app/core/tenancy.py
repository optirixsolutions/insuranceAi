"""Bind a database session to one organisation for PostgreSQL row-level security.

``bind_tenant`` stores the organisation on the session and sets the transaction-local
``app.org_id`` setting. An ``after_begin`` listener re-applies it to every later
transaction of the same session (e.g. after a commit), so RLS can never silently lapse.
Without a bound organisation, RLS policies match no rows.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

_ORG_KEY = "claimos_org_id"
_SET_ORG = text("SELECT set_config('app.org_id', :org_id, true)")


@event.listens_for(Session, "after_begin")
def _apply_tenant(session: Session, _transaction: Any, connection: Any) -> None:
    org_id = session.info.get(_ORG_KEY)
    if org_id is not None:
        connection.execute(_SET_ORG, {"org_id": str(org_id)})


async def bind_tenant(session: AsyncSession, organization_id: uuid.UUID) -> None:
    current = session.info.get(_ORG_KEY)
    if current is not None and current != organization_id:
        raise RuntimeError("session is already bound to a different organisation")
    session.info[_ORG_KEY] = organization_id
    # Applies to the current transaction (starting one if needed); the listener covers later ones.
    await session.execute(_SET_ORG, {"org_id": str(organization_id)})


def bound_tenant(session: AsyncSession) -> uuid.UUID | None:
    value = session.info.get(_ORG_KEY)
    return value if isinstance(value, uuid.UUID) else None
