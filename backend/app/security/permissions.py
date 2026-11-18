"""Permission catalogue and built-in organisation roles.

This module is the single source of truth: migration 0002 seeds ``permissions``
from ``PERMISSIONS`` and registration creates one row per ``SYSTEM_ROLES`` entry.
Object-level qualifiers ("only assigned claims") are enforced by access policies
in later phases, on top of these coarse permissions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class P(StrEnum):
    ORG_MANAGE = "org.manage"
    USERS_MANAGE = "users.manage"
    CLAIMS_READ = "claims.read"
    CLAIMS_WRITE = "claims.write"
    CLAIMS_ASSIGN = "claims.assign"
    DOCUMENTS_READ = "documents.read"
    DOCUMENTS_UPLOAD = "documents.upload"
    FIELDS_CORRECT = "fields.correct"
    PROCESSING_START = "processing.start"
    PROCESSING_CANCEL = "processing.cancel"
    FRAUD_INVESTIGATE = "fraud.investigate"
    REVIEWS_DECIDE = "reviews.decide"
    REVIEWS_ESCALATE = "reviews.escalate"
    POLICIES_MANAGE = "policies.manage"
    PRODUCTS_MANAGE = "products.manage"
    RULES_PUBLISH = "rules.publish"
    AI_MANAGE = "ai.manage"
    COMPLIANCE_MANAGE = "compliance.manage"
    AUDIT_READ = "audit.read"
    REPORTS_GENERATE = "reports.generate"


PERMISSIONS: dict[P, str] = {
    P.ORG_MANAGE: "Manage organisation profile and settings",
    P.USERS_MANAGE: "Invite, disable and assign roles to users",
    P.CLAIMS_READ: "View claims",
    P.CLAIMS_WRITE: "Create and edit claims",
    P.CLAIMS_ASSIGN: "Assign claims to staff",
    P.DOCUMENTS_READ: "View claim documents",
    P.DOCUMENTS_UPLOAD: "Upload claim documents",
    P.FIELDS_CORRECT: "Correct extracted document fields",
    P.PROCESSING_START: "Start AI processing of a claim",
    P.PROCESSING_CANCEL: "Cancel AI processing of a claim",
    P.FRAUD_INVESTIGATE: "Work fraud investigations",
    P.REVIEWS_DECIDE: "Approve or reject claims within assigned authority",
    P.REVIEWS_ESCALATE: "Escalate claims for review",
    P.POLICIES_MANAGE: "Create and edit policies",
    P.PRODUCTS_MANAGE: "Configure insurance products",
    P.RULES_PUBLISH: "Publish versioned rule sets",
    P.AI_MANAGE: "Configure models, prompts and agents",
    P.COMPLIANCE_MANAGE: "Manage compliance profiles",
    P.AUDIT_READ: "Read the audit log",
    P.REPORTS_GENERATE: "Generate claim reports",
}


@dataclass(frozen=True, slots=True)
class RoleDefinition:
    key: str
    name: str
    description: str
    permissions: frozenset[P]


_ALL = frozenset(P)

SYSTEM_ROLES: tuple[RoleDefinition, ...] = (
    RoleDefinition("org_admin", "Organization Administrator", "Full control of the organisation", _ALL),
    RoleDefinition(
        "claims_manager",
        "Claims Manager",
        "Runs the claims operation and decides claims within authority",
        frozenset(
            {
                P.CLAIMS_READ,
                P.CLAIMS_WRITE,
                P.CLAIMS_ASSIGN,
                P.DOCUMENTS_READ,
                P.DOCUMENTS_UPLOAD,
                P.FIELDS_CORRECT,
                P.PROCESSING_START,
                P.PROCESSING_CANCEL,
                P.REVIEWS_DECIDE,
                P.REVIEWS_ESCALATE,
                P.POLICIES_MANAGE,
                P.REPORTS_GENERATE,
            }
        ),
    ),
    RoleDefinition(
        "claims_adjuster",
        "Claims Adjuster",
        "Handles assigned claims",
        frozenset(
            {
                P.CLAIMS_READ,
                P.CLAIMS_WRITE,
                P.DOCUMENTS_READ,
                P.DOCUMENTS_UPLOAD,
                P.FIELDS_CORRECT,
                P.PROCESSING_START,
                P.PROCESSING_CANCEL,
                P.REVIEWS_ESCALATE,
                P.REPORTS_GENERATE,
            }
        ),
    ),
    RoleDefinition(
        "fraud_investigator",
        "Fraud Investigator",
        "Investigates flagged claims",
        frozenset({P.CLAIMS_READ, P.DOCUMENTS_READ, P.FRAUD_INVESTIGATE, P.REVIEWS_ESCALATE}),
    ),
    RoleDefinition(
        "compliance_officer",
        "Compliance Officer",
        "Oversees compliance profiles and audit",
        frozenset(
            {
                P.CLAIMS_READ,
                P.DOCUMENTS_READ,
                P.COMPLIANCE_MANAGE,
                P.AUDIT_READ,
                P.REVIEWS_ESCALATE,
                P.REPORTS_GENERATE,
            }
        ),
    ),
    RoleDefinition(
        "reviewer",
        "Reviewer",
        "Decides claims within assigned authority",
        frozenset(
            {P.CLAIMS_READ, P.DOCUMENTS_READ, P.REVIEWS_DECIDE, P.REVIEWS_ESCALATE, P.REPORTS_GENERATE}
        ),
    ),
    RoleDefinition(
        "auditor",
        "Read-only Auditor",
        "Read-only access to claims and the audit log",
        frozenset({P.CLAIMS_READ, P.DOCUMENTS_READ, P.AUDIT_READ}),
    ),
)

SYSTEM_ROLE_KEYS = frozenset(r.key for r in SYSTEM_ROLES)
ADMIN_ROLE_KEY = "org_admin"


def role_definition(key: str) -> RoleDefinition | None:
    return next((r for r in SYSTEM_ROLES if r.key == key), None)
