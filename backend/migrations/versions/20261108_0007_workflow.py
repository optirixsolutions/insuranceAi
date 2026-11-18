"""Workflow orchestration: executions, events (outbox), checkpoint index, LangGraph checkpointer tables.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-08

* ``workflow_executions``: one LangGraph thread per execution (``thread_id = id``); at most one
  queued/running/interrupted execution per claim. ``heartbeat_at`` drives crash recovery.
* ``workflow_events``: append-only, gap-free ``seq`` per execution; the SSE endpoint replays from it.
* ``workflow_checkpoints``: index of LangGraph checkpoints for UI/audit (payloads stay in the
  checkpointer's own tables, created here from the library's migration list, as the schema owner).
* ``agent_runs.execution_id`` now references the execution that ran the agent.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from langgraph.checkpoint.postgres.base import BasePostgresSaver
from sqlalchemy.dialects import postgresql as pg

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = pg.UUID(as_uuid=True)
JSONB = pg.JSONB()
TS = sa.DateTime(timezone=True)
ORG_SETTING = "NULLIF(current_setting('app.org_id', true), '')::uuid"
STATUSES = ("queued", "running", "interrupted", "completed", "failed", "cancelled")
TRIGGERS = ("initial", "resume", "reassessment", "more_info", "retry")
EVENTS = (
    "execution_started",
    "node_started",
    "node_completed",
    "node_failed",
    "retry",
    "interrupt",
    "resumed",
    "status_changed",
    "progress",
    "execution_completed",
    "execution_failed",
    "execution_cancelled",
    "recovered",
)


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _org() -> sa.Column[object]:
    return sa.Column(
        "organization_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "workflow_executions",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column("claim_id", UUID, sa.ForeignKey("claims.id", ondelete="CASCADE"), nullable=False),
        sa.Column("graph_name", sa.String(60), nullable=False),
        sa.Column("graph_version", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("trigger", sa.String(20), nullable=False),
        sa.Column("current_node", sa.String(60)),
        sa.Column("interrupt", JSONB),
        sa.Column("progress", sa.Numeric(5, 4), nullable=False, server_default="0"),
        sa.Column("iteration_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reassessment_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("more_info_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("recovery_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("token_budget", sa.Integer, nullable=False, server_default="200000"),
        sa.Column("tokens_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("started_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", TS),
        sa.Column("heartbeat_at", TS),
        sa.Column("finished_at", TS),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("cancel_requested_at", TS),
        sa.Column("error_code", sa.String(60)),
        sa.Column("error_detail", sa.String(1000)),
        sa.Column("celery_task_id", sa.String(80)),
        sa.Column("idempotency_key", sa.String(120)),
        sa.Column("row_version", sa.Integer, nullable=False, server_default="1"),
        sa.CheckConstraint(_in("status", STATUSES), name="status"),
        sa.CheckConstraint(_in("trigger", TRIGGERS), name="trigger"),
        sa.CheckConstraint("progress BETWEEN 0 AND 1", name="progress"),
    )
    op.create_index(
        "ix_workflow_executions_org_claim",
        "workflow_executions",
        ["organization_id", "claim_id", "created_at"],
    )
    op.create_index(
        "ix_workflow_executions_status_heartbeat", "workflow_executions", ["status", "heartbeat_at"]
    )
    op.create_index(
        "uq_workflow_executions_one_active",
        "workflow_executions",
        ["claim_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'interrupted')"),
    )
    op.create_index(
        "uq_workflow_executions_idempotency",
        "workflow_executions",
        ["organization_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "workflow_events",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column(
            "execution_id", UUID, sa.ForeignKey("workflow_executions.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("claim_id", UUID, sa.ForeignKey("claims.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.BigInteger, nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("node", sa.String(60)),
        sa.Column("agent_key", sa.String(40)),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("occurred_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("execution_id", "seq", name="uq_workflow_events_execution_seq"),
        sa.CheckConstraint(_in("event_type", EVENTS), name="event_type"),
    )
    op.create_index(
        "ix_workflow_events_org_claim", "workflow_events", ["organization_id", "claim_id", "occurred_at"]
    )
    op.execute(
        "CREATE TRIGGER trg_workflow_events_append_only BEFORE UPDATE OR DELETE ON workflow_events "
        "FOR EACH ROW EXECUTE FUNCTION claimos_forbid_mutation()"
    )

    op.create_table(
        "workflow_checkpoints",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column(
            "execution_id", UUID, sa.ForeignKey("workflow_executions.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("checkpoint_id", sa.String(80), nullable=False),
        sa.Column("checkpoint_ns", sa.String(200), nullable=False, server_default=""),
        sa.Column("node", sa.String(60)),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("execution_id", "checkpoint_ns", "checkpoint_id", name="uq_workflow_checkpoints"),
    )
    op.create_index(
        "ix_workflow_checkpoints_execution", "workflow_checkpoints", ["execution_id", "created_at"]
    )

    op.create_foreign_key(
        "fk_agent_runs_execution", "agent_runs", "workflow_executions", ["execution_id"], ["id"]
    )
    op.create_index("ix_agent_runs_execution", "agent_runs", ["execution_id"])

    for table in ("workflow_executions", "workflow_events", "workflow_checkpoints"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING (organization_id = {ORG_SETTING}) "
            f"WITH CHECK (organization_id = {ORG_SETTING})"
        )

    # Crash recovery must see stale executions of every tenant; this narrow definer function returns
    # identifiers only (RLS still applies to every other read the recovery task makes per tenant).
    op.execute(
        "CREATE FUNCTION claimos_stale_executions(stale_seconds integer) "
        "RETURNS TABLE (execution_id uuid, organization_id uuid) LANGUAGE sql SECURITY DEFINER "
        "SET search_path = public AS $$ SELECT id, organization_id FROM workflow_executions "
        "WHERE (status = 'running' AND heartbeat_at < now() - make_interval(secs => stale_seconds)) "
        "OR (status = 'queued' AND created_at < now() - make_interval(secs => stale_seconds)) $$"
    )
    op.execute("REVOKE ALL ON FUNCTION claimos_stale_executions(integer) FROM PUBLIC")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'claimos_app') THEN "
        "EXECUTE 'GRANT EXECUTE ON FUNCTION claimos_stale_executions(integer) TO claimos_app'; END IF; END $$"
    )

    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'claimos_app') THEN "
        "EXECUTE 'REVOKE UPDATE, DELETE, TRUNCATE ON workflow_events FROM claimos_app'; "
        "EXECUTE 'REVOKE DELETE, TRUNCATE ON workflow_executions FROM claimos_app'; END IF; END $$"
    )

    # LangGraph's own checkpoint tables, from the library's migration list. Some statements use
    # CREATE INDEX CONCURRENTLY, which cannot run in a transaction: run them in autocommit mode.
    with op.get_context().autocommit_block():
        for version, statement in enumerate(BasePostgresSaver.MIGRATIONS):
            op.execute(statement)
            op.execute(f"INSERT INTO checkpoint_migrations (v) VALUES ({version}) ON CONFLICT DO NOTHING")
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'claimos_app') THEN "
            "EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON checkpoints, checkpoint_blobs, checkpoint_writes, "
            "checkpoint_migrations TO claimos_app'; END IF; END $$"
        )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS claimos_stale_executions(integer)")
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints", "checkpoint_migrations"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.drop_index("ix_agent_runs_execution", table_name="agent_runs")
    op.drop_constraint("fk_agent_runs_execution", "agent_runs", type_="foreignkey")
    op.drop_table("workflow_checkpoints")
    op.drop_table("workflow_events")
    op.drop_table("workflow_executions")
