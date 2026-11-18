"""Agents: prompt versions, agent runs and validated agent outputs.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-07

* ``prompt_versions`` mirrors source-controlled prompt files; content is immutable per version
  (a changed file must get a new version number).
* ``agent_runs`` is append-only once finished: only a ``running`` row may be updated (once, to
  its final state); nothing is ever deleted. One running run per claim and agent.
* ``agent_outputs`` is append-only. ``model_invocations.agent_run_id`` links each model call to
  the run that made it. ``execution_id`` is the Phase 7 workflow execution (no FK yet).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = pg.UUID(as_uuid=True)
JSONB = pg.JSONB()
TS = sa.DateTime(timezone=True)
ORG_SETTING = "NULLIF(current_setting('app.org_id', true), '')::uuid"
AGENT_KEYS = ("intake", "document", "policy", "fraud", "assessment", "validation", "decision", "compliance")
RUN_STATUSES = ("running", "succeeded", "partial", "failed", "skipped", "timed_out")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        "prompt_versions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("agent_key", sa.String(40), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("template_path", sa.String(200), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("agent_key", "version", name="uq_prompt_versions_agent_version"),
        sa.CheckConstraint("status IN ('draft', 'active', 'retired')", name="status"),
    )
    op.execute(
        "CREATE FUNCTION claimos_prompt_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF TG_OP = 'DELETE' OR NEW.content IS DISTINCT FROM OLD.content "
        "OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256 OR NEW.version IS DISTINCT FROM OLD.version "
        "OR NEW.agent_key IS DISTINCT FROM OLD.agent_key THEN "
        "RAISE EXCEPTION 'prompt versions are immutable; publish a new version' USING ERRCODE = 'P0001'; END IF; "
        "RETURN NEW; END $$"
    )
    op.execute(
        "CREATE TRIGGER trg_prompt_versions_immutable BEFORE UPDATE OR DELETE ON prompt_versions "
        "FOR EACH ROW EXECUTE FUNCTION claimos_prompt_immutable()"
    )

    op.create_table(
        "agent_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "organization_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("claim_id", UUID, sa.ForeignKey("claims.id", ondelete="CASCADE"), nullable=False),
        sa.Column("execution_id", UUID),
        sa.Column("agent_key", sa.String(40), nullable=False),
        sa.Column("node", sa.String(60), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("model_slug", sa.String(200)),
        sa.Column("prompt_version_id", UUID, sa.ForeignKey("prompt_versions.id")),
        sa.Column("rule_set_version", sa.String(40), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("used_llm", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("llm_note", sa.String(500)),
        sa.Column("escalate", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("findings", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("missing", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(60)),
        sa.Column("error_detail", sa.String(1000)),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("triggered_by", UUID, sa.ForeignKey("users.id")),
        sa.CheckConstraint(_in("agent_key", AGENT_KEYS), name="agent_key"),
        sa.CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        sa.CheckConstraint("attempt >= 1", name="attempt"),
        sa.UniqueConstraint("claim_id", "agent_key", "attempt", name="uq_agent_runs_claim_agent_attempt"),
    )
    op.create_index("ix_agent_runs_org_claim", "agent_runs", ["organization_id", "claim_id", "started_at"])
    op.create_index("ix_agent_runs_agent_started", "agent_runs", ["agent_key", "started_at"])
    op.create_index(
        "uq_agent_runs_one_running",
        "agent_runs",
        ["claim_id", "agent_key"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.execute(
        "CREATE FUNCTION claimos_agent_run_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'agent runs are append-only' USING ERRCODE = 'P0001'; END IF; "
        "IF OLD.status <> 'running' THEN "
        "RAISE EXCEPTION 'agent run % is finished and immutable', OLD.id USING ERRCODE = 'P0001'; END IF; "
        "IF NEW.id <> OLD.id OR NEW.claim_id <> OLD.claim_id OR NEW.agent_key <> OLD.agent_key "
        "OR NEW.attempt <> OLD.attempt OR (NEW.input_hash <> OLD.input_hash AND OLD.input_hash <> 'pending') "
        "OR NEW.organization_id <> OLD.organization_id THEN "
        "RAISE EXCEPTION 'agent run identity is immutable' USING ERRCODE = 'P0001'; END IF; "
        "RETURN NEW; END $$"
    )
    op.execute(
        "CREATE TRIGGER trg_agent_runs_guard BEFORE UPDATE OR DELETE ON agent_runs "
        "FOR EACH ROW EXECUTE FUNCTION claimos_agent_run_guard()"
    )

    op.create_table(
        "agent_outputs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "organization_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "agent_run_id",
            UUID,
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("schema_name", sa.String(80), nullable=False),
        sa.Column("schema_version", sa.String(20), nullable=False),
        sa.Column("output", JSONB, nullable=False),
        sa.Column("validation_errors", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("output_hash", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_outputs_org", "agent_outputs", ["organization_id"])
    op.execute(
        "CREATE TRIGGER trg_agent_outputs_append_only BEFORE UPDATE OR DELETE ON agent_outputs "
        "FOR EACH ROW EXECUTE FUNCTION claimos_forbid_mutation()"
    )

    op.add_column("model_invocations", sa.Column("agent_run_id", UUID, sa.ForeignKey("agent_runs.id")))
    op.create_index("ix_model_invocations_agent_run", "model_invocations", ["agent_run_id"])

    for table in ("agent_runs", "agent_outputs"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING (organization_id = {ORG_SETTING}) "
            f"WITH CHECK (organization_id = {ORG_SETTING})"
        )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'claimos_app') THEN "
        "EXECUTE 'REVOKE DELETE, TRUNCATE ON agent_runs, prompt_versions FROM claimos_app'; "
        "EXECUTE 'REVOKE UPDATE, DELETE, TRUNCATE ON agent_outputs FROM claimos_app'; END IF; END $$"
    )


def downgrade() -> None:
    op.drop_index("ix_model_invocations_agent_run", table_name="model_invocations")
    op.drop_column("model_invocations", "agent_run_id")
    op.drop_table("agent_outputs")
    op.drop_table("agent_runs")
    op.execute("DROP FUNCTION IF EXISTS claimos_agent_run_guard()")
    op.drop_table("prompt_versions")
    op.execute("DROP FUNCTION IF EXISTS claimos_prompt_immutable()")
