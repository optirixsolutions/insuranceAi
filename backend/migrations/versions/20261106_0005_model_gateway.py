"""Model registry, agent configuration and model invocations.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = pg.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)
ORG_SETTING = "NULLIF(current_setting('app.org_id', true), '')::uuid"
TABLES = ("model_registry", "agent_configs", "model_invocations")


def _org() -> sa.Column[object]:
    return sa.Column(
        "organization_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )


def _id() -> sa.Column[object]:
    return sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _stamps() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    op.create_table(
        "model_registry",
        _id(),
        _org(),
        sa.Column("provider", sa.String(40), nullable=False, server_default="openrouter"),
        sa.Column("slug", sa.String(200), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("is_free", sa.Boolean, nullable=False),
        sa.Column("context_length", sa.Integer),
        sa.Column("max_completion_tokens", sa.Integer),
        sa.Column(
            "input_modalities", pg.ARRAY(sa.String(20)), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("declared_capabilities", pg.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("verified_capabilities", pg.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("probe_status", sa.String(20), nullable=False, server_default="never"),
        sa.Column("probed_at", TS),
        sa.Column("prompt_price_per_mtok", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("completion_price_per_mtok", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("data_policy", sa.String(30), nullable=False, server_default="unknown"),
        sa.Column("data_policy_note", sa.String(500)),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("in_catalog", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("requests_per_minute", sa.Integer, nullable=False, server_default="15"),
        sa.Column("row_version", sa.Integer, nullable=False, server_default="1"),
        *_stamps(),
        sa.UniqueConstraint("organization_id", "slug", name="uq_model_registry_org_slug"),
        sa.UniqueConstraint("organization_id", "id", name="uq_model_registry_org_id"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="status"),
        sa.CheckConstraint(
            "probe_status IN ('never', 'running', 'passed', 'partial', 'failed')", name="probe"
        ),
        sa.CheckConstraint(
            "data_policy IN ('unknown', 'may_log', 'no_training', 'zero_retention')", name="policy"
        ),
        sa.CheckConstraint("requests_per_minute BETWEEN 1 AND 10000", name="rpm"),
    )
    op.create_index("ix_model_registry_org", "model_registry", ["organization_id"])

    op.create_table(
        "agent_configs",
        _id(),
        _org(),
        sa.Column("agent_key", sa.String(40), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("primary_model_id", UUID),
        sa.Column("fallback_model_ids", pg.ARRAY(UUID), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "required_capabilities", pg.ARRAY(sa.String(20)), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("confidence_threshold", sa.Float, nullable=False, server_default="0.85"),
        sa.Column("max_retries", sa.Integer, nullable=False, server_default="2"),
        sa.Column("timeout_seconds", sa.Integer, nullable=False, server_default="60"),
        sa.Column("max_output_tokens", sa.Integer, nullable=False, server_default="2000"),
        sa.Column("temperature", sa.Float, nullable=False, server_default="0"),
        sa.Column("row_version", sa.Integer, nullable=False, server_default="1"),
        *_stamps(),
        sa.UniqueConstraint("organization_id", "agent_key", name="uq_agent_configs_org_agent"),
        sa.ForeignKeyConstraint(
            ["organization_id", "primary_model_id"],
            ["model_registry.organization_id", "model_registry.id"],
            name="fk_agent_configs_primary_model",
        ),
        sa.CheckConstraint("max_retries BETWEEN 0 AND 5", name="retries"),
        sa.CheckConstraint("timeout_seconds BETWEEN 5 AND 600", name="timeout"),
        sa.CheckConstraint("confidence_threshold BETWEEN 0.5 AND 1", name="threshold"),
        sa.CheckConstraint("temperature BETWEEN 0 AND 2", name="temperature"),
        sa.CheckConstraint("max_output_tokens BETWEEN 64 AND 32000", name="tokens"),
    )
    op.create_index("ix_agent_configs_org", "agent_configs", ["organization_id"])

    op.create_table(
        "model_invocations",
        _id(),
        _org(),
        sa.Column("agent_key", sa.String(40), nullable=False),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("model_slug", sa.String(200), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("error_code", sa.String(60)),
        sa.Column("latency_ms", sa.Integer, nullable=False),
        sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(14, 8), nullable=False, server_default="0"),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("data_classification", sa.String(20), nullable=False),
        sa.Column("request_id", sa.String(64)),
        sa.Column("provider_request_id", sa.String(120)),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("data_classification IN ('synthetic', 'real')", name="classification"),
    )
    op.create_index(
        "ix_model_invocations_org_created", "model_invocations", ["organization_id", "created_at"]
    )
    op.create_index("ix_model_invocations_org_agent", "model_invocations", ["organization_id", "agent_key"])
    op.execute(
        "CREATE TRIGGER trg_model_invocations_append_only BEFORE UPDATE OR DELETE ON model_invocations "
        "FOR EACH ROW EXECUTE FUNCTION claimos_forbid_mutation()"
    )
    for table in ("model_registry", "agent_configs"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION claimos_set_updated_at()"
        )
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING (organization_id = {ORG_SETTING}) "
            f"WITH CHECK (organization_id = {ORG_SETTING})"
        )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'claimos_app') THEN "
        "EXECUTE 'REVOKE UPDATE, DELETE, TRUNCATE ON model_invocations FROM claimos_app'; END IF; END $$"
    )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
