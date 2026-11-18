"""Row locks on claims and workflow executions must not block foreign-key inserts (Phase 14).

The benchmark found the parallel POLICY_VERIFICATION and FRAUD_ANALYSIS branches deadlocking: one
transaction held ``SELECT ... FOR UPDATE`` on the execution row and waited for the per-execution advisory
lock, while the other held that advisory lock and waited to insert a row referencing the execution (a
foreign-key check takes KEY SHARE, which FOR UPDATE blocks). PostgreSQL aborted one branch and LangGraph
retried it 2 s later. Locking with FOR NO KEY UPDATE removes the conflict with foreign-key checks.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.claims_helpers import claim_payload, create_claim, setup_book
from tests.integration.conftest import ApiClient, register

pytestmark = pytest.mark.integration
BACKEND = Path(__file__).resolve().parents[2]
LOCKS = {
    "FOR UPDATE": "SELECT id FROM claims WHERE id = :c FOR UPDATE",
    "FOR NO KEY UPDATE": "SELECT id FROM claims WHERE id = :c FOR NO KEY UPDATE",
}


async def _note_insert_waits(engine: AsyncEngine, claim_id: str, org: str, user: str, clause: str) -> bool:
    """True if inserting a row that references the claim waits while another transaction holds ``clause``."""
    async with engine.connect() as holder, engine.connect() as writer:
        await holder.begin()
        await holder.execute(text(LOCKS[clause]), {"c": claim_id})
        await writer.begin()
        await writer.execute(text("SET LOCAL lock_timeout = '500ms'"))
        try:
            await writer.execute(
                text(
                    "INSERT INTO claim_notes (organization_id, claim_id, author_id, body) "
                    "VALUES (:o, :c, :u, 'lock probe')"
                ),
                {"o": org, "c": claim_id, "u": user},
            )
            waited = False
        except DBAPIError as exc:
            assert "lock timeout" in str(exc).lower(), exc
            waited = True
        await writer.rollback()
        await holder.rollback()
    return waited


async def test_no_key_update_lets_foreign_key_inserts_through(
    new_client: Callable[[], ApiClient], migrator_engine: AsyncEngine
) -> None:
    admin = new_client()
    me = register(admin)
    _, customer, policy = setup_book(admin)
    claim = create_claim(admin, claim_payload(customer, policy))
    assert claim.status_code == 201, claim.text
    args = (migrator_engine, claim.json()["id"], me["organization"]["id"], me["user"]["id"])
    assert await _note_insert_waits(*args, "FOR UPDATE") is True  # the old lock: the cause of the deadlock
    assert await _note_insert_waits(*args, "FOR NO KEY UPDATE") is False  # the lock the platform now takes


def test_claim_and_execution_locks_use_no_key_update() -> None:
    """Every lock on a claims or workflow_executions row uses key_share=True (FOR NO KEY UPDATE)."""
    files = [
        "workflows/runtime.py",
        "workflows/stp.py",
        "app/services/workflow_service.py",
        "app/services/claim_access.py",
        "app/services/review_service.py",
    ]
    bare: list[str] = []
    for name in files:
        lines = (BACKEND / name).read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, 1):
            if ".with_for_update()" not in line:
                continue
            context = " ".join(lines[max(0, number - 8) : number])
            if re.search(r"\b(Claim|WorkflowExecution)\b", context) and "HumanReview" not in context:
                bare.append(f"{name}:{number}")
    assert bare == [], f"claims/workflow_executions locked FOR UPDATE at {bare}"
