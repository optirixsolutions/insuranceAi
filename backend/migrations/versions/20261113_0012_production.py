"""Production readiness (Phase 13): MFA, retention runs, audit-chain verification history.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-13

* ``user_mfa``: one TOTP factor per user (secret encrypted with AES-256-GCM in the app), the last
  accepted time step (replay protection) and hashed one-time recovery codes.
* ``mfa_challenges``: the short-lived second step of a sign-in after a correct password.
* ``organizations.mfa_required_roles``: roles that must use MFA (empty = optional for everyone).
* ``retention_runs``: what each daily retention job deleted (platform-wide, counts only).
* ``audit_verifications``: result of each scheduled audit hash-chain verification per organisation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = pg.UUID(as_uuid=True)
JSONB = pg.JSONB()
TS = sa.DateTime(timezone=True)
ORG_SETTING = "NULLIF(current_setting('app.org_id', true), '')::uuid"
RETENTION_ARGS = "integer, integer, integer, integer, integer"


def _org() -> sa.Column[object]:
    return sa.Column(
        "organization_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "user_mfa",
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        _org(),
        sa.Column("secret_enc", sa.String(400), nullable=False),
        sa.Column("confirmed_at", TS),
        sa.Column("last_used_step", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("recovery_hashes", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "mfa_challenges",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.LargeBinary, nullable=False, unique=True),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("used_at", TS),
        sa.Column("ip", sa.String(64)),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.add_column(
        "organizations",
        sa.Column("mfa_required_roles", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.create_table(
        "retention_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS),
        sa.Column("deleted", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.String(500)),
    )
    op.create_table(
        "audit_verifications",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("events_checked", sa.Integer, nullable=False),
        sa.Column("first_bad_event_id", UUID),
        sa.Column("detail", sa.String(500)),
        sa.Column("checked_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_verifications_org", "audit_verifications", ["organization_id", "checked_at"])
    for table in ("user_mfa", "mfa_challenges", "audit_verifications"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING (organization_id = {ORG_SETTING}) "
            f"WITH CHECK (organization_id = {ORG_SETTING})"
        )
    # Retention across every tenant: a narrow definer function deletes only expired platform data
    # (never claims, decisions, documents, reports or audit events) and returns counts per table.
    op.execute(
        "CREATE FUNCTION claimos_retention(session_days integer, attempt_days integer, "
        "notification_days integer, delivery_days integer, checkpoint_days integer) RETURNS jsonb "
        "LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$ "
        "DECLARE out jsonb := '{}'; n integer; BEGIN "
        "DELETE FROM idempotency_keys WHERE expires_at < now(); GET DIAGNOSTICS n = ROW_COUNT; "
        "out := out || jsonb_build_object('idempotency_keys', n); "
        "DELETE FROM mfa_challenges WHERE expires_at < now() - interval '1 day'; "
        "GET DIAGNOSTICS n = ROW_COUNT; out := out || jsonb_build_object('mfa_challenges', n); "
        "DELETE FROM user_sessions WHERE (revoked_at IS NOT NULL OR absolute_expires_at < now()) "
        "AND coalesce(revoked_at, absolute_expires_at) < now() - make_interval(days => session_days); "
        "GET DIAGNOSTICS n = ROW_COUNT; out := out || jsonb_build_object('user_sessions', n); "
        "DELETE FROM login_attempts WHERE created_at < now() - make_interval(days => attempt_days); "
        "GET DIAGNOSTICS n = ROW_COUNT; out := out || jsonb_build_object('login_attempts', n); "
        "DELETE FROM notifications WHERE channel <> 'in_app' AND status <> 'pending' "
        "AND created_at < now() - make_interval(days => notification_days); "
        "GET DIAGNOSTICS n = ROW_COUNT; out := out || jsonb_build_object('outbound_notifications', n); "
        "DELETE FROM notifications WHERE channel = 'in_app' AND read_at IS NOT NULL "
        "AND read_at < now() - make_interval(days => notification_days); "
        "GET DIAGNOSTICS n = ROW_COUNT; out := out || jsonb_build_object('read_notifications', n); "
        "DELETE FROM webhook_deliveries WHERE status IN ('delivered', 'dead') "
        "AND created_at < now() - make_interval(days => delivery_days); "
        "GET DIAGNOSTICS n = ROW_COUNT; out := out || jsonb_build_object('webhook_deliveries', n); "
        "DELETE FROM workflow_checkpoints c USING workflow_executions e WHERE c.execution_id = e.id "
        "AND e.status IN ('completed', 'cancelled', 'failed') "
        "AND e.finished_at < now() - make_interval(days => checkpoint_days); "
        "GET DIAGNOSTICS n = ROW_COUNT; out := out || jsonb_build_object('workflow_checkpoints', n); "
        "RETURN out; END $$"
    )
    op.execute(f"REVOKE ALL ON FUNCTION claimos_retention({RETENTION_ARGS}) FROM PUBLIC")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'claimos_app') THEN "
        f"EXECUTE 'GRANT EXECUTE ON FUNCTION claimos_retention({RETENTION_ARGS}) TO claimos_app'; "
        "END IF; END $$"
    )

    # The nightly audit-chain check visits each organisation; this lists ids only.
    op.execute(
        "CREATE FUNCTION claimos_active_organization_ids() RETURNS SETOF uuid "
        "LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$ "
        "SELECT id FROM organizations WHERE status = 'active' ORDER BY created_at $$"
    )
    op.execute("REVOKE ALL ON FUNCTION claimos_active_organization_ids() FROM PUBLIC")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'claimos_app') THEN "
        "EXECUTE 'GRANT EXECUTE ON FUNCTION claimos_active_organization_ids() TO claimos_app'; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS claimos_active_organization_ids()")
    op.execute(f"DROP FUNCTION IF EXISTS claimos_retention({RETENTION_ARGS})")
    op.drop_table("audit_verifications")
    op.drop_table("retention_runs")
    op.drop_column("organizations", "mfa_required_roles")
    op.drop_table("mfa_challenges")
    op.drop_table("user_mfa")
