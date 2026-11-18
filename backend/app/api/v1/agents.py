"""Agent runs on a claim: start one agent or all eight, list runs, read a run and its output."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict

from agents.registry import AGENTS, ORDER
from app.api.deps import ActorDep, DbDep, require
from app.core.errors import DependencyUnavailableError
from app.core.request_context import get_request_id
from app.models.agents import AgentRun
from app.security.permissions import P
from app.security.principal import Principal
from app.services import agent_service
from app.services.claim_access import get_visible_claim

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["agents"])
Reader = Annotated[Principal, Depends(require(P.CLAIMS_READ))]
Starter = Annotated[Principal, Depends(require(P.PROCESSING_START))]
AgentKey = Literal[
    "intake", "document", "policy", "fraud", "assessment", "validation", "decision", "compliance"
]


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    claim_id: uuid.UUID
    agent_key: str
    attempt: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    model_slug: str | None
    prompt_version_id: uuid.UUID | None
    rule_set_version: str
    input_hash: str
    used_llm: bool
    llm_note: str | None
    escalate: bool
    findings: list[dict[str, Any]]
    missing: list[dict[str, Any]]
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal
    error_code: str | None
    error_detail: str | None


class AgentRunDetail(AgentRunOut):
    output: dict[str, Any] | None
    output_schema: str | None
    output_hash: str | None


class StartRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_key: AgentKey


class PipelineJob(BaseModel):
    job_id: str
    agents: list[str]


class AgentInfo(BaseModel):
    key: str
    uses_llm: bool
    allowed_tools: list[str]


def _enqueue(name: str, args: list[Any]) -> str:
    from tasks.celery_app import celery_app

    return str(celery_app.send_task(name, args=args, queue="workflow").id)


@router.get("/agent-catalog", response_model=list[AgentInfo])
async def agent_catalog(principal: Reader) -> list[AgentInfo]:
    """The eight agents in pipeline order and the read-only tools each may call."""
    return [
        AgentInfo(key=k, uses_llm=AGENTS[k].uses_llm, allowed_tools=sorted(AGENTS[k].allowed_tools))
        for k in ORDER
    ]


@router.post(
    "/claims/{claim_id}/agent-runs", response_model=AgentRunOut, status_code=status.HTTP_202_ACCEPTED
)
async def start_agent_run(
    claim_id: uuid.UUID, body: StartRun, principal: Starter, actor: ActorDep, db: DbDep
) -> AgentRun:
    run = await agent_service.start_run(db, principal, claim_id, body.agent_key, actor, get_request_id())
    await db.commit()
    try:
        _enqueue("agents.run", [str(run.organization_id), str(run.id)])
    except Exception as exc:  # broker down: finalise the run so it does not block a retry
        logger.exception("agent_enqueue_failed", agent_run_id=str(run.id))
        run.status, run.error_code, run.error_detail = "failed", "ENQUEUE_FAILED", "task broker unavailable"
        run.finished_at = datetime.now(UTC)
        await db.commit()
        raise DependencyUnavailableError("Could not queue the agent run; try again") from exc
    return run


@router.post(
    "/claims/{claim_id}/agent-runs/pipeline", response_model=PipelineJob, status_code=status.HTTP_202_ACCEPTED
)
async def start_pipeline(claim_id: uuid.UUID, principal: Starter, db: DbDep) -> PipelineJob:
    """Run all eight agents in order (sequential; Phase 7 replaces this with the LangGraph graph)."""
    claim = await get_visible_claim(db, principal, claim_id)
    args = [str(claim.organization_id), str(claim.id), str(principal.user_id), get_request_id()]
    try:
        job = _enqueue("agents.run_all", args)
    except Exception as exc:
        raise DependencyUnavailableError("Could not queue the agent pipeline; try again") from exc
    return PipelineJob(job_id=job, agents=list(ORDER))


@router.get("/claims/{claim_id}/agent-runs", response_model=list[AgentRunOut])
async def list_agent_runs(claim_id: uuid.UUID, principal: Reader, db: DbDep) -> list[AgentRun]:
    return await agent_service.list_runs(db, principal, claim_id)


@router.get("/agent-runs/{run_id}", response_model=AgentRunDetail)
async def get_agent_run(run_id: uuid.UUID, principal: Reader, db: DbDep) -> AgentRunDetail:
    run, output = await agent_service.get_run(db, principal, run_id)
    base = AgentRunOut.model_validate(run).model_dump()
    return AgentRunDetail(
        **base,
        output=output.output if output else None,
        output_schema=output.schema_name if output else None,
        output_hash=output.output_hash if output else None,
    )
