"""Baseline: extensions and shared trigger functions.

Revision ID: 0001
Revises:
Create Date: 2026-11-04

Domain tables arrive in later phases. This revision installs what every later
migration relies on:

* pgcrypto (gen_random_uuid), citext (emails), pg_trgm (search), vector (Phase 12)
* claimos_set_updated_at(): BEFORE UPDATE trigger maintaining updated_at
* claimos_forbid_mutation(): trigger used by append-only tables (audit, decisions, events)
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXTENSIONS = ("pgcrypto", "citext", "pg_trgm", "vector")


def upgrade() -> None:
    for ext in EXTENSIONS:
        op.execute(f'CREATE EXTENSION IF NOT EXISTS "{ext}"')

    op.execute(
        """
        CREATE OR REPLACE FUNCTION claimos_set_updated_at() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION claimos_forbid_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'table % is append-only (% not permitted)', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS claimos_forbid_mutation()")
    op.execute("DROP FUNCTION IF EXISTS claimos_set_updated_at()")
    # Extensions are intentionally kept: other database objects may depend on them.
