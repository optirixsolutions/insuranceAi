"""Organisations, users, roles, permissions and sessions.

Every tenant table carries ``organization_id`` and is protected by PostgreSQL
row-level security (see migration 0002). Child rows reference parents through
composite ``(organization_id, id)`` foreign keys so cross-tenant links are impossible.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    default_currency: Mapped[str] = mapped_column(String(3), server_default="USD")
    default_timezone: Mapped[str] = mapped_column(String(64), server_default="UTC")
    jurisdiction_code: Mapped[str | None] = mapped_column(String(16))
    ai_data_processing_approved: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    mfa_required_roles: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")  # Phase 13
    row_version: Mapped[int] = mapped_column(Integer, server_default="1")


class Permission(Base):
    """Global permission catalogue (not tenant-scoped)."""

    __tablename__ = "permissions"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(String(300))


class Role(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("organization_id", "key"),
        UniqueConstraint("organization_id", "id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(300), server_default="")
    is_system: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "role_id"], ["roles.organization_id", "roles.id"], ondelete="CASCADE"
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    permission_key: Mapped[str] = mapped_column(
        String(64), ForeignKey("permissions.key", ondelete="CASCADE"), primary_key=True
    )


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("organization_id", "id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(CITEXT)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), server_default="invited")
    access_token_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    access_token_purpose: Mapped[str | None] = mapped_column(String(20))
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, server_default="1")


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "user_id"], ["users.organization_id", "users.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["organization_id", "role_id"], ["roles.organization_id", "roles.id"], ondelete="CASCADE"
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class UserSession(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "user_sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "user_id"], ["users.organization_id", "users.id"], ondelete="CASCADE"
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    csrf_hash: Mapped[bytes] = mapped_column(LargeBinary)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(String(40))


class LoginAttempt(UUIDPrimaryKeyMixin, Base):
    """Pre-authentication record; the organisation may be unknown, so this table has no RLS."""

    __tablename__ = "login_attempts"
    # Inserted before a tenant is known: the session cannot read the row back, so the
    # INSERT must not use RETURNING (all values are produced client-side).
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    email_hash: Mapped[bytes] = mapped_column(LargeBinary, index=True)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(300))
    success: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=text("now()"), index=True
    )
