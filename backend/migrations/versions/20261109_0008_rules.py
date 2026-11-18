"""Rule sets (versioned product configuration) and claim decisions (STP now, humans in Phase 9).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-09

* ``rule_sets``: drafts are editable; a published version is immutable and can only be retired;
  at most one published version per product (publishing retires the previous one).
* ``claim_decisions``: append-only; ``decided_by`` may be null only for ``decision_mode = 'stp'``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = pg.UUID(as_uuid=True)
JSONB = pg.JSONB()
TS = sa.DateTime(timezone=True)
ORG_SETTING = "NULLIF(current_setting('app.org_id', true), '')::uuid"


def _org() -> sa.Column[object]:
    return sa.Column(
        "organization_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "rule_sets",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column(
            "product_id", UUID, sa.ForeignKey("insurance_products.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("version_no", sa.Integer, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("config", JSONB, nullable=False),
        sa.Column("config_sha256", sa.String(64), nullable=False),
        sa.Column("change_note", sa.String(500), nullable=False, server_default=""),
        sa.Column("created_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("published_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("published_at", TS),
        sa.Column("row_version", sa.Integer, nullable=False, server_default="1"),
        sa.UniqueConstraint("product_id", "version_no", name="uq_rule_sets_product_version"),
        sa.CheckConstraint("status IN ('draft', 'published', 'retired')", name="status"),
    )
    op.create_index("ix_rule_sets_org_product", "rule_sets", ["organization_id", "product_id"])
    op.create_index(
        "uq_rule_sets_one_published",
        "rule_sets",
        ["product_id"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )
    op.execute(
        "CREATE FUNCTION claimos_rule_set_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF TG_OP = 'DELETE' THEN "
        "  IF OLD.status = 'draft' THEN RETURN OLD; END IF; "
        "  RAISE EXCEPTION 'published rule sets cannot be deleted' USING ERRCODE = 'P0001'; END IF; "
        "IF OLD.status = 'draft' THEN RETURN NEW; END IF; "
        "IF OLD.status = 'published' AND NEW.status = 'retired' AND NEW.config = OLD.config "
        "AND NEW.version_no = OLD.version_no AND NEW.product_id = OLD.product_id THEN RETURN NEW; END IF; "
        "RAISE EXCEPTION 'rule set version % is % and immutable', OLD.version_no, OLD.status "
        "USING ERRCODE = 'P0001'; END $$"
    )
    op.execute(
        "CREATE TRIGGER trg_rule_sets_guard BEFORE UPDATE OR DELETE ON rule_sets "
        "FOR EACH ROW EXECUTE FUNCTION claimos_rule_set_guard()"
    )

    op.create_table(
        "claim_decisions",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column("claim_id", UUID, sa.ForeignKey("claims.id", ondelete="CASCADE"), nullable=False),
        sa.Column("execution_id", UUID, sa.ForeignKey("workflow_executions.id")),
        sa.Column("decision", sa.String(30), nullable=False),
        sa.Column("approved_amount", sa.Numeric(19, 4)),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("decided_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("decision_mode", sa.String(10), nullable=False),
        sa.Column("override", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("override_reason", sa.String(1000)),
        sa.Column("rule_set_id", UUID, sa.ForeignKey("rule_sets.id")),
        sa.Column("gates", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("before_state", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("after_state", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected', 'more_information', 'escalated', 'returned')",
            name="decision",
        ),
        sa.CheckConstraint("decision_mode IN ('human', 'stp')", name="mode"),
        sa.CheckConstraint("decided_by IS NOT NULL OR decision_mode = 'stp'", name="decider"),
        sa.CheckConstraint("decision_mode = 'human' OR decision = 'approved'", name="stp_only_approves"),
    )
    op.create_index(
        "ix_claim_decisions_org_claim", "claim_decisions", ["organization_id", "claim_id", "created_at"]
    )
    op.execute(
        "CREATE TRIGGER trg_claim_decisions_append_only BEFORE UPDATE OR DELETE ON claim_decisions "
        "FOR EACH ROW EXECUTE FUNCTION claimos_forbid_mutation()"
    )
    for table in ("rule_sets", "claim_decisions"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING (organization_id = {ORG_SETTING}) "
            f"WITH CHECK (organization_id = {ORG_SETTING})"
        )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'claimos_app') THEN "
        "EXECUTE 'REVOKE UPDATE, DELETE, TRUNCATE ON claim_decisions FROM claimos_app'; END IF; END $$"
    )


def downgrade() -> None:
    op.drop_table("claim_decisions")
    op.drop_table("rule_sets")
    op.execute("DROP FUNCTION IF EXISTS claimos_rule_set_guard()")
