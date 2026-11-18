"""Model registry administration, agent model assignment, probes, usage and gateway wiring."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import BusinessRuleError, DependencyUnavailableError, NotFoundError
from app.models.ai import AgentConfig, ModelInvocation, ModelRegistryEntry
from app.models.identity import Organization
from app.security.principal import Principal
from app.services import audit_service
from app.services.audit_service import AuditActor
from app.services.common import check_version
from model_gateway.catalog import CatalogModel, fetch_catalog
from model_gateway.gateway import AttemptRecord, InvocationRecorder, ModelGateway
from model_gateway.probes import CAPABILITIES, account_error, probe
from model_gateway.provider import MockProvider, OpenRouterProvider, Provider
from model_gateway.routing import ModelProfile, effective
from model_gateway.throttle import Throttle

# LLM-using agents (docs/architecture/03-agent-contracts.md §2) and the capabilities they need.
AGENT_DEFAULTS: dict[str, dict[str, Any]] = {
    "document": {"required": ["json_mode"], "description": "Classification and field extraction"},
    "policy": {"required": ["json_mode"], "description": "Peril mapping and exclusion interpretation"},
    "fraud": {"required": ["json_mode"], "description": "Narrative consistency review"},
    "assessment": {"required": ["json_mode"], "description": "Line-item normalisation"},
    "decision": {"required": ["json_mode"], "description": "Recommendation explanation"},
}


class AiNotConfiguredError(DependencyUnavailableError):
    title = "AI provider not configured"


class ProviderAccountError(DependencyUnavailableError):
    title = "The AI provider rejected the account"


def build_provider(settings: Settings) -> Provider:
    if settings.ai_provider == "mock":
        return MockProvider()
    if settings.openrouter_api_key is None:
        raise AiNotConfiguredError("Set OPENROUTER_API_KEY on the server to call models")
    return OpenRouterProvider(
        api_key=settings.openrouter_api_key.get_secret_value(),
        base_url=str(settings.openrouter_base_url),
        app_url=str(settings.app_base_url),
    )


def build_gateway(
    settings: Settings, throttle: Throttle | None, provider: Provider | None = None
) -> ModelGateway:
    return ModelGateway(
        provider or build_provider(settings),
        throttle,
        daily_token_budget=settings.ai_daily_token_budget,
        max_transport_attempts=settings.ai_max_transport_attempts,
    )


def profile(entry: ModelRegistryEntry) -> ModelProfile:
    verified = frozenset(
        k for k, v in (entry.verified_capabilities or {}).items() if isinstance(v, dict) and v.get("ok")
    )
    return ModelProfile(
        id=str(entry.id),
        slug=entry.slug,
        status=entry.status if entry.in_catalog else "disabled",
        data_policy=entry.data_policy,
        verified=verified,
        requests_per_minute=entry.requests_per_minute,
        prompt_price_per_mtok=float(entry.prompt_price_per_mtok),
        completion_price_per_mtok=float(entry.completion_price_per_mtok),
    )


def recorder(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    agent_key: str,
    purpose: str,
    data_class: str,
    request_id: str | None,
    agent_run_id: uuid.UUID | None = None,
) -> InvocationRecorder:
    async def record(attempt: AttemptRecord) -> None:
        db.add(
            ModelInvocation(
                organization_id=organization_id,
                agent_key=agent_key,
                purpose=purpose,
                model_slug=attempt.model_slug,
                attempt=attempt.attempt,
                status=attempt.status,
                error_code=attempt.error_code,
                latency_ms=attempt.latency_ms,
                prompt_tokens=attempt.prompt_tokens,
                completion_tokens=attempt.completion_tokens,
                cost_usd=attempt.cost_usd,
                request_sha256=attempt.request_sha256,
                data_classification=data_class,
                request_id=request_id,
                provider_request_id=attempt.provider_request_id,
                agent_run_id=agent_run_id,
            )
        )
        await db.flush()

    return record


# ---- Registry ------------------------------------------------------------------------------


async def get_model(db: AsyncSession, principal: Principal, model_id: uuid.UUID) -> ModelRegistryEntry:
    entry = await db.scalar(
        select(ModelRegistryEntry).where(
            ModelRegistryEntry.organization_id == principal.organization_id, ModelRegistryEntry.id == model_id
        )
    )
    if entry is None:
        raise NotFoundError("Model not found")
    return entry


async def list_models(db: AsyncSession, principal: Principal) -> list[ModelRegistryEntry]:
    rows = await db.scalars(
        select(ModelRegistryEntry)
        .where(ModelRegistryEntry.organization_id == principal.organization_id)
        .order_by(
            ModelRegistryEntry.in_catalog.desc(), ModelRegistryEntry.is_free.desc(), ModelRegistryEntry.slug
        )
    )
    return list(rows.all())


def _wanted(settings: Settings, catalog: list[CatalogModel]) -> list[CatalogModel]:
    configured = {
        s
        for s in (
            settings.openrouter_default_model,
            settings.openrouter_vision_model,
            settings.openrouter_reasoning_model,
            settings.openrouter_fast_model,
        )
        if s
    }
    return [m for m in catalog if m.is_free or m.slug in configured]


async def sync_catalog(
    db: AsyncSession, settings: Settings, principal: Principal, actor: AuditActor
) -> dict[str, int]:
    """Refresh the registry from OpenRouter: free models plus any explicitly configured ones."""
    try:
        catalog = await fetch_catalog(str(settings.openrouter_base_url))
    except Exception as exc:
        raise DependencyUnavailableError(
            f"Could not fetch the OpenRouter catalogue ({type(exc).__name__})"
        ) from exc
    wanted = {m.slug: m for m in _wanted(settings, catalog)}
    existing = {e.slug: e for e in await list_models(db, principal)}
    added = updated = removed = 0
    for slug, model in wanted.items():
        entry = existing.get(slug)
        values = {
            "display_name": model.name[:200],
            "is_free": model.is_free,
            "context_length": model.context_length,
            "max_completion_tokens": model.max_completion_tokens,
            "input_modalities": model.input_modalities,
            "declared_capabilities": model.declared,
            "prompt_price_per_mtok": model.prompt_price_per_mtok,
            "completion_price_per_mtok": model.completion_price_per_mtok,
            "in_catalog": True,
        }
        if entry is None:
            db.add(
                ModelRegistryEntry(
                    organization_id=principal.organization_id,
                    slug=slug,
                    data_policy="may_log" if model.is_free else "unknown",
                    **values,
                )
            )
            added += 1
        else:
            for key, value in values.items():
                setattr(entry, key, value)
            updated += 1
    all_slugs = {m.slug for m in catalog}
    for slug, entry in existing.items():
        if slug not in all_slugs and entry.in_catalog:
            entry.in_catalog = False  # withdrawn upstream: routing will skip it
            removed += 1
    await db.flush()
    await ensure_agent_configs(db, principal.organization_id, settings)
    summary = {"added": added, "updated": updated, "withdrawn": removed, "catalog_size": len(catalog)}
    await audit_service.record(
        db,
        actor=actor,
        action="ai.catalog_synced",
        entity_type="model_registry",
        entity_id=None,
        after=summary,
    )
    await db.commit()
    return summary


async def update_model(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    model_id: uuid.UUID,
    *,
    expected_version: int,
    changes: dict[str, Any],
) -> ModelRegistryEntry:
    entry = await get_model(db, principal, model_id)
    check_version(entry.row_version, expected_version, "model")
    if changes.get("data_policy") in ("no_training", "zero_retention") and not changes.get(
        "data_policy_note"
    ):
        raise BusinessRuleError("Approving a model for real data requires a note citing the provider's terms")
    before = {k: getattr(entry, k) for k in changes}
    for key, value in changes.items():
        setattr(entry, key, value)
    entry.row_version += 1
    await audit_service.record(
        db,
        actor=actor,
        action="ai.model_updated",
        entity_type="model_registry",
        entity_id=entry.id,
        before={k: str(v) for k, v in before.items()},
        after={k: str(v) for k, v in changes.items()},
    )
    await db.commit()
    return entry


# ---- Agent configuration -----------------------------------------------------------------


async def ensure_agent_configs(db: AsyncSession, organization_id: uuid.UUID, settings: Settings) -> None:
    have = set(
        (
            await db.scalars(
                select(AgentConfig.agent_key).where(AgentConfig.organization_id == organization_id)
            )
        ).all()
    )
    default_model = await db.scalar(
        select(ModelRegistryEntry.id).where(
            ModelRegistryEntry.organization_id == organization_id,
            ModelRegistryEntry.slug == settings.openrouter_default_model,
        )
    )
    for key, spec in AGENT_DEFAULTS.items():
        if key not in have:
            db.add(
                AgentConfig(
                    organization_id=organization_id,
                    agent_key=key,
                    primary_model_id=default_model,
                    required_capabilities=spec["required"],
                )
            )
    if default_model is not None:
        # Agents created before the first catalogue sync get the configured default model now.
        unassigned = await db.scalars(
            select(AgentConfig).where(
                AgentConfig.organization_id == organization_id, AgentConfig.primary_model_id.is_(None)
            )
        )
        for config in unassigned.all():
            config.primary_model_id = default_model
            config.row_version += 1
    await db.flush()


async def list_agents(db: AsyncSession, principal: Principal, settings: Settings) -> list[AgentConfig]:
    await ensure_agent_configs(db, principal.organization_id, settings)
    await db.commit()
    rows = await db.scalars(
        select(AgentConfig)
        .where(AgentConfig.organization_id == principal.organization_id)
        .order_by(AgentConfig.agent_key)
    )
    return list(rows.all())


async def update_agent(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    settings: Settings,
    agent_key: str,
    *,
    expected_version: int,
    changes: dict[str, Any],
) -> AgentConfig:
    await ensure_agent_configs(db, principal.organization_id, settings)
    config = await db.scalar(
        select(AgentConfig).where(
            AgentConfig.organization_id == principal.organization_id, AgentConfig.agent_key == agent_key
        )
    )
    if config is None:
        raise NotFoundError("Agent not found")
    check_version(config.row_version, expected_version, "agent configuration")
    required = set(changes.get("required_capabilities", config.required_capabilities))
    model_ids = [m for m in [changes.get("primary_model_id"), *changes.get("fallback_model_ids", [])] if m]
    for model_id in model_ids:
        entry = await get_model(db, principal, model_id)
        missing = required - set(effective(profile(entry).verified))
        if missing:
            raise BusinessRuleError(
                f"{entry.slug} has not passed the capability probe for: "
                f"{', '.join(sorted(missing))}. Run the capability test first."
            )
    # Phase 12 gate: a model the agent did not use before needs a passed regression run for this agent.
    from app.services import eval_service

    await eval_service.require_model_runs(db, principal.organization_id, config, model_ids)
    before = {k: str(getattr(config, k)) for k in changes}
    for key, value in changes.items():
        setattr(config, key, value)
    config.row_version += 1
    await audit_service.record(
        db,
        actor=actor,
        action="ai.agent_configured",
        entity_type="agent_config",
        entity_id=config.id,
        before=before,
        after={k: str(v) for k, v in changes.items()},
    )
    await db.commit()
    return config


async def model_chain(
    db: AsyncSession, organization_id: uuid.UUID, config: AgentConfig
) -> list[ModelProfile]:
    ids = [i for i in [config.primary_model_id, *config.fallback_model_ids] if i]
    entries = {
        e.id: e
        for e in (
            await db.scalars(
                select(ModelRegistryEntry).where(
                    ModelRegistryEntry.organization_id == organization_id, ModelRegistryEntry.id.in_(ids)
                )
            )
        ).all()
    }
    return [profile(entries[i]) for i in ids if i in entries]


async def org_allows_real_data(db: AsyncSession, organization_id: uuid.UUID) -> bool:
    return bool(
        await db.scalar(
            select(Organization.ai_data_processing_approved).where(Organization.id == organization_id)
        )
    )


# ---- Probes and usage ----------------------------------------------------------------------


async def run_probes(
    db: AsyncSession,
    settings: Settings,
    provider: Provider,
    throttle: Throttle | None,
    organization_id: uuid.UUID,
    model_id: uuid.UUID,
) -> ModelRegistryEntry:
    entry = await db.scalar(select(ModelRegistryEntry).where(ModelRegistryEntry.id == model_id))
    if entry is None:
        raise NotFoundError("Model not found")
    results: dict[str, Any] = {}
    record = recorder(
        db,
        organization_id,
        agent_key="capability_probe",
        purpose="capability_probe",
        data_class="synthetic",
        request_id=None,
    )
    for capability in CAPABILITIES:
        if capability == "vision" and "image" not in entry.input_modalities:
            results[capability] = {
                "ok": False,
                "latency_ms": 0,
                "detail": "model does not accept images",
                "tested_at": datetime.now(UTC).isoformat(),
            }
            continue
        if throttle is not None:
            await throttle.acquire(entry.slug, entry.requests_per_minute, max_wait=90)
        result = await probe(
            provider, entry.slug, capability, request_timeout=settings.ai_request_timeout_seconds
        )
        if account_error(result):
            # Not the model's fault: keep earlier verification results and report the account problem.
            previously_ok = any(
                isinstance(v, dict) and v.get("ok") for v in (entry.verified_capabilities or {}).values()
            )
            entry.probe_status = "passed" if previously_ok else ("failed" if entry.probed_at else "never")
            await db.commit()
            raise ProviderAccountError(
                f"OpenRouter rejected the request ({result.detail}). "
                "Check OPENROUTER_API_KEY and the account's credits."
            )
        results[capability] = result.as_dict()
        await record(
            AttemptRecord(
                entry.slug,
                1,
                "ok" if result.ok else "probe_failed",
                None if result.ok else "probe_failed",
                result.latency_ms,
                0,
                0,
                0.0,
                "probe:" + capability,
                None,
            )
        )
    passed = [c for c, r in results.items() if r["ok"]]
    entry.verified_capabilities = results
    entry.probe_status = "passed" if len(passed) == len(results) else ("partial" if passed else "failed")
    entry.probed_at = datetime.now(UTC)
    await audit_service.record(
        db,
        actor=AuditActor("system", None),
        action="ai.model_probed",
        entity_type="model_registry",
        entity_id=entry.id,
        after={"slug": entry.slug, "passed": passed, "status": entry.probe_status},
    )
    await db.commit()
    return entry


async def usage(db: AsyncSession, principal: Principal, days: int) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await db.execute(
            select(
                ModelInvocation.agent_key,
                ModelInvocation.model_slug,
                func.count(),
                func.count().filter(ModelInvocation.status == "ok"),
                func.coalesce(func.sum(ModelInvocation.prompt_tokens), 0),
                func.coalesce(func.sum(ModelInvocation.completion_tokens), 0),
                func.coalesce(func.sum(ModelInvocation.cost_usd), 0),
                func.percentile_cont(0.5).within_group(ModelInvocation.latency_ms),
            )
            .where(
                ModelInvocation.organization_id == principal.organization_id,
                ModelInvocation.created_at >= since,
            )
            .group_by(ModelInvocation.agent_key, ModelInvocation.model_slug)
            .order_by(func.count().desc())
        )
    ).all()
    return [
        {
            "agent_key": r[0],
            "model_slug": r[1],
            "calls": r[2],
            "succeeded": r[3],
            "prompt_tokens": int(r[4]),
            "completion_tokens": int(r[5]),
            "cost_usd": str(r[6]),
            "p50_latency_ms": int(r[7] or 0),
        }
        for r in rows
    ]
