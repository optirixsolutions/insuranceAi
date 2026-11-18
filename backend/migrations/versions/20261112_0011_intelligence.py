"""Advanced intelligence (Phase 12): claim embeddings, evaluation runs, agent feedback, prompt selection.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-12

* ``claim_embeddings``: one 256-dimension vector per claim (pgvector, HNSW cosine index) for similar-claim
  search and near-duplicate detection. The embedding model id is stored with each vector.
* ``eval_runs``: regression runs of an agent for a candidate prompt version or model; activation of either
  requires a passed run (enforced in the services).
* ``agent_feedback``: labels on agent outputs, automatic (from human decisions) or given by people.
* ``agent_configs.prompt_version``: the prompt version an organisation runs for each agent (default v1).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = pg.UUID(as_uuid=True)
JSONB = pg.JSONB()
TS = sa.DateTime(timezone=True)
ORG_SETTING = "NULLIF(current_setting('app.org_id', true), '')::uuid"
NO_USER = "coalesce(created_by, '00000000-0000-0000-0000-000000000000'::uuid)"


def _org() -> sa.Column[object]:
    return sa.Column(
        "organization_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "claim_embeddings",
        sa.Column("claim_id", UUID, sa.ForeignKey("claims.id", ondelete="CASCADE"), primary_key=True),
        _org(),
        sa.Column("model", sa.String(40), nullable=False),
        sa.Column("text_sha256", sa.String(64), nullable=False),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.execute("ALTER TABLE claim_embeddings ADD COLUMN embedding vector(256) NOT NULL")
    op.execute(
        "CREATE INDEX ix_claim_embeddings_hnsw ON claim_embeddings "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    op.create_index("ix_claim_embeddings_org", "claim_embeddings", ["organization_id"])

    op.create_table(
        "eval_runs",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column("agent_key", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("prompt_version", sa.String(20), nullable=False),
        sa.Column("prompt_sha256", sa.String(64), nullable=False),
        sa.Column("model_id", UUID, sa.ForeignKey("model_registry.id", ondelete="SET NULL")),
        sa.Column("model_slug", sa.String(200)),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("cases", sa.Integer, nullable=False, server_default="0"),
        sa.Column("metrics", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("parts", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("regressions", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("model_calls", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.String(500)),
        sa.Column("requested_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", TS),
        sa.CheckConstraint("kind IN ('prompt', 'model', 'baseline')", name="kind"),
        sa.CheckConstraint("status IN ('queued', 'running', 'passed', 'failed', 'error')", name="status"),
    )
    op.create_index(
        "ix_eval_runs_lookup", "eval_runs", ["organization_id", "agent_key", "status", "created_at"]
    )

    op.create_table(
        "agent_feedback",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column("claim_id", UUID, sa.ForeignKey("claims.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_run_id", UUID, sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_key", sa.String(40), nullable=False),
        sa.Column("label", sa.String(20), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("note", sa.String(1000)),
        sa.Column("created_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "label IN ('agree', 'disagree', 'true_positive', 'false_positive', 'helpful', 'not_helpful')",
            name="label",
        ),
        sa.CheckConstraint("source IN ('review_decision', 'person')", name="source"),
    )
    op.create_index(
        "ix_agent_feedback_agent", "agent_feedback", ["organization_id", "agent_key", "created_at"]
    )
    op.execute(
        f"CREATE UNIQUE INDEX uq_agent_feedback_once ON agent_feedback (agent_run_id, source, {NO_USER})"
    )

    op.add_column(
        "agent_configs", sa.Column("prompt_version", sa.String(20), nullable=False, server_default="v1")
    )

    for table in ("claim_embeddings", "eval_runs", "agent_feedback"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING (organization_id = {ORG_SETTING}) "
            f"WITH CHECK (organization_id = {ORG_SETTING})"
        )


def downgrade() -> None:
    op.drop_column("agent_configs", "prompt_version")
    op.drop_table("agent_feedback")
    op.drop_table("eval_runs")
    op.drop_table("claim_embeddings")
