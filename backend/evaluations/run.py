"""Run the Phase 6 evaluation sets and write a report.

    python -m evaluations.run                       # deterministic, all sets
    python -m evaluations.run --live --org <uuid>   # also: injection + document sets through real models

Live runs use the organisation's agent configuration (verified models only, data-policy gate,
budgets) and record every call in model_invocations. Free models are rate-limited (OpenRouter's
free tier allows a small number of requests per day), so ``--live-limit`` caps the cases sent.
Run inside the backend container for the OCR variants of the document set.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evaluations import (
    assessment_set,
    decision_set,
    document_set,
    fraud_set,
    injection_set,
    policy_set,
    validation_set,
)
from evaluations.harness import EvalResult

SETS = {
    "document": document_set.evaluate,
    "policy": policy_set.evaluate,
    "fraud": fraud_set.evaluate,
    "assessment": assessment_set.evaluate,
    "validation": validation_set.evaluate,
    "decision": decision_set.evaluate,
    "injection": injection_set.evaluate,
}


def render(results: list[EvalResult], started: datetime) -> str:
    lines = [
        "# Phase 6 agent evaluation report",
        "",
        f"Generated {started.isoformat(timespec='seconds')} by `python -m evaluations.run`. "
        "Synthetic data only.",
        "",
        "| Set | Mode | Cases | Metric | Value | Gate | Result |",
        "|-----|------|-------|--------|-------|------|--------|",
    ]
    for r in results:
        for m in r.metrics:
            gate = (
                "reported"
                if m.threshold is None
                else (f">= {m.threshold}" if m.higher_is_better else f"<= {m.threshold}")
            )
            result = "pass" if m.passed else "FAIL"
            row = (r.agent, r.mode, r.cases, m.name, f"{m.value:.4g}", gate, result)
            lines.append("| " + " | ".join(str(x) for x in row) + " |")
    for r in results:
        if r.notes or r.failures:
            lines += ["", f"## {r.agent} ({r.mode})", ""]
            lines += [f"- Note: {n}" for n in r.notes]
            lines += [f"- Failure: {f}" for f in r.failures[:25]]
    return "\n".join(lines) + "\n"


async def live_factory(org: uuid.UUID) -> tuple[Any, Any]:
    """Per-agent gateway clients for one organisation, plus a closer."""
    from redis.asyncio import Redis
    from sqlalchemy import select

    from app.core.config import get_settings
    from app.core.db import Database
    from app.core.tenancy import bind_tenant
    from app.models.ai import AgentConfig
    from app.services import ai_service
    from app.services.agent_service import GatewayLlm
    from model_gateway.throttle import Throttle

    settings = get_settings()
    db = Database.from_settings(settings, application_name="claimos-eval")
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    session = db.sessionmaker()
    await bind_tenant(session, org)
    await ai_service.ensure_agent_configs(session, org, settings)
    configs = {
        c.agent_key: c
        for c in (await session.scalars(select(AgentConfig).where(AgentConfig.organization_id == org))).all()
    }
    allows = await ai_service.org_allows_real_data(session, org)
    clients: dict[str, GatewayLlm] = {}

    def factory(key: str) -> GatewayLlm | None:
        if key not in configs:
            return None
        if key not in clients:
            clients[key] = GatewayLlm(
                session,
                settings,
                Throttle(redis),
                org,
                configs[key],
                data_class="synthetic",
                allows_real=allows,
                request_id="eval",
                run_id=None,
            )
        return clients[key]

    async def close() -> dict[str, Any]:
        await session.commit()
        usage = {
            k: {
                "calls": len(c.calls),
                "models": sorted({x.model for x in c.calls if x.model}),
                "notes": c.notes[:3],
            }
            for k, c in clients.items()
        }
        await session.close()
        await redis.aclose()
        await db.dispose()
        return usage

    return factory, close


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sets", default=",".join(SETS))
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--org", type=uuid.UUID)
    parser.add_argument("--live-limit", type=int, default=3)
    parser.add_argument("--out", type=Path, default=Path("evaluations/reports"))
    args = parser.parse_args()
    started = datetime.now(UTC)
    results: list[EvalResult] = []
    for name in args.sets.split(","):
        results.append(await SETS[name]())
        print(f"{name}: {'PASS' if results[-1].passed else 'FAIL'}")
    live_usage: dict[str, Any] = {}
    if args.live:
        if args.org is None:
            parser.error("--live needs --org (an organisation with verified models assigned to its agents)")
        factory, close = await live_factory(args.org)
        try:
            results.append(await injection_set.evaluate(factory, limit=args.live_limit))
            results.append(await document_set.evaluate(factory, limit=args.live_limit))
        finally:
            live_usage = await close()
        print(f"live: {json.dumps(live_usage)}")
    args.out.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    report = {
        "generated_at": started.isoformat(),
        "results": [r.as_dict() for r in results],
        "live_usage": live_usage,
    }
    (args.out / f"phase6-{stamp}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.out / f"phase6-{stamp}.md").write_text(render(results, started), encoding="utf-8")
    print(render(results, started))
    gated = [r for r in results if r.mode == "deterministic"]
    return 0 if all(r.passed for r in gated) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
