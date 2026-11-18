"""Reports, notifications and webhooks (Phase 11).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-11

* ``report_versions``: one row per generated report; a queued row may become ready or failed exactly
  once, then it is immutable (trigger), and it is never deleted by the app role.
* ``notifications``: in-app, email and SMS notifications with delivery status.
* ``webhook_subscriptions`` / ``webhook_deliveries``: outbound webhooks; signing secrets are stored
  encrypted (AES-256-GCM, app-level); one delivery per subscription and event, retried with backoff.
* ``claimos_due_webhook_deliveries`` / ``claimos_pending_notifications``: narrow definer functions so the
  dispatch tasks can find work of every tenant (identifiers only; sending itself runs tenant-bound).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = pg.UUID(as_uuid=True)
JSONB = pg.JSONB()
TS = sa.DateTime(timezone=True)
ORG_SETTING = "NULLIF(current_setting('app.org_id', true), '')::uuid"
APP_ROLE = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'claimos_app') THEN "
    "EXECUTE '{}'; END IF; END $$"
)


def _org() -> sa.Column[object]:
    return sa.Column(
        "organization_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "report_versions",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column("claim_id", UUID, sa.ForeignKey("claims.id", ondelete="CASCADE"), nullable=False),
        sa.Column("decision_id", UUID, sa.ForeignKey("claim_decisions.id")),
        sa.Column("version_no", sa.Integer, nullable=False),
        sa.Column("format", sa.String(10), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("template_version", sa.String(40), nullable=False),
        sa.Column("storage_key", sa.String(400)),
        sa.Column("sha256", sa.String(64)),
        sa.Column("size_bytes", sa.BigInteger),
        sa.Column("evidence_count", sa.Integer),
        sa.Column("error", sa.String(500)),
        sa.Column("requested_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("generated_at", TS),
        sa.UniqueConstraint("claim_id", "version_no", "format", name="uq_report_versions_version"),
        sa.CheckConstraint("format IN ('pdf', 'json')", name="format"),
        sa.CheckConstraint("status IN ('queued', 'ready', 'failed')", name="status"),
        sa.CheckConstraint(
            "status <> 'ready' OR (storage_key IS NOT NULL AND sha256 IS NOT NULL)", name="ready"
        ),
    )
    op.create_index(
        "ix_report_versions_claim", "report_versions", ["organization_id", "claim_id", "created_at"]
    )
    op.execute(
        "CREATE FUNCTION claimos_report_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'report versions are kept' USING ERRCODE = 'P0001'; "
        "END IF; "
        "IF OLD.status = 'queued' AND NEW.status IN ('ready', 'failed') AND NEW.claim_id = OLD.claim_id "
        "AND NEW.version_no = OLD.version_no AND NEW.format = OLD.format THEN RETURN NEW; END IF; "
        "RAISE EXCEPTION 'report version % (%) is % and immutable', OLD.version_no, OLD.format, "
        "OLD.status USING ERRCODE = 'P0001'; END $$"
    )
    op.execute(
        "CREATE TRIGGER trg_report_versions_guard BEFORE UPDATE OR DELETE ON report_versions "
        "FOR EACH ROW EXECUTE FUNCTION claimos_report_guard()"
    )

    op.create_table(
        "notifications",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("template_key", sa.String(60), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.String(2000), nullable=False),
        sa.Column("link", sa.String(400)),
        sa.Column("claim_id", UUID, sa.ForeignKey("claims.id", ondelete="CASCADE")),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("provider", sa.String(20)),
        sa.Column("provider_message_id", sa.String(200)),
        sa.Column("error", sa.String(500)),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", TS),
        sa.Column("read_at", TS),
        sa.CheckConstraint("channel IN ('in_app', 'email', 'sms')", name="channel"),
        sa.CheckConstraint("status IN ('pending', 'sent', 'failed', 'read', 'skipped')", name="status"),
    )
    op.create_index(
        "ix_notifications_inbox", "notifications", ["organization_id", "user_id", "channel", "created_at"]
    )

    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("description", sa.String(200), nullable=False, server_default=""),
        sa.Column("secret_enc", sa.String(400), nullable=False),
        sa.Column("secret_hint", sa.String(12), nullable=False),
        sa.Column("event_types", JSONB, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("disabled_at", TS),
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column(
            "subscription_id",
            UUID,
            sa.ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_id", UUID, nullable=False),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("response_code", sa.Integer),
        sa.Column("last_error", sa.String(500)),
        sa.Column("next_attempt_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("delivered_at", TS),
        sa.UniqueConstraint("subscription_id", "event_id", name="uq_webhook_deliveries_event"),
        sa.CheckConstraint("status IN ('pending', 'delivered', 'failed', 'dead')", name="status"),
    )
    op.create_index("ix_webhook_deliveries_due", "webhook_deliveries", ["status", "next_attempt_at"])
    op.create_index("ix_webhook_deliveries_sub", "webhook_deliveries", ["subscription_id", "created_at"])

    for table in ("report_versions", "notifications", "webhook_subscriptions", "webhook_deliveries"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING (organization_id = {ORG_SETTING}) "
            f"WITH CHECK (organization_id = {ORG_SETTING})"
        )
    op.execute(
        "CREATE FUNCTION claimos_due_webhook_deliveries(max_rows integer) "
        "RETURNS TABLE (delivery_id uuid, organization_id uuid) LANGUAGE sql SECURITY DEFINER "
        "SET search_path = public AS $$ SELECT id, organization_id FROM webhook_deliveries "
        "WHERE status IN ('pending', 'failed') AND next_attempt_at <= now() "
        "ORDER BY next_attempt_at LIMIT max_rows $$"
    )
    op.execute(
        "CREATE FUNCTION claimos_pending_notifications(max_rows integer) "
        "RETURNS TABLE (notification_id uuid, organization_id uuid) LANGUAGE sql SECURITY DEFINER "
        "SET search_path = public AS $$ SELECT id, organization_id FROM notifications "
        "WHERE status = 'pending' AND channel IN ('email', 'sms') ORDER BY created_at LIMIT max_rows $$"
    )
    op.execute("REVOKE ALL ON FUNCTION claimos_pending_notifications(integer) FROM PUBLIC")
    op.execute(
        APP_ROLE.format("GRANT EXECUTE ON FUNCTION claimos_pending_notifications(integer) TO claimos_app")
    )
    op.execute("REVOKE ALL ON FUNCTION claimos_due_webhook_deliveries(integer) FROM PUBLIC")
    op.execute(
        APP_ROLE.format("GRANT EXECUTE ON FUNCTION claimos_due_webhook_deliveries(integer) TO claimos_app")
    )
    op.execute(APP_ROLE.format("REVOKE DELETE, TRUNCATE ON report_versions FROM claimos_app"))


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS claimos_due_webhook_deliveries(integer)")
    op.execute("DROP FUNCTION IF EXISTS claimos_pending_notifications(integer)")
    op.drop_table("webhook_deliveries")
    op.drop_table("webhook_subscriptions")
    op.drop_table("notifications")
    op.drop_table("report_versions")
    op.execute("DROP FUNCTION IF EXISTS claimos_report_guard()")
