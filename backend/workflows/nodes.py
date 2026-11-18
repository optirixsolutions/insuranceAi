"""Graph nodes (02-workflow.md §3). Each node is wrapped for cancellation, visit limits,
heartbeats, claim status, and node_started / node_completed / node_failed events.

Routing decisions are taken inside nodes and stored in ``state["route"]``; the graph's
conditional edges only read that value (pure functions, no I/O).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt
from sqlalchemy import select

from app.models.agents import AgentOutput, AgentRun
from app.models.documents import ClaimDocument
from app.models.workflow import WorkflowEvent
from app.services import agent_service
from app.services.audit_service import AuditActor
from rules_engine.claim_status import ClaimStatus, InvalidTransitionError
from workflows.runtime import CancelledByUserError, Runtime, emit, publish, set_claim_status
from workflows.state import ClaimState

MAX_VISITS = 60
MAX_REASSESSMENTS = 3
MAX_MORE_INFO = 5
DOC_WAIT_SECONDS = 900
PENDING_DOC_STATES = ("queued", "processing")
WEIGHTS = {
    "INTAKE": 5,
    "DOCUMENT_PROCESSING": 20,
    "DOCUMENT_VALIDATION": 3,
    "POLICY_VERIFICATION": 15,
    "FRAUD_ANALYSIS": 15,
    "CLAIM_ASSESSMENT": 15,
    "RESULT_AGGREGATION": 2,
    "CROSS_VALIDATION": 10,
    "DECISION_RECOMMENDATION": 10,
    "COMPLIANCE_CHECK": 5,
}
Step = Callable[[Runtime, ClaimState], Awaitable[tuple[dict[str, Any], str, dict[str, Any]]]]


class NodeFailedError(RuntimeError):
    pass


def runtime_of(config: RunnableConfig) -> Runtime:
    rt: Runtime = (config.get("configurable") or {})["runtime"]
    return rt


def retryable(exc: BaseException) -> bool:
    return not isinstance(exc, (CancelledByUserError, GraphInterrupt, NodeFailedError))


async def _progress(rt: Runtime, s: Any, including: str) -> Decimal:
    done = {including} | set(
        (
            await s.scalars(
                select(WorkflowEvent.node).where(
                    WorkflowEvent.execution_id == rt.execution_id,
                    WorkflowEvent.event_type == "node_completed",
                )
            )
        ).all()
    )
    total = sum(WEIGHTS.values())
    return (Decimal(sum(w for n, w in WEIGHTS.items() if n in done)) / total).quantize(Decimal("0.0001"))


def wrap(name: str, step: Step, *, status: ClaimStatus | None = None, agent: str | None = None) -> Any:
    async def node(state: ClaimState, config: RunnableConfig) -> dict[str, Any]:
        rt = runtime_of(config)
        async with rt.session() as s:
            await rt.check_cancelled(s)
            execution = await rt.execution(s, lock=True)
            execution.iteration_count += 1
            if execution.iteration_count > MAX_VISITS:
                raise NodeFailedError(f"visit limit of {MAX_VISITS} nodes reached")
            await rt.heartbeat(s, name)
            events = []
            if status is not None:
                events += await move(s, execution, status, f"workflow: {name.lower()}")
            events.append(await emit(s, execution, "node_started", node=name, agent_key=agent))
            await s.commit()
        await publish(rt.redis, events)
        started = time.monotonic()
        try:
            update, summary, info = await step(rt, state)
        except (GraphInterrupt, CancelledByUserError):
            raise
        except Exception as exc:
            async with rt.session() as s:
                execution = await rt.execution(s)
                failed = await emit(
                    s,
                    execution,
                    "node_failed",
                    node=name,
                    agent_key=agent,
                    payload={"error": type(exc).__name__, "detail": str(exc)[:300]},
                )
                await s.commit()
            await publish(rt.redis, [failed])
            raise
        async with rt.session() as s:
            execution = await rt.execution(s, lock=True)
            execution.progress = await _progress(rt, s, name)
            done = await emit(
                s,
                execution,
                "node_completed",
                node=name,
                agent_key=agent,
                payload={
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "summary": summary,
                    "progress": float(execution.progress),
                    **info,
                },
            )
            await s.commit()
        await publish(rt.redis, [done])
        return {**update, "completed": [name], "summaries": {**update.get("summaries", {}), name: summary}}

    node.__name__ = name
    return node


async def move(s: Any, execution: Any, target: ClaimStatus, reason: str) -> list[WorkflowEvent]:
    try:
        return await set_claim_status(s, execution, target, reason, "workflow")
    except InvalidTransitionError:
        return await set_claim_status(s, execution, target, reason, "decision")


async def _output(rt: Runtime, run_id: str | None) -> dict[str, Any]:
    if not run_id:
        return {}
    async with rt.session() as s:
        out = await s.scalar(select(AgentOutput).where(AgentOutput.agent_run_id == uuid.UUID(run_id)))
        return dict(out.output) if out else {}


SUMMARY: dict[str, Callable[[dict[str, Any]], str]] = {
    "intake": lambda o: (
        f"Priority {o.get('priority')}; {len(o.get('missing_documents', []))} mandatory document(s) missing"
    ),
    "document": lambda o: (
        f"{len(o.get('documents', []))} document(s); "
        f"{sum(len(d['needs_human_verification']) for d in o.get('documents', []))} item(s) to verify"
    ),
    "policy": lambda o: (
        f"Version {'found' if o.get('policy_version_id') else 'not found'}; primary coverage "
        f"{(o.get('coverages') or [{}])[0].get('status', 'n/a')}; "
        f"{len(o.get('exclusions_need_interpretation', []))} exclusion(s) to interpret"
    ),
    "fraud": lambda o: (
        f"Risk {o.get('risk_score')}/100 ({o.get('risk_level')}); {len(o.get('indicators', []))} indicator(s)"
    ),
    "assessment": lambda o: (
        f"{o.get('status')}; eligible payout {o.get('eligible_payout')} {o.get('currency')}"
    ),
    "validation": lambda o: (
        f"{len(o.get('findings', []))} finding(s); worst {o.get('max_severity') or 'none'}"
    ),
    "decision": lambda o: f"{o.get('recommendation')} (rule {o.get('matrix_rule')})",
    "compliance": lambda o: (
        f"{o.get('status')}; human review {'required' if o.get('human_review_required') else 'not required'}"
    ),
}


async def run_agent(rt: Runtime, key: str, node: str) -> tuple[AgentRun, dict[str, Any]]:
    async with rt.session() as s:
        run = await agent_service.create_run(
            s,
            rt.organization_id,
            rt.claim_id,
            key,
            AuditActor("agent", None, rt.request_id),
            rt.request_id,
            rt.user_id,
            execution_id=rt.execution_id,
            node=node,
        )
        await s.commit()
        run = await agent_service.execute_run(s, rt.settings, rt.throttle, rt.organization_id, run.id)
        await s.commit()
        execution = await rt.execution(s, lock=True)
        execution.tokens_used += run.prompt_tokens + run.completion_tokens
        execution.cost_usd += run.cost_usd
        await s.commit()
    if run.status not in ("succeeded", "partial"):
        raise NodeFailedError(f"{key} agent {run.status}: {run.error_code} {run.error_detail or ''}"[:300])
    return run, await _output(rt, str(run.id))


def agent_step(key: str, node: str, route: Callable[[dict[str, Any], ClaimState], str] | None = None) -> Step:
    async def step(rt: Runtime, state: ClaimState) -> tuple[dict[str, Any], str, dict[str, Any]]:
        run, output = await run_agent(rt, key, node)
        update: dict[str, Any] = {"runs": {key: str(run.id)}}
        if route is not None:
            update["route"] = route(output, state)
        info = {
            "agent_run_id": str(run.id),
            "status": run.status,
            "used_llm": run.used_llm,
            "attempt": run.attempt,
        }
        return update, SUMMARY[key](output), info

    return step


# --- deterministic and interrupt steps -----------------------------------------------------------


async def wait_for_documents(rt: Runtime, state: ClaimState) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """The Phase 4 pipeline processes uploads on the documents queue; wait for it, then read them."""
    deadline = time.monotonic() + DOC_WAIT_SECONDS
    while True:
        async with rt.session() as s:
            await rt.check_cancelled(s)
            pending = (
                await s.scalars(
                    select(ClaimDocument.original_filename).where(
                        ClaimDocument.claim_id == rt.claim_id,
                        ClaimDocument.deleted_at.is_(None),
                        ClaimDocument.processing_status.in_(PENDING_DOC_STATES),
                    )
                )
            ).all()
            await rt.heartbeat(s)
            await s.commit()
        if not pending:
            break
        if time.monotonic() > deadline:
            raise NodeFailedError(
                f"documents still processing after {DOC_WAIT_SECONDS}s: {list(pending)[:5]}"
            )
        await asyncio.sleep(3)
    update, summary, info = await agent_step("document", "DOCUMENT_PROCESSING")(rt, state)
    if state.get("recheck_intake"):  # documents now carry their types: re-check required documents
        run, _ = await run_agent(rt, "intake", "DOCUMENT_PROCESSING")
        update["runs"]["intake"] = str(run.id)
        update["recheck_intake"] = False
        summary += "; intake re-checked"
    return update, summary, info


async def validate_documents(rt: Runtime, state: ClaimState) -> tuple[dict[str, Any], str, dict[str, Any]]:
    out = await _output(rt, state.get("runs", {}).get("document"))
    items = [
        f"{d['filename']}: {n}"
        for d in out.get("documents", [])
        for n in d.get("needs_human_verification", [])
    ]
    items += [
        f"{d['filename']}: unreadable"
        for d in out.get("documents", [])
        if d.get("readability") == "unreadable"
    ]
    review = bool(items) and not state.get("review_override")
    summary = f"{len(items)} item(s) need verification" + (" - sent to document review" if review else "")
    return {"route": "review" if review else "continue", "summaries": {"_verify": items}}, summary, {}


def interrupt_payload(kind: str, **data: Any) -> dict[str, Any]:
    return {"interrupt_id": uuid.uuid4().hex, "kind": kind, **data}


async def document_review(rt: Runtime, state: ClaimState) -> tuple[dict[str, Any], str, dict[str, Any]]:
    async with rt.session() as s:
        execution = await rt.execution(s)
        events = await move(
            s, execution, ClaimStatus.DOCUMENT_REVIEW, "workflow: fields need human verification"
        )
        await s.commit()
    await publish(rt.redis, events)
    answer = interrupt(
        interrupt_payload(
            "document_review",
            items=state.get("summaries", {}).get("_verify", [])[:50],
            actions=["fields_verified", "continue"],
        )
    )
    action = str(answer.get("action"))
    route = "reprocess" if action == "fields_verified" else "continue"
    return (
        {"route": route, "review_override": route == "continue", "resume": answer},
        f"Resumed: {action}",
        {"action": action},
    )


async def aggregate(rt: Runtime, state: ClaimState) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Supervisor: every branch produced a valid output (else re-run it once); cross-agent contradictions."""
    runs = dict(state.get("runs", {}))
    rerun: list[str] = []
    outputs: dict[str, dict[str, Any]] = {}
    for key, node in (
        ("intake", "INTAKE"),
        ("document", "DOCUMENT_PROCESSING"),
        ("policy", "POLICY_VERIFICATION"),
        ("fraud", "FRAUD_ANALYSIS"),
        ("assessment", "CLAIM_ASSESSMENT"),
    ):
        outputs[key] = await _output(rt, runs.get(key))
        if not outputs[key]:
            run, outputs[key] = await run_agent(rt, key, node)
            runs[key] = str(run.id)
            rerun.append(key)
    contradictions = []
    if outputs["intake"].get("duplicates") and not any(
        i["rule_code"] == "F-DUP-CLAIM" for i in outputs["fraud"].get("indicators", [])
    ):
        contradictions.append("intake found a possible duplicate claim that fraud analysis did not flag")
    if not outputs["policy"].get("policy_version_id") and outputs["assessment"].get("status") == "calculated":
        contradictions.append("assessment calculated a payout without a resolved policy version")
    if outputs["policy"].get("holder_match") == "mismatch" and not any(
        i["rule_code"] == "F-ID-MISMATCH" for i in outputs["fraud"].get("indicators", [])
    ):
        contradictions.append("policy holder mismatch not reflected in fraud indicators")
    summary = f"{5 - len(rerun)}/5 branch outputs valid" + (f"; re-ran {', '.join(rerun)}" if rerun else "")
    summary += f"; {len(contradictions)} contradiction(s)"
    return (
        {"runs": runs, "summaries": {"_contradictions": contradictions}},
        summary,
        {"contradictions": contradictions},
    )


def _missing_items(out_intake: dict[str, Any], out_decision: dict[str, Any]) -> list[str]:
    items = [f"document: {d}" for d in out_intake.get("missing_documents", [])]
    items += list(out_intake.get("field_problems", []))
    items += [m for m in out_decision.get("missing_information", []) if m not in items]
    return items[:30]


async def more_info(rt: Runtime, state: ClaimState) -> tuple[dict[str, Any], str, dict[str, Any]]:
    count = state.get("more_info_count", 0) + 1
    if count > MAX_MORE_INFO:
        return (
            {"route": "human_review", "more_info_count": count},
            f"More-information limit ({MAX_MORE_INFO}) reached: escalated",
            {},
        )
    async with rt.session() as s:
        execution = await rt.execution(s)
        execution.more_info_count = count
        events = await move(s, execution, ClaimStatus.AWAITING_INFORMATION, "workflow: information required")
        await s.commit()
    await publish(rt.redis, events)
    runs = state.get("runs", {})
    items = _missing_items(await _output(rt, runs.get("intake")), await _output(rt, runs.get("decision")))
    answer = interrupt(
        interrupt_payload(
            "more_information", requested_items=items, actions=["information_supplied", "escalate"]
        )
    )
    action = str(answer.get("action"))
    route = "human_review" if action == "escalate" else "intake"
    return (
        {"route": route, "more_info_count": count, "resume": answer},
        f"Resumed: {action}",
        {"action": action},
    )


# --- routers (pure) ------------------------------------------------------------------------------


async def compliance(rt: Runtime, state: ClaimState) -> tuple[dict[str, Any], str, dict[str, Any]]:
    update, summary, info = await agent_step("compliance", "COMPLIANCE_CHECK")(rt, state)
    decision = await _output(rt, state.get("runs", {}).get("decision"))
    # No STP gate yet (Phase 8): every recommendation goes to a person; missing information goes back out.
    compliance_out = await _output(rt, update["runs"]["compliance"])
    if decision.get("recommendation") == "MORE_INFORMATION_REQUIRED":
        update["route"] = "more_info"
    elif compliance_out.get("automated_approval_allowed") and not compliance_out.get("human_review_required"):
        update["route"] = "stp"  # eligibility only: AUTO_APPROVAL_ELIGIBILITY re-checks every gate
    else:
        update["route"] = "human_review"
    return update, summary, info


def by_route(state: ClaimState) -> str:
    return state.get("route", "")


async def intake(rt: Runtime, state: ClaimState) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Intake; documents still in the upload pipeline count as provided (DOCUMENT_PROCESSING waits
    for them). Missing documents are caught again by the decision matrix after processing."""
    update, summary, info = await agent_step("intake", "INTAKE")(rt, state)
    output = await _output(rt, update["runs"]["intake"])
    async with rt.session() as s:
        pending = await s.scalar(
            select(ClaimDocument.id)
            .where(
                ClaimDocument.claim_id == rt.claim_id,
                ClaimDocument.deleted_at.is_(None),
                ClaimDocument.processing_status.in_(PENDING_DOC_STATES),
            )
            .limit(1)
        )
    missing = bool(output.get("missing_documents")) and pending is None
    update["route"] = "more_info" if missing or not output.get("mandatory_fields_ok", True) else "documents"
    update["recheck_intake"] = pending is not None and bool(output.get("missing_documents"))
    if update["recheck_intake"]:
        summary += " (uploads still processing; re-checked after document processing)"
    return update, summary, info
