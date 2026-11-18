"""Human review: review items, reviewer authorities, four-eyes pending decisions.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-10

* ``human_reviews``: one per HUMAN_REVIEW interrupt (unique per execution + interrupt id);
  at most one open review per claim.
* ``reviewer_authorities``: approval limits per user (optionally per product) with validity dates.
* ``pending_decisions``: a first decision that needs a second approver waits here (one per
  review); the final ``claim_decisions`` row is written only when a different authorised person
  confirms it.
* ``claim_decisions`` gains review, reason and second-approver columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = pg.UUID(as_uuid=True)
JSONB = pg.JSONB()
TS = sa.DateTime(timezone=True)
ORG_SETTING = "NULLIF(current_setting('app.org_id', true), '')::uuid"
OPEN = "('pending', 'in_progress', 'awaiting_second_approval', 'escalated')"


def _org() -> sa.Column[object]:
    return sa.Column(
        "organization_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "human_reviews",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column("claim_id", UUID, sa.ForeignKey("claims.id", ondelete="CASCADE"), nullable=False),
        sa.Column("execution_id", UUID, sa.ForeignKey("workflow_executions.id"), nullable=False),
        sa.Column("interrupt_id", sa.String(64), nullable=False),
        sa.Column("recommendation", sa.String(40)),
        sa.Column("payout", sa.Numeric(19, 4)),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("fraud_level", sa.String(20)),
        sa.Column("reasons", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("assigned_to", UUID, sa.ForeignKey("users.id")),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("priority_score", sa.Integer, nullable=False, server_default="0"),
        sa.Column("due_at", TS),
        sa.Column("four_eyes_required", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("outcome", sa.String(30)),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", TS),
        sa.Column("row_version", sa.Integer, nullable=False, server_default="1"),
        sa.UniqueConstraint("execution_id", "interrupt_id", name="uq_human_reviews_interrupt"),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'awaiting_second_approval', 'completed', "
            "'escalated', 'cancelled')",
            name="status",
        ),
    )
    op.create_index(
        "ix_human_reviews_queue", "human_reviews", ["organization_id", "status", "priority_score"]
    )
    op.create_index(
        "uq_human_reviews_one_open",
        "human_reviews",
        ["claim_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN {OPEN}"),
    )

    op.create_table(
        "reviewer_authorities",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", UUID, sa.ForeignKey("insurance_products.id", ondelete="CASCADE")),
        sa.Column("max_approval_amount", sa.Numeric(19, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("can_reject", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("can_override_fraud", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("valid_from", sa.Date, nullable=False),
        sa.Column("valid_to", sa.Date),
        sa.Column("granted_by", UUID, sa.ForeignKey("users.id")),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("max_approval_amount >= 0", name="amount"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="validity"),
    )
    op.create_index("ix_reviewer_authorities_user", "reviewer_authorities", ["organization_id", "user_id"])

    op.create_table(
        "pending_decisions",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column(
            "review_id",
            UUID,
            sa.ForeignKey("human_reviews.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("decision", sa.String(30), nullable=False),
        sa.Column("approved_amount", sa.Numeric(19, 4)),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("reason_code", sa.String(60)),
        sa.Column("override", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("override_reason", sa.String(1000)),
        sa.Column("clause_ids", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("first_decider", UUID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("decision IN ('approved', 'rejected')", name="decision"),
    )

    for column in (
        sa.Column("review_id", UUID, sa.ForeignKey("human_reviews.id")),
        sa.Column("second_approver_id", UUID, sa.ForeignKey("users.id")),
        sa.Column("reason", sa.String(1000)),
        sa.Column("reason_code", sa.String(60)),
        sa.Column("clause_ids", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    ):
        op.add_column("claim_decisions", column)

    for table in ("human_reviews", "reviewer_authorities", "pending_decisions"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING (organization_id = {ORG_SETTING}) "
            f"WITH CHECK (organization_id = {ORG_SETTING})"
        )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'claimos_app') THEN "
        "EXECUTE 'REVOKE DELETE, TRUNCATE ON human_reviews FROM claimos_app'; END IF; END $$"
    )


def downgrade() -> None:
    for column in ("clause_ids", "reason_code", "reason", "second_approver_id", "review_id"):
        op.drop_column("claim_decisions", column)
    op.drop_table("pending_decisions")
    op.drop_table("reviewer_authorities")
    op.drop_table("human_reviews")
