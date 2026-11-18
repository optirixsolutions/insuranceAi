"""The authenticated caller of a request."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: uuid.UUID
    organization_id: uuid.UUID
    session_id: uuid.UUID
    email: str
    display_name: str
    role_keys: frozenset[str]
    permissions: frozenset[str]
    csrf_hash: bytes = field(repr=False)
    # Phase 13: the organisation requires MFA for one of this user's roles and none is set up yet.
    # Every permission check refuses until it is (enrolment endpoints only need a session).
    mfa_enrollment_required: bool = False

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def holds_all(self, permissions: frozenset[str]) -> bool:
        return permissions <= self.permissions
