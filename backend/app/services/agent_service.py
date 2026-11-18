"""Agent runs: start, execute (worker), persist, read. Bridges agents to the Phase 5 gateway.

A run row is created ``running`` by the API (one running run per claim and agent), executed by
the ``workflow`` worker, and finalised exactly once (the database refuses later changes). The
validated output is stored separately and is append-only. Every model call made by the run is
recorded in ``model_invocations`` with the run id.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agents.base import (
    PROMPT_VERSIONS,
    LlmCall,
    LlmUnavailableError,
    load_prompt,
    prompt_versions,
    rule_label,
)
from agents.contracts import OUTPUT_MODELS
from agents.registry import AGENTS, LLM_AGENTS, ORDER, get
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError
from app.models.agents import AgentOutput, AgentRun, PromptVersion
from app.models.ai import AgentConfig
from app.models.claims import Claim
from app.security.principal import Principal
from app.services import agent_snapshot, ai_service, audit_service
from app.services.audit_service import AuditActor
from app.services.claim_access import get_visible_claim
from model_gateway.gateway import GatewayExhaustedError, NoEligibleModelError, StructuredCall
from model_gateway.throttle import BudgetExceededError, Throttle
from rules_engine.calculation import RULE_SET_VERSION

RUN_DEADLINE_SECONDS = 900
DEFAULT_THRESHOLD = 0.85


class PromptDriftError(RuntimeError):
    """A prompt file changed without a new version number."""


async def ensure_prompts(db: AsyncSession, selected: dict[str, str] | None = None) -> dict[str, uuid.UUID]:
    """Mirror every prompt file into ``prompt_versions`` (v1 is the baseline, later versions start as
    drafts until an organisation activates them after a passing regression run). Refuse to run if a
    file changed without a new version number. Returns the row id of the selected version per agent."""
    ids: dict[str, uuid.UUID] = {}
    for key in LLM_AGENTS:
        for version in prompt_versions(key):
            prompt = load_prompt(key, version)
            await db.execute(
                insert(PromptVersion)
                .values(
                    id=uuid.uuid4(),
                    agent_key=key,
                    version=prompt.version,
                    template_path=prompt.path,
                    content_sha256=prompt.sha256,
                    content=prompt.content,
                    status="active" if version == "v1" else "draft",
                )
                .on_conflict_do_nothing(constraint="uq_prompt_versions_agent_version")
            )
            row = (
                await db.scalars(
                    select(PromptVersion).where(
                        PromptVersion.agent_key == key, PromptVersion.version == prompt.version
                    )
                )
            ).one()
            if row.content_sha256 != prompt.sha256:
                raise PromptDriftError(
                    f"{prompt.path} changed without a version bump (stored {row.content_sha256[:12]})"
                )
            if version == (selected or {}).get(key, "v1"):
                ids[key] = row.id
    return ids


class GatewayLlm:
    """``LlmClient`` over the model gateway: verified models only, data-policy gate, recording."""

    def __init__(
        self,
        db: AsyncSession,
        settings: Settings,
        throttle: Throttle,
        org_id: uuid.UUID,
        config: AgentConfig,
        *,
        data_class: str,
        allows_real: bool,
        request_id: str | None,
        run_id: uuid.UUID | None,
    ) -> None:
        self.db, self.settings, self.throttle, self.org_id, self.config = (
            db,
            settings,
            throttle,
            org_id,
            config,
        )
        self.data_class, self.allows_real = data_class, allows_real
        self.gateway = ai_service.build_gateway(settings, throttle)
        self.record = ai_service.recorder(
            db,
            org_id,
            agent_key=config.agent_key,
            purpose="agent_run",
            data_class=data_class,
            request_id=request_id,
            agent_run_id=run_id,
        )
        self.calls: list[LlmCall] = []
        self.notes: list[str] = []
        self.cost = Decimal("0")

    async def structured[M: BaseModel](
        self, schema: type[M], *, system: str, user: str, validate: Any = None
    ) -> M:
        chain = await ai_service.model_chain(self.db, self.org_id, self.config)
        call = StructuredCall(
            organization_id=self.org_id,
            agent_key=self.config.agent_key,
            purpose="agent_run",
            system_prompt=system,
            user_content=user,
            data_class=self.data_class,  # type: ignore[arg-type]
            required_capabilities=frozenset(self.config.required_capabilities),
            org_allows_real_data=self.allows_real,
            max_output_tokens=self.config.max_output_tokens,
            temperature=self.config.temperature,
            timeout_seconds=float(self.config.timeout_seconds),
            max_repairs=self.config.max_retries,
            validate=validate,
        )
        try:
            outcome = await self.gateway.run_structured(call, schema, chain, self.record)
        except GatewayExhaustedError as exc:
            self._account(None, exc.attempts)
            self.notes.append(
                "all models failed: "
                + ", ".join(f"{a.model_slug}:{a.error_code}" for a in exc.attempts)[:300]
            )
            raise LlmUnavailableError(str(exc)) from exc
        except (NoEligibleModelError, BudgetExceededError) as exc:
            self.notes.append(str(exc)[:300])
            raise LlmUnavailableError(str(exc)) from exc
        self._account(outcome.model_slug, outcome.attempts)
        return outcome.data

    def _account(self, model: str | None, attempts: list[Any]) -> None:
        self.calls.append(
            LlmCall(
                model,
                [a.status for a in attempts],
                sum(a.prompt_tokens for a in attempts),
                sum(a.completion_tokens for a in attempts),
            )
        )
        self.cost += sum((Decimal(str(a.cost_usd)) for a in attempts), Decimal("0"))


async def _config(db: AsyncSession, org_id: uuid.UUID, settings: Settings, key: str) -> AgentConfig | None:
    if key not in LLM_AGENTS:
        return None
    await ai_service.ensure_agent_configs(db, org_id, settings)
    return await db.scalar(
        select(AgentConfig).where(AgentConfig.organization_id == org_id, AgentConfig.agent_key == key)
    )


async def start_run(
    db: AsyncSession,
    principal: Principal,
    claim_id: uuid.UUID,
    agent_key: str,
    actor: AuditActor,
    request_id: str | None,
) -> AgentRun:
    if agent_key not in AGENTS:
        raise NotFoundError(f"Unknown agent '{agent_key}'")
    claim = await get_visible_claim(db, principal, claim_id)
    return await create_run(
        db, claim.organization_id, claim.id, agent_key, actor, request_id, principal.user_id
    )


async def create_run(
    db: AsyncSession,
    org_id: uuid.UUID,
    claim_id: uuid.UUID,
    agent_key: str,
    actor: AuditActor,
    request_id: str | None,
    user_id: uuid.UUID | None,
    *,
    execution_id: uuid.UUID | None = None,
    node: str | None = None,
) -> AgentRun:
    """Create a ``running`` row. Callers must already have checked access to the claim.

    Within a workflow execution, a run of the same agent left ``running`` by that execution means
    the worker died mid-node; it is finalised as ``failed`` (WORKER_LOST) before the replay starts.
    """
    claim = await db.get(Claim, claim_id)
    if claim is None or claim.organization_id != org_id:
        raise NotFoundError("Claim not found")
    if execution_id is not None:
        orphan = await db.scalar(
            select(AgentRun).where(
                AgentRun.claim_id == claim.id,
                AgentRun.agent_key == agent_key,
                AgentRun.status == "running",
                AgentRun.execution_id == execution_id,
            )
        )
        if orphan is not None:
            orphan.status, orphan.error_code = "failed", "WORKER_LOST"
            orphan.error_detail = "The worker stopped during this run; the workflow replayed the node"
            orphan.finished_at = datetime.now(UTC)
            await db.flush()
    attempt = (
        await db.scalar(
            select(func.max(AgentRun.attempt)).where(
                AgentRun.claim_id == claim.id, AgentRun.agent_key == agent_key
            )
        )
    ) or 0
    run = AgentRun(
        id=uuid.uuid4(),
        organization_id=claim.organization_id,
        claim_id=claim.id,
        agent_key=agent_key,
        node=node or agent_key,
        execution_id=execution_id,
        attempt=attempt + 1,
        status="running",
        started_at=datetime.now(UTC),
        rule_set_version=RULE_SET_VERSION,
        input_hash="pending",
        trace_id=request_id,
        triggered_by=user_id,
        findings=[],
        missing=[],
    )
    db.add(run)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise ConflictError(f"The {agent_key} agent is already running on this claim") from exc
    await audit_service.record(
        db,
        actor=actor,
        action="agent.run.started",
        entity_type="claim",
        entity_id=claim.id,
        after={"agent_run_id": str(run.id), "agent_key": agent_key, "attempt": run.attempt},
    )
    return run


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


async def execute_run(
    db: AsyncSession, settings: Settings, throttle: Throttle, org_id: uuid.UUID, run_id: uuid.UUID
) -> AgentRun:
    run = await db.get(AgentRun, run_id)
    if run is None:
        raise LookupError(f"agent run {run_id} not found")
    if run.status != "running":
        return run  # already finalised (task redelivery after a crash)
    started = time.monotonic()
    agent = get(run.agent_key)
    llm: GatewayLlm | None = None
    try:
        config = await _config(db, org_id, settings, run.agent_key)
        version = config.prompt_version if config else "v1"
        prompts = await ensure_prompts(db, {run.agent_key: version})
        PROMPT_VERSIONS.set({run.agent_key: version})
        threshold = float(config.confidence_threshold) if config else DEFAULT_THRESHOLD
        snapshot = await agent_snapshot.load(db, org_id, run.claim_id, run.agent_key, threshold)
        run.input_hash = _hash(snapshot.model_dump(mode="json"))
        run.rule_set_version = rule_label(snapshot)
        run.prompt_version_id = prompts.get(run.agent_key)
        has_models = bool(config and await ai_service.model_chain(db, org_id, config))
        if agent.uses_llm and config is not None and config.enabled and has_models:
            try:
                llm = GatewayLlm(
                    db,
                    settings,
                    throttle,
                    org_id,
                    config,
                    data_class="synthetic" if snapshot.claim.is_synthetic else "real",
                    allows_real=snapshot.org_allows_real_data,
                    request_id=run.trace_id,
                    run_id=run.id,
                )
            except ai_service.AiNotConfiguredError:
                llm = None
        result = await asyncio.wait_for(agent.run(agent.context(snapshot, llm)), RUN_DEADLINE_SECONDS)
        model_cls = OUTPUT_MODELS[run.agent_key]
        errors: list[Any] = []
        try:
            data = (
                model_cls.model_validate(result.data).model_dump(mode="json")
                if result.data is not None
                else None
            )
        except ValidationError as exc:
            data, errors = None, json.loads(exc.json())
        status = result.status if not errors else "failed"
        if data is not None:
            db.add(
                AgentOutput(
                    id=uuid.uuid4(),
                    organization_id=org_id,
                    agent_run_id=run.id,
                    schema_name=model_cls.__name__,
                    schema_version=agent.schema_version,
                    output=data,
                    validation_errors=errors,
                    output_hash=_hash(data),
                )
            )
        run.status = status
        run.used_llm, run.escalate = result.used_llm, result.escalate
        notes = [n for n in [result.llm_note, *(llm.notes if llm else [])] if n]
        if agent.uses_llm and llm is None:
            notes.insert(0, "No verified model assigned to this agent; deterministic core only")
        run.llm_note = "; ".join(notes)[:500] or None
        run.findings = [f.model_dump(mode="json") for f in result.findings]
        run.missing = [m.model_dump(mode="json") for m in result.missing]
        if errors:
            run.error_code, run.error_detail = "OUTPUT_SCHEMA_INVALID", json.dumps(errors)[:1000]
    except TimeoutError:
        run.status, run.error_code, run.error_detail = (
            "timed_out",
            "DEADLINE",
            f"exceeded {RUN_DEADLINE_SECONDS}s",
        )
    except Exception as exc:  # the run must always be finalised, with the reason
        run.status, run.error_code, run.error_detail = "failed", type(exc).__name__, str(exc)[:1000]
    if llm is not None:
        run.model_slug = next((c.model for c in reversed(llm.calls) if c.model), None)
        run.prompt_tokens = sum(c.prompt_tokens for c in llm.calls)
        run.completion_tokens = sum(c.completion_tokens for c in llm.calls)
        run.cost_usd = llm.cost
    run.finished_at = datetime.now(UTC)
    run.duration_ms = int((time.monotonic() - started) * 1000)
    await audit_service.record(
        db,
        actor=AuditActor("agent", None, run.trace_id),
        action=f"agent.run.{run.status}",
        entity_type="claim",
        entity_id=run.claim_id,
        after={
            "agent_run_id": str(run.id),
            "agent_key": run.agent_key,
            "status": run.status,
            "used_llm": run.used_llm,
            "model": run.model_slug,
        },
    )
    await db.flush()
    return run


async def list_runs(db: AsyncSession, principal: Principal, claim_id: uuid.UUID) -> list[AgentRun]:
    claim = await get_visible_claim(db, principal, claim_id)
    return list(
        (
            await db.scalars(
                select(AgentRun).where(AgentRun.claim_id == claim.id).order_by(AgentRun.started_at.desc())
            )
        ).all()
    )


async def get_run(
    db: AsyncSession, principal: Principal, run_id: uuid.UUID
) -> tuple[AgentRun, AgentOutput | None]:
    run = await db.get(AgentRun, run_id)
    if run is None:
        raise NotFoundError("Agent run not found")
    await get_visible_claim(db, principal, run.claim_id)  # object-level access
    output = await db.scalar(select(AgentOutput).where(AgentOutput.agent_run_id == run.id))
    return run, output


__all__ = ["ORDER", "PromptDriftError", "ensure_prompts", "execute_run", "get_run", "list_runs", "start_run"]
