"""Nightly maintenance against the real database: retention deletes only expired platform data, and the
audit-chain check records results and alerts on tampering (Phase 13)."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.db import Database
from app.services import maintenance_service
from tests.integration.conftest import ApiClient, invite, register

pytestmark = pytest.mark.integration


async def test_retention_deletes_only_expired_platform_data(
    new_client: Callable[[], ApiClient], settings: Settings, migrator_engine: AsyncEngine
) -> None:
    me = register(new_client())
    org, user = me["organization"]["id"], me["user"]["id"]
    marker = hashlib.sha256(uuid.uuid4().bytes).digest()
    async with migrator_engine.begin() as conn:
        for days in (200, 1):  # one expired, one recent
            await conn.execute(
                text(
                    "INSERT INTO login_attempts (id, organization_id, user_id, email_hash, success, reason, "
                    "created_at) VALUES (gen_random_uuid(), :o, :u, :h, true, 'retention_test', "
                    "now() - make_interval(days => :d))"
                ),
                {"o": org, "u": user, "h": marker, "d": days},
            )
        await conn.execute(
            text("UPDATE user_sessions SET revoked_at = now() - interval '60 days' WHERE user_id = :u"),
            {"u": user},
        )
        audit_before = await conn.scalar(
            text("SELECT count(*) FROM audit_events WHERE organization_id = :o"), {"o": org}
        )

    db = Database.from_settings(settings, application_name="claimos-retention-test")
    try:
        deleted = await maintenance_service.run_retention(db, settings)
    finally:
        await db.dispose()

    assert deleted["login_attempts"] >= 1 and deleted["user_sessions"] >= 1
    assert set(deleted) >= {
        "idempotency_keys",
        "mfa_challenges",
        "webhook_deliveries",
        "workflow_checkpoints",
    }
    async with migrator_engine.connect() as conn:
        left = (
            await conn.scalars(
                text(
                    "SELECT created_at > now() - interval '2 days' FROM login_attempts WHERE email_hash = :h"
                ),
                {"h": marker},
            )
        ).all()
        assert left == [True]  # the recent attempt stays
        assert (
            await conn.scalar(text("SELECT count(*) FROM user_sessions WHERE user_id = :u"), {"u": user}) == 0
        )
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM audit_events WHERE organization_id = :o"), {"o": org}
            )
            == audit_before
        )
        run = (
            await conn.execute(
                text(
                    "SELECT deleted, finished_at, error FROM retention_runs ORDER BY started_at DESC LIMIT 1"
                )
            )
        ).one()
    assert run.deleted == deleted and run.finished_at is not None and run.error is None


async def test_audit_chain_check_records_and_alerts_on_tampering(
    new_client: Callable[[], ApiClient], settings: Settings, migrator_engine: AsyncEngine
) -> None:
    clean_admin = new_client()
    clean = register(clean_admin)["organization"]["id"]
    victim_admin = new_client()
    victim = register(victim_admin)
    invite(victim_admin, ["auditor"])
    org = victim["organization"]["id"]
    async with migrator_engine.begin() as conn:  # a privileged attacker edits history
        await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER trg_audit_events_append_only"))
        await conn.execute(
            text(
                "UPDATE audit_events SET after = '{\"forged\": true}' WHERE organization_id = :o AND seq = 2"
            ),
            {"o": org},
        )
        await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER trg_audit_events_append_only"))

    db = Database.from_settings(settings, application_name="claimos-audit-verify-test")
    try:
        summary = await maintenance_service.verify_audit_chains(db, settings)
    finally:
        await db.dispose()

    assert org in summary["broken"] and clean not in summary["broken"]
    async with migrator_engine.connect() as conn:
        rows = {
            str(r.organization_id): r
            for r in (
                await conn.execute(
                    text(
                        "SELECT DISTINCT ON (organization_id) organization_id, ok, events_checked, "
                        "first_bad_event_id, detail FROM audit_verifications "
                        "WHERE organization_id IN (:a, :b) ORDER BY organization_id, checked_at DESC"
                    ),
                    {"a": org, "b": clean},
                )
            ).all()
        }
        bad_seq = await conn.scalar(
            text("SELECT seq FROM audit_events WHERE id = :i"), {"i": rows[org].first_bad_event_id}
        )
        alerts = (
            await conn.execute(
                text(
                    "SELECT channel, link FROM notifications WHERE organization_id = :o "
                    "AND template_key = 'audit.chain_broken'"
                ),
                {"o": org},
            )
        ).all()
    assert rows[clean].ok is True and rows[clean].events_checked >= 2
    assert rows[org].ok is False and bad_seq == 2 and "seq 2" in rows[org].detail
    # The admin (the only active audit reader; the auditor is still invited) gets in-app and email alerts.
    assert sorted(a.channel for a in alerts) == ["email", "in_app"] and alerts[0].link == "/audit"
    inbox = victim_admin.get("/notifications").json()
    assert any(n["template_key"] == "audit.chain_broken" for n in inbox["items"])
