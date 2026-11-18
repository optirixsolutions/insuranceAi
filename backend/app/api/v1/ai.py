"""AI administration: model registry, capability probes, agent model assignment, usage, test runs."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from redis.asyncio import Redis

from app.api.deps import ActorDep, DbDep, SettingsDep, get_redis, parse_if_match, require
from app.core.errors import NotFoundError
from app.core.request_context import get_request_id
from app.models.ai import AgentConfig, ModelRegistryEntry
from app.security.permissions import P
from app.security.principal import Principal
from app.services import ai_service
from model_gateway.provider import OpenRouterProvider, ProviderError

router = APIRouter(tags=["ai"])
Admin = Annotated[Principal, Depends(require(P.AI_MANAGE))]
RedisDep = Annotated[Redis, Depends(get_redis)]
IfMatch = Annotated[str | None, Header()]


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    slug: str
    display_name: str
    is_free: bool
    context_length: int | None
    input_modalities: list[str]
    declared_capabilities: dict[str, bool]
    verified_capabilities: dict[str, Any]
    probe_status: str
    probed_at: datetime | None
    prompt_price_per_mtok: Decimal
    completion_price_per_mtok: Decimal
    data_policy: str
    data_policy_note: str | None
    status: str
    in_catalog: bool
    requests_per_minute: int
    row_version: int


class ModelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "disabled"] | None = None
    data_policy: Literal["unknown", "may_log", "no_training", "zero_retention"] | None = None
    data_policy_note: Annotated[str, StringConstraints(max_length=500)] | None = None
    requests_per_minute: Annotated[int, Field(ge=1, le=10000)] | None = None


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    agent_key: str
    description: str = ""
    enabled: bool
    primary_model_id: uuid.UUID | None
    fallback_model_ids: list[uuid.UUID]
    required_capabilities: list[str]
    confidence_threshold: float
    max_retries: int
    timeout_seconds: int
    max_output_tokens: int
    temperature: float
    row_version: int


class AgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    primary_model_id: uuid.UUID | None = None
    fallback_model_ids: Annotated[list[uuid.UUID], Field(max_length=5)] | None = None
    required_capabilities: list[Literal["json_mode", "json_schema", "tools", "vision"]] | None = None
    confidence_threshold: Annotated[float, Field(ge=0.5, le=1)] | None = None
    max_retries: Annotated[int, Field(ge=0, le=5)] | None = None
    timeout_seconds: Annotated[int, Field(ge=5, le=600)] | None = None
    max_output_tokens: Annotated[int, Field(ge=64, le=32000)] | None = None
    temperature: Annotated[float, Field(ge=0, le=2)] | None = None


class TestRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_key: Annotated[str, StringConstraints(pattern=r"^[a-z_]{2,40}$")]
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=20, max_length=4000)]
    confirm_synthetic: Literal[True] = Field(
        description="The text is synthetic and contains no real personal data"
    )


class Job(BaseModel):
    job_id: str
    status: str
    result: dict[str, Any] | None = None


class AiStatus(BaseModel):
    provider: str
    api_key_configured: bool
    key_status: str = "not_configured"  # valid | rejected | unreachable | not_configured | not_applicable
    key_detail: str | None = None
    default_model: str
    models_registered: int
    models_verified: int


def _agent_out(config: AgentConfig) -> AgentOut:
    out = AgentOut.model_validate(config)
    out.description = ai_service.AGENT_DEFAULTS.get(config.agent_key, {}).get("description", "")
    return out


async def _key_check(settings: Any, redis: Redis) -> tuple[str, str | None]:
    """Ask OpenRouter whether the key is valid (cached 2 minutes; consumes no tokens)."""
    if settings.ai_provider == "mock":
        return "not_applicable", "mock provider"
    if settings.openrouter_api_key is None:
        return "not_configured", None
    cached = await redis.get("claimos:ai:key_status")
    if cached:
        cached_state, _, cached_detail = (cached.decode() if isinstance(cached, bytes) else cached).partition(
            "|"
        )
        return cached_state, cached_detail or None
    provider = ai_service.build_provider(settings)
    state: str = "valid"
    detail: str | None = None
    try:
        info = await provider.key_info() if isinstance(provider, OpenRouterProvider) else {}
        if info:
            detail = f"free tier: {info.get('is_free_tier')}, limit remaining: {info.get('limit_remaining')}"
    except ProviderError as exc:
        state = "unreachable" if exc.retryable else "rejected"
        detail = f"{exc.code}: {exc}"
    await redis.set("claimos:ai:key_status", f"{state}|{detail or ''}", ex=120)
    return state, detail


@router.get("/ai/status", response_model=AiStatus)
async def ai_status(principal: Admin, db: DbDep, settings: SettingsDep, redis: RedisDep) -> AiStatus:
    models = await ai_service.list_models(db, principal)
    key_status, key_detail = await _key_check(settings, redis)
    return AiStatus(
        provider=settings.ai_provider,
        api_key_configured=settings.openrouter_api_key is not None,
        key_status=key_status,
        key_detail=key_detail,
        default_model=settings.openrouter_default_model,
        models_registered=len(models),
        models_verified=sum(1 for m in models if m.probe_status in ("passed", "partial")),
    )


@router.get("/models", response_model=list[ModelOut])
async def list_models(principal: Admin, db: DbDep) -> list[ModelOut]:
    return [ModelOut.model_validate(m) for m in await ai_service.list_models(db, principal)]


@router.post("/models/sync", response_model=dict[str, int])
async def sync_models(principal: Admin, actor: ActorDep, db: DbDep, settings: SettingsDep) -> dict[str, int]:
    """Refresh the registry from the OpenRouter catalogue (free models plus configured ones)."""
    return await ai_service.sync_catalog(db, settings, principal, actor)


@router.patch("/models/{model_id}", response_model=ModelOut)
async def update_model(
    model_id: uuid.UUID,
    body: ModelUpdate,
    principal: Admin,
    actor: ActorDep,
    db: DbDep,
    if_match: IfMatch = None,
) -> ModelOut:
    entry: ModelRegistryEntry = await ai_service.update_model(
        db,
        principal,
        actor,
        model_id,
        expected_version=parse_if_match(if_match),
        changes=body.model_dump(exclude_unset=True),
    )
    return ModelOut.model_validate(entry)


async def _remember_job(redis: Redis, job_id: str, principal: Principal) -> None:
    await redis.set(f"claimos:ai:job:{job_id}", str(principal.organization_id), ex=3600)


@router.post("/models/{model_id}/capability-test", response_model=Job, status_code=status.HTTP_202_ACCEPTED)
async def capability_test(
    model_id: uuid.UUID, principal: Admin, db: DbDep, settings: SettingsDep, redis: RedisDep
) -> Job:
    """Send synthetic probe requests to verify JSON mode, JSON schema, tools and vision."""
    from tasks.celery_app import celery_app

    ai_service.build_provider(settings)  # fail fast (503) if no key is configured
    entry = await ai_service.get_model(db, principal, model_id)
    entry.probe_status = "running"
    await db.commit()
    job = celery_app.send_task(
        "ai.probe_model", args=[str(principal.organization_id), str(model_id)], queue="system"
    )
    await _remember_job(redis, job.id, principal)
    return Job(job_id=job.id, status="queued")


@router.get("/agents", response_model=list[AgentOut])
async def list_agents(principal: Admin, db: DbDep, settings: SettingsDep) -> list[AgentOut]:
    return [_agent_out(c) for c in await ai_service.list_agents(db, principal, settings)]


@router.patch("/agents/{agent_key}", response_model=AgentOut)
async def update_agent(
    agent_key: str,
    body: AgentUpdate,
    principal: Admin,
    actor: ActorDep,
    db: DbDep,
    settings: SettingsDep,
    if_match: IfMatch = None,
) -> AgentOut:
    config = await ai_service.update_agent(
        db,
        principal,
        actor,
        settings,
        agent_key,
        expected_version=parse_if_match(if_match),
        changes=body.model_dump(exclude_unset=True),
    )
    return _agent_out(config)


@router.get("/ai/usage", response_model=list[dict[str, Any]])
async def ai_usage(
    principal: Admin, db: DbDep, days: Annotated[int, Query(ge=1, le=90)] = 7
) -> list[dict[str, Any]]:
    return await ai_service.usage(db, principal, days)


@router.post("/ai/test-runs", response_model=Job, status_code=status.HTTP_202_ACCEPTED)
async def start_test_run(
    body: TestRunRequest, principal: Admin, settings: SettingsDep, redis: RedisDep
) -> Job:
    """Run synthetic text through the full gateway for an agent (routing, retries, repair, fallback)."""
    from tasks.celery_app import celery_app

    ai_service.build_provider(settings)
    job = celery_app.send_task(
        "ai.test_run",
        args=[str(principal.organization_id), body.agent_key, body.text, get_request_id()],
        queue="system",
    )
    await _remember_job(redis, job.id, principal)
    return Job(job_id=job.id, status="queued")


@router.get("/ai/jobs/{job_id}", response_model=Job)
async def get_job(job_id: str, principal: Admin, redis: RedisDep) -> Job:
    owner = await redis.get(f"claimos:ai:job:{job_id}")
    owner_text = owner.decode() if isinstance(owner, bytes) else owner
    if owner_text != str(principal.organization_id):
        raise NotFoundError("Job not found")
    from celery.result import AsyncResult

    from tasks.celery_app import celery_app

    result = AsyncResult(job_id, app=celery_app)
    if result.state == "SUCCESS":
        return Job(job_id=job_id, status="done", result=result.result)
    if result.state == "FAILURE":
        return Job(job_id=job_id, status="failed", result={"error": type(result.result).__name__})
    return Job(job_id=job_id, status=result.state.lower())
