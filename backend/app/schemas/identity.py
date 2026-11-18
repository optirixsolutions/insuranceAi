from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.common import Email, IpAddress, Name, Password

OrgName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=200)]


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_name: OrgName
    display_name: Name
    email: Email
    password: Password


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: Email
    password: Password


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: Password
    new_password: Password


class AcceptAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: Annotated[str, StringConstraints(min_length=20, max_length=200)]
    password: Password


class MeUser(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str


class MeOrganization(BaseModel):
    id: uuid.UUID
    name: str
    slug: str


class MeResponse(BaseModel):
    user: MeUser
    organization: MeOrganization
    roles: list[str]
    permissions: list[str]
    session_expires_at: datetime | None = None


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    ip: IpAddress
    user_agent: str | None
    created_at: datetime
    last_seen_at: datetime
    current: bool = False


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    slug: str
    status: str
    default_currency: str
    default_timezone: str
    jurisdiction_code: str | None
    ai_data_processing_approved: bool
    row_version: int


class OrganizationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: OrgName | None = None
    default_currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    default_timezone: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    jurisdiction_code: Annotated[str, StringConstraints(max_length=16)] | None = None


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    status: Literal["invited", "active", "disabled"]
    roles: list[str]
    last_login_at: datetime | None
    created_at: datetime
    row_version: int


class InviteUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: Email
    display_name: Name
    roles: Annotated[
        list[Annotated[str, StringConstraints(max_length=64)]], Field(min_length=1, max_length=10)
    ]


class AccessLinkOut(BaseModel):
    url: str
    purpose: Literal["invite", "reset"]
    expires_at: datetime


class InviteUserResponse(BaseModel):
    user: UserOut
    access_link: AccessLinkOut


class UpdateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: Name | None = None
    status: Literal["active", "disabled"] | None = None


class SetRolesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: Annotated[
        list[Annotated[str, StringConstraints(max_length=64)]], Field(min_length=1, max_length=10)
    ]


class LoginActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    created_at: datetime
    success: bool
    reason: str
    ip: IpAddress
    user_agent: str | None


class RoleOut(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    description: str
    is_system: bool
    permissions: list[str]


class PermissionOut(BaseModel):
    key: str
    description: str


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    seq: int
    occurred_at: datetime
    actor_type: str
    actor_id: uuid.UUID | None
    actor_name: str | None = None
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    request_id: str | None
    ip: IpAddress
    before: dict[str, object] | None
    after: dict[str, object] | None


class AuditVerificationOut(BaseModel):
    valid: bool
    events_checked: int
    first_invalid_seq: int | None
    reason: str | None
