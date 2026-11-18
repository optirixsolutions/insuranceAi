from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.catalog import Address, Amount, Currency
from rules_engine.money import to_minor_units

Priority = Literal["low", "normal", "high", "urgent"]


class Money(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: Amount
    currency: Currency


class MoneyOut(BaseModel):
    """Amounts leave the API in the currency's minor units, whatever the storage precision."""

    amount: Decimal
    currency: str

    @model_validator(mode="after")
    def _minor_units(self) -> MoneyOut:
        self.amount = to_minor_units(self.amount, self.currency)
        return self


class IncidentLocation(Address):
    description: Annotated[str, StringConstraints(max_length=300)] | None = None


class ClaimCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: uuid.UUID
    policy_id: uuid.UUID
    loss_type: Annotated[str, StringConstraints(pattern=r"^[a-z_]{2,40}$")]
    incident_date: date
    incident_time: time | None = None
    incident_location: IncidentLocation | None = None
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=5000)]
    claimed_amount: Money
    channel: Literal["staff", "portal", "email", "api"] = "staff"
    is_synthetic: bool = False


class ClaimUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=5000)] | None
    ) = None
    loss_type: Annotated[str, StringConstraints(pattern=r"^[a-z_]{2,40}$")] | None = None
    incident_date: date | None = None
    incident_time: time | None = None
    incident_location: IncidentLocation | None = None
    claimed_amount: Amount | None = None
    priority: Priority | None = None


class StatusChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=1000)]


class AssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["adjuster", "reviewer", "investigator"]
    user_id: uuid.UUID | None


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]
    visibility: Literal["internal", "restricted"] = "internal"


class Finding(BaseModel):
    code: str
    severity: str
    message: str


class AssignmentOut(BaseModel):
    role_on_claim: str
    user_id: uuid.UUID
    display_name: str
    assigned_at: datetime


class ClaimListItem(BaseModel):
    id: uuid.UUID
    claim_reference: str
    status: str
    priority: str
    loss_type: str
    incident_date: date
    claimed_amount: MoneyOut
    customer_id: uuid.UUID
    customer_name: str
    policy_id: uuid.UUID
    policy_number: str
    product_name: str
    product_line: str
    adjuster_name: str | None
    is_synthetic: bool
    created_at: datetime


class PolicyVersionRef(BaseModel):
    id: uuid.UUID
    version_no: int
    effective_from: date
    effective_to: date
    sum_insured: Decimal
    deductible_amount: Decimal
    currency: str

    @model_validator(mode="after")
    def _minor_units(self) -> PolicyVersionRef:
        self.sum_insured = to_minor_units(self.sum_insured, self.currency)
        self.deductible_amount = to_minor_units(self.deductible_amount, self.currency)
        return self


class ClaimDetail(ClaimListItem):
    incident_time: time | None
    incident_location: dict[str, Any] | None
    description: str
    channel: str
    reported_at: datetime
    policy_version: PolicyVersionRef | None
    findings: list[Finding]
    assignments: list[AssignmentOut]
    allowed_status_changes: list[str]
    required_documents: list[str]
    documents_received: list[str]
    row_version: int
    can_edit: bool


class ClaimCreated(ClaimDetail):
    missing_documents: list[str]
    duplicate_suspects: list[dict[str, str]]


class NoteOut(BaseModel):
    id: uuid.UUID
    body: str
    visibility: str
    author_id: uuid.UUID
    author_name: str
    created_at: datetime


class TimelineEntryOut(BaseModel):
    at: datetime
    kind: str
    title: str
    detail: str | None
    actor_name: str | None


class RelatedClaimOut(BaseModel):
    id: uuid.UUID
    claim_reference: str
    status: str
    incident_date: date
    relation: str
    source: str
    detail: str


class AssigneeOut(BaseModel):
    id: uuid.UUID
    display_name: str
    email: str


class LossTypesOut(BaseModel):
    lines: dict[str, list[str]] = Field(default_factory=dict)
