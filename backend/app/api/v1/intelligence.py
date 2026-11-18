"""Advanced intelligence (Phase 12): similar claims, anomalies, feedback, prompt versions, evaluations."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import ActorDep, DbDep, require
from app.core.errors import NotFoundError
from app.models.agents import PromptVersion
from app.models.ai import AgentConfig
from app.models.intelligence import AgentFeedback, EvalRun
from app.security.permissions import P
from app.security.principal import Principal
from app.services import anomaly_service, eval_service, feedback_service, similarity
from app.services.agent_service import ensure_prompts
from app.services.metrics_common import Scope

router = APIRouter(tags=["intelligence"])
Reader = Annotated[Principal, Depends(require(P.CLAIMS_READ))]
AiAdmin = Annotated[Principal, Depends(require(P.AI_MANAGE))]
Days = Annotated[int, Query(ge=1, le=365)]


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Literal["helpful", "not_helpful", "agree", "disagree"]
    note: Annotated[str | None, Field(max_length=1000)] = None


class FeedbackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    agent_run_id: uuid.UUID
    agent_key: str
    label: str
    source: str
    note: str | None
    created_at: datetime


class EvalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_key: Annotated[str, Field(max_length=40)]
    prompt_version: Annotated[str | None, Field(pattern=r"^v[0-9]{1,3}$")] = None
    model_id: uuid.UUID | None = None


class CompareIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_key: Annotated[str, Field(max_length=40)]
    model_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=5)]


class EvalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    agent_key: str
    kind: str
    prompt_version: str
    prompt_sha256: str
    model_id: uuid.UUID | None
    model_slug: str | None
    status: str
    cases: int
    metrics: list[dict[str, Any]]
    regressions: list[str]
    model_calls: int
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class PromptOut(BaseModel):
    id: uuid.UUID
    agent_key: str
    version: str
    content_sha256: str
    content: str
    active_for_org: bool
    latest_eval: EvalOut | None


# --- similar claims and anomalies ----------------------------------------------------------------


@router.get("/claims/{claim_id}/similar")
async def similar_claims(
    claim_id: uuid.UUID, principal: Reader, db: DbDep, limit: Annotated[int, Query(ge=1, le=50)] = 10
) -> dict[str, Any]:
    items = await similarity.similar(db, principal, claim_id, limit=limit)
    await db.commit()  # the claim's own embedding may have been (re)computed
    return {
        "model": similarity.MODEL,
        "definition": "Claims whose wording is closest to this one (cosine similarity of local text "
        "embeddings: shared words, phrases and spellings, not meaning). Only claims you can see.",
        "items": items,
    }


@router.post("/intelligence/embeddings/backfill")
async def backfill_embeddings(principal: AiAdmin, db: DbDep) -> dict[str, int]:
    count = await similarity.backfill(db, principal.organization_id)
    await db.commit()
    return {"embedded": count}


@router.get("/analytics/anomalies")
async def anomalies(
    principal: Reader, db: DbDep, days: Days = 90, include_synthetic: bool = True
) -> dict[str, Any]:
    return await anomaly_service.detect(db, Scope(principal, days, include_synthetic))


# --- feedback ------------------------------------------------------------------------------------


@router.post("/agent-runs/{run_id}/feedback", response_model=FeedbackOut, status_code=status.HTTP_201_CREATED)
async def give_feedback(run_id: uuid.UUID, body: FeedbackIn, principal: Reader, db: DbDep) -> AgentFeedback:
    row = await feedback_service.give(db, principal, run_id, body.label, body.note)
    await db.commit()
    return row


@router.get("/analytics/feedback")
async def feedback_summary(principal: Reader, db: DbDep, days: Days = 90) -> dict[str, Any]:
    return await feedback_service.summary(db, Scope(principal, days, True))


@router.get("/intelligence/curation-cases")
async def curation_cases(principal: AiAdmin, db: DbDep, days: Days = 90) -> list[dict[str, Any]]:
    return await feedback_service.curation_cases(db, Scope(principal, days, True))


# --- prompt versions -----------------------------------------------------------------------------


@router.get("/prompts", response_model=list[PromptOut])
async def list_prompts(principal: AiAdmin, db: DbDep, agent_key: str | None = None) -> list[PromptOut]:
    await ensure_prompts(db)
    await db.commit()
    stmt = select(PromptVersion).order_by(PromptVersion.agent_key, PromptVersion.version)
    if agent_key:
        stmt = stmt.where(PromptVersion.agent_key == agent_key)
    org_configs = await db.scalars(
        select(AgentConfig).where(AgentConfig.organization_id == principal.organization_id)
    )
    selected = {c.agent_key: c.prompt_version for c in org_configs}
    out = []
    for p in await db.scalars(stmt):
        latest = await db.scalar(
            select(EvalRun)
            .where(
                EvalRun.organization_id == principal.organization_id,
                EvalRun.agent_key == p.agent_key,
                EvalRun.prompt_sha256 == p.content_sha256,
            )
            .order_by(EvalRun.created_at.desc())
            .limit(1)
        )
        out.append(
            PromptOut(
                id=p.id,
                agent_key=p.agent_key,
                version=p.version,
                content_sha256=p.content_sha256,
                content=p.content,
                active_for_org=selected.get(p.agent_key, "v1") == p.version,
                latest_eval=EvalOut.model_validate(latest) if latest else None,
            )
        )
    return out


@router.post("/prompts/{prompt_id}/activate")
async def activate_prompt(
    prompt_id: uuid.UUID, principal: AiAdmin, actor: ActorDep, db: DbDep
) -> dict[str, str]:
    """Run this prompt version for the organisation. Refused without a passed regression run."""
    prompt = await db.get(PromptVersion, prompt_id)
    if prompt is None:
        raise NotFoundError("Prompt version not found")
    config = await eval_service.activate_prompt(db, principal, actor, prompt.agent_key, prompt.version)
    await db.commit()
    return {"agent_key": config.agent_key, "prompt_version": config.prompt_version}


# --- evaluations ---------------------------------------------------------------------------------


@router.post("/evals", response_model=EvalOut, status_code=status.HTTP_202_ACCEPTED)
async def request_eval(body: EvalIn, principal: AiAdmin, actor: ActorDep, db: DbDep) -> EvalRun:
    run = await eval_service.request(
        db,
        principal,
        actor,
        agent_key=body.agent_key,
        prompt_version=body.prompt_version,
        model_id=body.model_id,
    )
    await db.commit()
    eval_service.enqueue(run)
    return run


@router.post("/evals/compare", response_model=list[EvalOut], status_code=status.HTTP_202_ACCEPTED)
async def compare_models(body: CompareIn, principal: AiAdmin, actor: ActorDep, db: DbDep) -> list[EvalRun]:
    """One regression run per model on the same cases, for a side-by-side comparison."""
    runs = [
        await eval_service.request(
            db, principal, actor, agent_key=body.agent_key, prompt_version=None, model_id=model_id
        )
        for model_id in dict.fromkeys(body.model_ids)
    ]
    await db.commit()
    for run in runs:
        eval_service.enqueue(run)
    return runs


@router.get("/evals", response_model=list[EvalOut])
async def list_evals(principal: AiAdmin, db: DbDep, agent_key: str | None = None) -> list[EvalRun]:
    return await eval_service.list_runs(db, principal, agent_key)


@router.get("/evals/{run_id}")
async def get_eval(run_id: uuid.UUID, principal: AiAdmin, db: DbDep) -> dict[str, Any]:
    run = await db.get(EvalRun, run_id)
    if run is None or run.organization_id != principal.organization_id:
        raise NotFoundError("Evaluation run not found")
    return {**EvalOut.model_validate(run).model_dump(mode="json"), "parts": run.parts}
