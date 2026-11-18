from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Code = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Z0-9_\-]{2,40}$")]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Key = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z0-9_]{2,40}$")]
ClauseRef = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
Amount = Annotated[Decimal, Field(max_digits=19, decimal_places=4)]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Line = Literal["motor", "health", "property", "travel"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- Products -----------------------------------------------------------------------------


class RequirementIn(Strict):
    document_type: Key
    mandatory: bool = True
    description: Annotated[str, StringConstraints(max_length=300)] = ""


class ProductCreate(Strict):
    code: Code
    name: ShortText
    line: Line
    currency: Currency
    description: Annotated[str, StringConstraints(max_length=2000)] = ""
    document_requirements: list[RequirementIn] | None = None


class ProductUpdate(Strict):
    name: ShortText | None = None
    description: Annotated[str, StringConstraints(max_length=2000)] | None = None
    status: Literal["active", "retired"] | None = None


class RequirementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    document_type: str
    mandatory: bool
    description: str


class ProductOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    line: str
    currency: str
    description: str
    status: str
    row_version: int
    document_requirements: list[RequirementOut]


# ---- Customers ----------------------------------------------------------------------------


class Address(Strict):
    line1: Annotated[str, StringConstraints(max_length=200)] | None = None
    line2: Annotated[str, StringConstraints(max_length=200)] | None = None
    city: Annotated[str, StringConstraints(max_length=100)] | None = None
    region: Annotated[str, StringConstraints(max_length=100)] | None = None
    postal_code: Annotated[str, StringConstraints(max_length=20)] | None = None
    country: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")] | None = None


class CustomerCreate(Strict):
    full_name: ShortText
    customer_type: Literal["individual", "business"] = "individual"
    external_ref: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] | None = None
    date_of_birth: date | None = None
    email: Annotated[str, StringConstraints(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")] | None = (
        None
    )
    phone: Annotated[str, StringConstraints(max_length=40, pattern=r"^[0-9+()\-\s]{5,40}$")] | None = None
    address: Address | None = None
    is_synthetic: bool = False


class CustomerUpdate(Strict):
    full_name: ShortText | None = None
    date_of_birth: date | None = None
    email: Annotated[str, StringConstraints(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")] | None = (
        None
    )
    phone: Annotated[str, StringConstraints(max_length=40, pattern=r"^[0-9+()\-\s]{5,40}$")] | None = None
    address: Address | None = None


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    full_name: str
    customer_type: str
    external_ref: str | None
    date_of_birth: date | None
    email: str | None
    phone: str | None
    address: dict[str, Any] | None
    is_synthetic: bool
    row_version: int


# ---- Policies -----------------------------------------------------------------------------


class ClauseIn(Strict):
    clause_ref: ClauseRef
    section: Literal["coverage", "exclusion", "condition", "definition", "endorsement"]
    title: ShortText
    text: Annotated[str, StringConstraints(min_length=1, max_length=20000)]


class CoverageIn(Strict):
    coverage_code: Key
    perils: Annotated[list[Key], Field(min_length=1, max_length=30)]
    limit_amount: Amount
    clause_ref: ClauseRef
    deductible_override: Amount | None = None
    co_pay_pct: Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)] | None = None


class ExclusionIn(Strict):
    exclusion_code: Key
    applies_to: Annotated[list[Key], Field(min_length=1, max_length=30)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    clause_ref: ClauseRef


class VersionIn(Strict):
    effective_from: date
    effective_to: date
    sum_insured: Amount
    deductible_amount: Amount
    annual_aggregate_limit: Amount | None = None
    waiting_period_days: Annotated[int, Field(ge=0, le=3650)] = 0
    endorsements: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    clauses: list[ClauseIn] = Field(default_factory=list, max_length=500)
    coverages: list[CoverageIn] = Field(default_factory=list, max_length=100)
    exclusions: list[ExclusionIn] = Field(default_factory=list, max_length=200)


class PolicyCreate(Strict):
    policy_number: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9\-/]{3,60}$")]
    customer_id: uuid.UUID
    product_id: uuid.UUID
    status: Literal["active", "lapsed", "cancelled", "expired"] = "active"
    external_ref: Annotated[str, StringConstraints(max_length=80)] | None = None
    is_synthetic: bool = False
    initial_version: VersionIn | None = None
    publish_initial_version: bool = False


class PublishRequest(Strict):
    supersede_version_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)


class ClauseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    clause_ref: str
    section: str
    title: str
    text: str


class CoverageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    coverage_code: str
    perils: list[str]
    limit_amount: Decimal
    clause_ref: str
    deductible_override: Decimal | None
    co_pay_pct: Decimal | None


class ExclusionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    exclusion_code: str
    applies_to: list[str]
    description: str
    clause_ref: str


class VersionOut(BaseModel):
    id: uuid.UUID
    version_no: int
    status: str
    effective_from: date
    effective_to: date
    currency: str
    sum_insured: Decimal
    deductible_amount: Decimal
    annual_aggregate_limit: Decimal | None
    waiting_period_days: int
    endorsements: list[dict[str, Any]]
    published_at: datetime | None
    clauses: list[ClauseOut]
    coverages: list[CoverageOut]
    exclusions: list[ExclusionOut]


class PolicySummary(BaseModel):
    id: uuid.UUID
    policy_number: str
    status: str
    customer_id: uuid.UUID
    customer_name: str
    product_id: uuid.UUID
    product_name: str
    product_line: str
    currency: str
    is_synthetic: bool


class PolicyOut(PolicySummary):
    source: str
    external_ref: str | None
    versions: list[VersionOut]


# ---- Import -------------------------------------------------------------------------------


class ImportCustomer(Strict):
    external_ref: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    full_name: ShortText
    customer_type: Literal["individual", "business"] = "individual"
    date_of_birth: date | None = None
    email: Annotated[str, StringConstraints(max_length=254)] | None = None
    phone: Annotated[str, StringConstraints(max_length=40)] | None = None
    is_synthetic: bool = False


class ImportVersion(VersionIn):
    publish: bool = True


class PolicyImportItem(Strict):
    policy_number: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9\-/]{3,60}$")]
    product_code: Code
    status: Literal["active", "lapsed", "cancelled", "expired"] = "active"
    external_ref: Annotated[str, StringConstraints(max_length=80)] | None = None
    is_synthetic: bool = False
    customer: ImportCustomer
    versions: Annotated[list[ImportVersion], Field(min_length=1, max_length=20)]


class PolicyImportRequest(Strict):
    dry_run: bool = True
    policies: Annotated[list[PolicyImportItem], Field(min_length=1, max_length=500)]


class PolicyImportReport(BaseModel):
    dry_run: bool
    policies_created: int
    customers_created: int
    versions_created: int
    versions_published: int
    policy_numbers: list[str]
