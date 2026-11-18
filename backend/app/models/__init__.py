"""ORM models. Import every model module here so Alembic autogenerate sees it."""

from app.models.agents import AgentOutput, AgentRun, PromptVersion
from app.models.ai import AgentConfig, ModelInvocation, ModelRegistryEntry
from app.models.audit import AuditEvent
from app.models.base import Base
from app.models.catalog import (
    Customer,
    DocumentRequirement,
    InsuranceProduct,
    Policy,
    PolicyClause,
    PolicyCoverage,
    PolicyExclusion,
    PolicyVersion,
)
from app.models.claims import (
    Claim,
    ClaimAssignment,
    ClaimNote,
    ClaimReferenceCounter,
    ClaimRelation,
    ClaimStatusChange,
)
from app.models.communications import (
    Notification,
    ReportVersion,
    WebhookDelivery,
    WebhookSubscription,
)
from app.models.documents import (
    ClaimDocument,
    DocumentExtraction,
    DocumentPage,
    ExtractedField,
    FieldCorrection,
)
from app.models.identity import (
    LoginAttempt,
    Organization,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
    UserSession,
)
from app.models.intelligence import AgentFeedback, EvalRun
from app.models.platform import IdempotencyKey
from app.models.rules import (
    ClaimDecision,
    HumanReview,
    PendingDecision,
    ReviewerAuthority,
    RuleSet,
)
from app.models.security import AuditVerification, MfaChallenge, RetentionRun, UserMfa
from app.models.workflow import WorkflowCheckpoint, WorkflowEvent, WorkflowExecution

__all__ = [
    "AgentConfig",
    "AgentFeedback",
    "AgentOutput",
    "AgentRun",
    "AuditEvent",
    "AuditVerification",
    "Base",
    "Claim",
    "ClaimAssignment",
    "ClaimDecision",
    "ClaimDocument",
    "ClaimNote",
    "ClaimReferenceCounter",
    "ClaimRelation",
    "ClaimStatusChange",
    "Customer",
    "DocumentExtraction",
    "DocumentPage",
    "DocumentRequirement",
    "EvalRun",
    "ExtractedField",
    "FieldCorrection",
    "HumanReview",
    "IdempotencyKey",
    "InsuranceProduct",
    "LoginAttempt",
    "MfaChallenge",
    "ModelInvocation",
    "ModelRegistryEntry",
    "Notification",
    "Organization",
    "PendingDecision",
    "Permission",
    "Policy",
    "PolicyClause",
    "PolicyCoverage",
    "PolicyExclusion",
    "PolicyVersion",
    "PromptVersion",
    "ReportVersion",
    "RetentionRun",
    "ReviewerAuthority",
    "Role",
    "RolePermission",
    "RuleSet",
    "User",
    "UserMfa",
    "UserRole",
    "UserSession",
    "WebhookDelivery",
    "WebhookSubscription",
    "WorkflowCheckpoint",
    "WorkflowEvent",
    "WorkflowExecution",
]
