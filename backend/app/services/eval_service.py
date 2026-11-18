"""Evaluation pipeline and activation gate (Phase 12).

An evaluation run tests one candidate for one agent: a prompt version (``kind=prompt``) or a model
(``kind=model``). It runs, in the ``system`` queue:

1. the agent's labelled set deterministically (rules and code must still pass), when the agent has one;
2. a live part through the real model gateway in which **only the candidate agent** uses the candidate
   prompt or model: the injection set (every agent in the chain, adversarial documents), plus the agent's
   own set in live mode where that set supports it (document, policy, assessment).

The run passes only if every gated metric passes, the candidate agent actually got answers from the model
(a run in which the model was never reached proves nothing and ends ``error``), and no gated metric is worse
than in the latest passed run for the same agent. Activating a prompt version, or assigning a model to an
agent that it was not assigned before, requires a passed run for exactly that prompt (file hash) or model
from the last ``VALID_DAYS`` days. Synthetic data only.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.base import PROMPT_VERSIONS, load_prompt, prompt_versions
from agents.registry import LLM_AGENTS
from app.core.config import Settings
from app.core.db import Database
from app.core.errors import BusinessRuleError, NotFoundError
from app.core.tenancy import bind_tenant
from app.models.ai import AgentConfig, ModelRegistryEntry
from app.models.intelligence import EvalRun
from app.security.principal import Principal
from app.services import audit_service
from app.services.audit_service import AuditActor

VALID_DAYS = 30
LIVE_LIMIT = 3  # free models allow few requests a day
LIVE_SETS = ("document", "policy", "assessment")


def _now() -> datetime:
    return datetime.now(UTC)


async def _config(db: AsyncSession, org: uuid.UUID, agent_key: str) -> AgentConfig:
    config = await db.scalar(
        select(AgentConfig).where(AgentConfig.organization_id == org, AgentConfig.agent_key == agent_key)
    )
    if config is None:
        raise NotFoundError("Agent not configured")
    return config


async def request(
    db: AsyncSession,
    principal: Principal,
    actor: AuditActor,
    *,
    agent_key: str,
    prompt_version: str | None,
    model_id: uuid.UUID | None,
) -> EvalRun:
    if agent_key not in LLM_AGENTS:
        raise BusinessRuleError(f"Only agents that use a model can be evaluated: {sorted(LLM_AGENTS)}")
    config = await _config(db, principal.organization_id, agent_key)
    version = prompt_version or config.prompt_version
    if version not in prompt_versions(agent_key):
        raise BusinessRuleError(f"{agent_key} has no prompt version {version}")
    model: ModelRegistryEntry | None = None
    if model_id is not None:
        model = await db.get(ModelRegistryEntry, model_id)
        if model is None or model.organization_id != principal.organization_id:
            raise NotFoundError("Model not found")
    elif config.primary_model_id is None:
        raise BusinessRuleError("Choose a model: this agent has none assigned")
    run = EvalRun(
        id=uuid.uuid4(),
        organization_id=principal.organization_id,
        agent_key=agent_key,
        kind="model" if model_id is not None else "prompt",
        prompt_version=version,
        prompt_sha256=load_prompt(agent_key, version).sha256,
        model_id=model.id if model else config.primary_model_id,
        model_slug=model.slug if model else None,
        status="queued",
        requested_by=principal.user_id,
        created_at=_now(),
    )
    db.add(run)
    await db.flush()
    await audit_service.record(
        db,
        actor=actor,
        action="eval.requested",
        entity_type="agent_config",
        entity_id=config.id,
        after={
            "eval_run_id": str(run.id),
            "kind": run.kind,
            "prompt": version,
            "model_id": str(run.model_id),
        },
    )
    return run


def enqueue(run: EvalRun) -> None:
    from tasks.celery_app import celery_app

    celery_app.send_task("system.eval_run", args=[str(run.organization_id), str(run.id)], queue="system")


# --- execution -----------------------------------------------------------------------------------

LlmFactory = Callable[[AsyncSession, AgentConfig], Awaitable[Any]]


async def _gateway_llm(s: AsyncSession, settings: Settings, org: uuid.UUID, config: AgentConfig) -> Any:
    from redis.asyncio import Redis

    from app.services import ai_service
    from app.services.agent_service import GatewayLlm
    from model_gateway.throttle import Throttle

    redis = Redis.from_url(settings.redis_url.get_secret_value())
    allows = await ai_service.org_allows_real_data(s, org)
    return GatewayLlm(
        s,
        settings,
        Throttle(redis),
        org,
        config,
        data_class="synthetic",
        allows_real=allows,
        request_id="eval",
        run_id=None,
    ), redis


def _metrics(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**m, "part": f"{p['agent']}/{p['mode']}"} for p in parts for m in p["metrics"]]


async def execute(db: Database, settings: Settings, organization_id: uuid.UUID, run_id: uuid.UUID) -> str:
    from evaluations import assessment_set, document_set, injection_set, policy_set
    from evaluations import run as eval_sets

    async with db.sessionmaker() as s:
        await bind_tenant(s, organization_id)
        run = await s.scalar(select(EvalRun).where(EvalRun.id == run_id).with_for_update())
        if run is None or run.status != "queued":
            return run.status if run else "missing"
        run.status = "running"
        await s.commit()
        config = await _config(s, organization_id, run.agent_key)
        candidate = AgentConfig(
            **{
                c: getattr(config, c)
                for c in (
                    "id",
                    "organization_id",
                    "agent_key",
                    "enabled",
                    "required_capabilities",
                    "confidence_threshold",
                    "max_retries",
                    "timeout_seconds",
                    "max_output_tokens",
                    "temperature",
                )
            }
        )
        candidate.primary_model_id, candidate.fallback_model_ids = run.model_id, []  # the candidate only
        token = PROMPT_VERSIONS.set({run.agent_key: run.prompt_version})
        parts: list[dict[str, Any]] = []
        redis = None
        try:
            if run.agent_key in eval_sets.SETS:
                parts.append((await eval_sets.SETS[run.agent_key]()).as_dict())
            llm, redis = await _gateway_llm(s, settings, organization_id, candidate)

            def factory(key: str) -> Any:
                return llm if key == run.agent_key else None

            parts.append((await injection_set.evaluate(factory, limit=LIVE_LIMIT)).as_dict())
            if run.agent_key in LIVE_SETS:
                if run.agent_key == "document":
                    live = await document_set.evaluate(factory, limit=LIVE_LIMIT)
                elif run.agent_key == "policy":
                    live = await policy_set.evaluate(factory)
                else:
                    live = await assessment_set.evaluate(factory)
                parts.append(live.as_dict())
            answered = sum(1 for c in llm.calls if c.model)
        except Exception as exc:  # recorded on the run
            run.status, run.error, run.finished_at = "error", f"{type(exc).__name__}: {exc}"[:500], _now()
            run.parts = parts
            await s.commit()
            return run.status
        finally:
            PROMPT_VERSIONS.reset(token)
            if redis is not None:
                await redis.aclose()
        run.parts, run.metrics = parts, _metrics(parts)
        run.cases, run.model_calls = sum(p["cases"] for p in parts), answered
        run.regressions = await _regressions(s, run)
        failed = [m for m in run.metrics if not m["passed"]]
        if answered == 0:
            run.status = "error"
            run.error = "the candidate model never answered (unavailable, rate-limited or quota exhausted)"
        elif failed or run.regressions:
            run.status = "failed"
            run.error = "; ".join(
                [f"{m['part']} {m['name']} {m['value']}" for m in failed] + run.regressions
            )[:500]
        else:
            run.status = "passed"
        run.finished_at = _now()
        await s.commit()
        return run.status


async def _regressions(s: AsyncSession, run: EvalRun) -> list[str]:
    baseline = await s.scalar(
        select(EvalRun)
        .where(EvalRun.agent_key == run.agent_key, EvalRun.status == "passed", EvalRun.id != run.id)
        .order_by(EvalRun.finished_at.desc())
        .limit(1)
    )
    if baseline is None:
        return []
    before = {(m["part"], m["name"]): m for m in baseline.metrics if m.get("threshold") is not None}
    out = []
    for m in run.metrics:
        old = before.get((m["part"], m["name"]))
        if old is None:
            continue
        worse = m["value"] < old["value"] if m["higher_is_better"] else m["value"] > old["value"]
        if worse:
            out.append(f"{m['part']} {m['name']}: {m['value']} vs baseline {old['value']}")
    return out


# --- the gate ------------------------------------------------------------------------------------


async def passed_run(
    db: AsyncSession,
    org: uuid.UUID,
    agent_key: str,
    *,
    prompt_sha256: str | None = None,
    model_id: uuid.UUID | None = None,
) -> EvalRun | None:
    stmt = select(EvalRun).where(
        EvalRun.organization_id == org,
        EvalRun.agent_key == agent_key,
        EvalRun.status == "passed",
        EvalRun.finished_at >= _now() - timedelta(days=VALID_DAYS),
    )
    if prompt_sha256 is not None:
        stmt = stmt.where(EvalRun.prompt_sha256 == prompt_sha256)
    if model_id is not None:
        stmt = stmt.where(EvalRun.model_id == model_id)
    run: EvalRun | None = await db.scalar(stmt.order_by(EvalRun.finished_at.desc()).limit(1))
    return run


async def require_model_runs(
    db: AsyncSession, org: uuid.UUID, config: AgentConfig, new_ids: list[uuid.UUID]
) -> None:
    current = {config.primary_model_id, *config.fallback_model_ids}
    for model_id in new_ids:
        if model_id in current:
            continue  # already assigned (re-ordering is not a new activation)
        if await passed_run(db, org, config.agent_key, model_id=model_id) is None:
            entry = await db.get(ModelRegistryEntry, model_id)
            raise BusinessRuleError(
                f"{entry.slug if entry else model_id} has no passed regression run for the "
                f"{config.agent_key} agent "
                f"in the last {VALID_DAYS} days. Run an evaluation first (AI Agents → Evaluations)."
            )


async def activate_prompt(
    db: AsyncSession, principal: Principal, actor: AuditActor, agent_key: str, version: str
) -> AgentConfig:
    if version not in prompt_versions(agent_key):
        raise NotFoundError("Prompt version not found")
    config = await _config(db, principal.organization_id, agent_key)
    sha = load_prompt(agent_key, version).sha256
    if version != config.prompt_version:
        run = await passed_run(db, principal.organization_id, agent_key, prompt_sha256=sha)
        if run is None:
            raise BusinessRuleError(
                f"{agent_key} prompt {version} has no passed regression run in the last {VALID_DAYS} days; "
                "it cannot be activated."
            )
    before = config.prompt_version
    config.prompt_version = version
    config.row_version += 1
    await audit_service.record(
        db,
        actor=actor,
        action="prompt.activated",
        entity_type="agent_config",
        entity_id=config.id,
        before={"prompt_version": before},
        after={"prompt_version": version, "sha256": sha},
    )
    return config


async def list_runs(
    db: AsyncSession, principal: Principal, agent_key: str | None, limit: int = 50
) -> list[EvalRun]:
    stmt = select(EvalRun).where(EvalRun.organization_id == principal.organization_id)
    if agent_key:
        stmt = stmt.where(EvalRun.agent_key == agent_key)
    return list((await db.scalars(stmt.order_by(EvalRun.created_at.desc()).limit(min(limit, 200)))).all())
