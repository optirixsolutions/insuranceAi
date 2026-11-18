"""Identity, tenancy (row-level security) and audit log.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = pg.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)
ORG_SETTING = "NULLIF(current_setting('app.org_id', true), '')::uuid"
# Frozen snapshot of the permission catalogue at this revision (later additions need new migrations).
PERMISSIONS_V1: dict[str, str] = {
    "org.manage": "Manage organisation profile and settings",
    "users.manage": "Invite, disable and assign roles to users",
    "claims.read": "View claims",
    "claims.write": "Create and edit claims",
    "claims.assign": "Assign claims to staff",
    "documents.read": "View claim documents",
    "documents.upload": "Upload claim documents",
    "fields.correct": "Correct extracted document fields",
    "processing.start": "Start AI processing of a claim",
    "processing.cancel": "Cancel AI processing of a claim",
    "fraud.investigate": "Work fraud investigations",
    "reviews.decide": "Approve or reject claims within assigned authority",
    "reviews.escalate": "Escalate claims for review",
    "policies.manage": "Create and edit policies",
    "products.manage": "Configure insurance products",
    "rules.publish": "Publish versioned rule sets",
    "ai.manage": "Configure models, prompts and agents",
    "compliance.manage": "Manage compliance profiles",
    "audit.read": "Read the audit log",
    "reports.generate": "Generate claim reports",
}
TENANT_TABLES = ("roles", "role_permissions", "users", "user_roles", "user_sessions", "audit_events")


def _id() -> sa.Column[object]:
    return sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
    ]


def _org_fk(table: str) -> sa.Column[object]:
    return sa.Column(
        "organization_id",
        UUID,
        sa.ForeignKey("organizations.id", ondelete="CASCADE", name=f"fk_{table}_organization"),
        nullable=False,
    )


def _updated_at_trigger(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION claimos_set_updated_at()"
    )


def _grant_if_app_role(sql: str) -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'claimos_app') THEN "
        f"EXECUTE {sql!r}; END IF; END $$"
    )


def upgrade() -> None:
    op.create_table(
        "organizations",
        _id(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("default_currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("default_timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("jurisdiction_code", sa.String(16)),
        sa.Column("ai_data_processing_approved", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("row_version", sa.Integer, nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint("status IN ('active', 'suspended')", name="status"),
        sa.CheckConstraint("default_currency ~ '^[A-Z]{3}$'", name="currency"),
    )

    op.create_table(
        "permissions",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("description", sa.String(300), nullable=False),
    )
    op.bulk_insert(
        sa.table("permissions", sa.column("key", sa.String), sa.column("description", sa.String)),
        [{"key": k, "description": d} for k, d in PERMISSIONS_V1.items()],
    )

    op.create_table(
        "roles",
        _id(),
        _org_fk("roles"),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(300), nullable=False, server_default=""),
        sa.Column("is_system", sa.Boolean, nullable=False, server_default=sa.false()),
        *_timestamps(),
        sa.UniqueConstraint("organization_id", "key", name="uq_roles_org_key"),
        sa.UniqueConstraint("organization_id", "id", name="uq_roles_org_id"),
    )

    op.create_table(
        "role_permissions",
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("role_id", UUID, primary_key=True),
        sa.Column(
            "permission_key",
            sa.String(64),
            sa.ForeignKey("permissions.key", ondelete="CASCADE", name="fk_role_permissions_permission"),
            primary_key=True,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "role_id"],
            ["roles.organization_id", "roles.id"],
            ondelete="CASCADE",
            name="fk_role_permissions_role",
        ),
    )

    op.create_table(
        "users",
        _id(),
        _org_fk("users"),
        sa.Column("email", pg.CITEXT, nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("password_hash", sa.Text),
        sa.Column("status", sa.String(20), nullable=False, server_default="invited"),
        sa.Column("access_token_hash", sa.LargeBinary),
        sa.Column("access_token_purpose", sa.String(20)),
        sa.Column("access_token_expires_at", TS),
        sa.Column("last_login_at", TS),
        sa.Column("password_changed_at", TS),
        sa.Column("deleted_at", TS),
        sa.Column("row_version", sa.Integer, nullable=False, server_default="1"),
        *_timestamps(),
        sa.UniqueConstraint("organization_id", "id", name="uq_users_org_id"),
        sa.CheckConstraint("status IN ('invited', 'active', 'disabled')", name="status"),
        sa.CheckConstraint("access_token_purpose IN ('invite', 'reset')", name="token_purpose"),
    )
    # Email is the login identifier, so it is unique across all organisations.
    op.create_index(
        "uq_users_email_active",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_users_org_status", "users", ["organization_id", "status"])
    op.create_index(
        "uq_users_access_token",
        "users",
        ["access_token_hash"],
        unique=True,
        postgresql_where=sa.text("access_token_hash IS NOT NULL"),
    )

    op.create_table(
        "user_roles",
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("user_id", UUID, primary_key=True),
        sa.Column("role_id", UUID, primary_key=True),
        sa.Column("granted_by", UUID),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["users.organization_id", "users.id"],
            ondelete="CASCADE",
            name="fk_user_roles_user",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "role_id"],
            ["roles.organization_id", "roles.id"],
            ondelete="CASCADE",
            name="fk_user_roles_role",
        ),
    )
    op.create_index("ix_user_roles_org", "user_roles", ["organization_id"])

    op.create_table(
        "user_sessions",
        _id(),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("token_hash", sa.LargeBinary, nullable=False, unique=True),
        sa.Column("csrf_hash", sa.LargeBinary, nullable=False),
        sa.Column("ip", pg.INET),
        sa.Column("user_agent", sa.String(300)),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("idle_expires_at", TS, nullable=False),
        sa.Column("absolute_expires_at", TS, nullable=False),
        sa.Column("revoked_at", TS),
        sa.Column("revoke_reason", sa.String(40)),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["users.organization_id", "users.id"],
            ondelete="CASCADE",
            name="fk_user_sessions_user",
        ),
    )
    op.create_index("ix_user_sessions_user", "user_sessions", ["user_id"])

    op.create_table(
        "login_attempts",
        _id(),
        sa.Column("organization_id", UUID),
        sa.Column("user_id", UUID),
        sa.Column("email_hash", sa.LargeBinary, nullable=False),
        sa.Column("ip", pg.INET),
        sa.Column("user_agent", sa.String(300)),
        sa.Column("success", sa.Boolean, nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_login_attempts_email", "login_attempts", ["email_hash", "created_at"])
    op.create_index("ix_login_attempts_user", "login_attempts", ["user_id", "created_at"])

    op.create_table(
        "audit_events",
        _id(),
        sa.Column(
            "organization_id",
            UUID,
            sa.ForeignKey("organizations.id", ondelete="RESTRICT", name="fk_audit_events_organization"),
            nullable=False,
        ),
        sa.Column("seq", sa.BigInteger, nullable=False),
        sa.Column("occurred_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("actor_type", sa.String(20), nullable=False),
        sa.Column("actor_id", UUID),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("entity_type", sa.String(60), nullable=False),
        sa.Column("entity_id", UUID),
        sa.Column("request_id", sa.String(64)),
        sa.Column("ip", pg.INET),
        sa.Column("before", pg.JSONB),
        sa.Column("after", pg.JSONB),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("organization_id", "seq", name="uq_audit_events_org_seq"),
        sa.CheckConstraint("actor_type IN ('user', 'system', 'agent', 'integration')", name="actor_type"),
    )
    op.create_index("ix_audit_events_entity", "audit_events", ["organization_id", "entity_type", "entity_id"])
    op.create_index("ix_audit_events_actor", "audit_events", ["organization_id", "actor_id"])
    op.execute(
        "CREATE TRIGGER trg_audit_events_append_only BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION claimos_forbid_mutation()"
    )

    for table in ("organizations", "roles", "users"):
        _updated_at_trigger(table)

    # ---- Row-level security ------------------------------------------------------------
    op.execute("ALTER TABLE organizations ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON organizations USING (id = {ORG_SETTING}) "
        f"WITH CHECK (id = {ORG_SETTING})"
    )
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING (organization_id = {ORG_SETTING}) "
            f"WITH CHECK (organization_id = {ORG_SETTING})"
        )
    # Login attempts are written before the organisation is known; reads are tenant-scoped.
    op.execute("ALTER TABLE login_attempts ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY login_attempts_insert ON login_attempts FOR INSERT WITH CHECK (true)")
    op.execute(
        f"CREATE POLICY login_attempts_read ON login_attempts FOR SELECT "
        f"USING (organization_id = {ORG_SETTING})"
    )

    # ---- Pre-authentication lookups --------------------------------------------------------
    # SECURITY DEFINER functions return identifiers only, so the API can resolve the tenant
    # for a login/session/token before RLS context exists. They expose no other columns.
    # One statement per execute: asyncpg does not accept multiple commands in one call.
    op.execute(
        """
        CREATE FUNCTION claimos_auth_find_user(p_email citext)
        RETURNS TABLE (user_id uuid, organization_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT u.id, u.organization_id FROM users u
            WHERE u.email = p_email AND u.deleted_at IS NULL
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION claimos_auth_find_session(p_token_hash bytea)
        RETURNS TABLE (session_id uuid, user_id uuid, organization_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT s.id, s.user_id, s.organization_id FROM user_sessions s
            WHERE s.token_hash = p_token_hash AND s.revoked_at IS NULL
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION claimos_auth_find_access_token(p_token_hash bytea)
        RETURNS TABLE (user_id uuid, organization_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT u.id, u.organization_id FROM users u
            WHERE u.access_token_hash = p_token_hash AND u.deleted_at IS NULL
        $$
        """
    )
    for fn in (
        "claimos_auth_find_user(citext)",
        "claimos_auth_find_session(bytea)",
        "claimos_auth_find_access_token(bytea)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        _grant_if_app_role(f"GRANT EXECUTE ON FUNCTION {fn} TO claimos_app")

    # ---- Privileges for the runtime role ------------------------------------------------------
    _grant_if_app_role("REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON permissions FROM claimos_app")
    _grant_if_app_role("REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM claimos_app")
    _grant_if_app_role("REVOKE UPDATE, DELETE, TRUNCATE ON login_attempts FROM claimos_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS claimos_auth_find_access_token(bytea)")
    op.execute("DROP FUNCTION IF EXISTS claimos_auth_find_session(bytea)")
    op.execute("DROP FUNCTION IF EXISTS claimos_auth_find_user(citext)")
    for table in (
        "audit_events",
        "login_attempts",
        "user_sessions",
        "user_roles",
        "users",
        "role_permissions",
        "roles",
        "permissions",
        "organizations",
    ):
        op.drop_table(table)
