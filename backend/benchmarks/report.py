"""Render a benchmark results directory as Markdown tables (Phase 14 report).

python -m benchmarks.report backend/benchmarks/results/<stamp> > report-tables.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

NODE_ORDER = (
    "INTAKE",
    "DOCUMENT_PROCESSING",
    "DOCUMENT_VALIDATION",
    "POLICY_VERIFICATION",
    "FRAUD_ANALYSIS",
    "CLAIM_ASSESSMENT",
    "RESULT_AGGREGATION",
    "CROSS_VALIDATION",
    "DECISION_RECOMMENDATION",
    "COMPLIANCE_CHECK",
)


def f(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def claims_table(runs: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Profile | Concurrency | Claims ok | Process P50 / P90 / P95 / max (s) | Documents P50 / P90 (s) "
        "| End-to-end P50 / P90 / P95 (s) | Throughput (claims/min) | LLM calls / claim | SSE lag P95 (ms) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        s = r["summary"]
        p, d, e = s["process_s"], s["documents_s"], s["end_to_end_s"]
        lines.append(
            f"| {r['profile']} | {r['concurrency']} | {s['succeeded']}/{s['claims']} "
            f"| {f(p['p50'])} / {f(p['p90'])} / {f(p['p95'])} / {f(p['max'])} "
            f"| {f(d['p50'])} / {f(d['p90'])} "
            f"| {f(e['p50'])} / {f(e['p90'])} / {f(e['p95'])} "
            f"| {f(r['throughput_claims_per_min'], 1)} | {f(s['llm_calls_per_claim']['mean'], 1)} "
            f"| {f(s['sse_lag_ms']['p95'], 0)} |"
        )
    return lines


def nodes_table(runs: list[dict[str, Any]]) -> list[str]:
    header = (
        "| Node | " + " | ".join(f"{r['profile']} c{r['concurrency']} P50 / P95 (s)" for r in runs) + " |"
    )
    lines = [header, "|---|" + "---|" * len(runs)]
    for node in NODE_ORDER:
        cells = []
        for r in runs:
            s = r["summary"]["nodes_s"].get(node)
            cells.append(f"{f(s['p50'], 3)} / {f(s['p95'], 3)}" if s else "—")
        lines.append(f"| {node} | " + " | ".join(cells) + " |")
    return lines


def api_table(runs: list[dict[str, Any]]) -> list[str]:
    lines = []
    for r in runs:
        lines += [
            f"**{r['base_url']}**, {r['concurrency']} concurrent clients, {f(r['seconds'], 0)} s: "
            f"{r['requests']} requests, {f(r['throughput_rps'], 0)} req/s, "
            f"errors {sum(r['errors'].values())}, "
            f"overall P50 {f(r['all_ms']['p50'], 0)} / P95 {f(r['all_ms']['p95'], 0)} / P99 "
            f"{f(r['all_ms']['p99'], 0)} ms.",
            "",
            "| Endpoint | n | P50 (ms) | P95 (ms) | P99 (ms) | max (ms) |",
            "|---|---|---|---|---|---|",
        ]
        for name, s in r["endpoints_ms"].items():
            lines.append(
                f"| `{name}` | {s['n']} | {f(s['p50'], 0)} | {f(s['p95'], 0)} "
                f"| {f(s['p99'], 0)} | {f(s['max'], 0)} |"
            )
        if r["errors"]:
            lines.append("")
            lines.append("Errors: " + ", ".join(f"`{k}` x{n}" for k, n in r["errors"].items()))
        lines.append("")
    return lines


def render(directory: Path) -> str:
    results = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]
    claims = sorted((r for r in results if "summary" in r), key=lambda r: (r["profile"], r["concurrency"]))
    apis = sorted(
        (r for r in results if "endpoints_ms" in r), key=lambda r: (r["base_url"], r["concurrency"])
    )
    out = [f"Results: `{directory.as_posix()}`", ""]
    if claims:
        ai = claims[0]["ai"]
        out += [
            f"AI provider: {ai.get('provider')}, key status: {ai.get('key_status')} "
            f"({ai.get('key_detail') or 'ok'}), default model: {ai.get('default_model')}.",
            "",
            "### Claim processing",
            "",
            *claims_table(claims),
            "",
            "### Time per workflow node",
            "",
            *nodes_table(claims),
            "",
        ]
    if apis:
        out += ["### Read API latency", "", *api_table(apis)]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: python -m benchmarks.report <results directory>", file=sys.stderr)
        return 2
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # dashes on Windows consoles
    sys.stdout.write(render(Path(args[0])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
