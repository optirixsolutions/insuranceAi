"""End-to-end claim processing benchmark against the running stack (Phase 14).

    python -m benchmarks.claims_bench --base-url http://nginx --profile simple --claims 10 --concurrency 2

Per synthetic claim: create it, upload its documents, wait until every document is processed, start
processing, follow the live event stream until the run stops (human review, decision pending, approved or
failed). Times come from the server's own event timestamps where they exist:

* ``process_s``   ``POST /processing`` accepted (execution ``created_at``) -> the stopping event. This is the
                  roadmap's target metric (06-roadmap, section 1).
* ``documents_s`` upload start -> all documents processed (client clock, 0.5 s polling).
* ``end_to_end_s`` upload start -> stopping event (what a claimant waits for).
* per-node durations (``node_started`` -> ``node_completed``) and SSE delivery lag (event received minus
  ``occurred_at``; run inside the compose network so both clocks are the same machine's).

Only synthetic data (``is_synthetic: true``) is sent.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.client import BenchClient
from benchmarks.stats import meets, summary
from evaluations.builders import driving_licence, motor_claim_form, motor_estimate
from evaluations.renderers import photo_image, scan_image, text_pdf

STOP_EVENTS = {"interrupt", "execution_failed", "execution_completed", "execution_cancelled"}
TARGET_P50_S, TARGET_P90_S = 180.0, 240.0
FIRST = ("Rohan", "Amara", "Lena", "Kofi", "Mei", "Tomas", "Priya", "Owen", "Sana", "Felix", "Ines", "Yusuf")
LAST = (
    "Tanaka",
    "Okafor",
    "Lindqvist",
    "Mensah",
    "Chen",
    "Novak",
    "Raman",
    "Hughes",
    "Malik",
    "Berg",
    "Duarte",
)
NARRATIVES = (
    "Rear-ended at a junction in {town} by a delivery van while stationary at the lights.",
    "Another car reversed out of a parking bay in {town} and struck my rear bumper.",
    "Queueing traffic stopped suddenly on the ring road near {town}; the car behind hit my bumper.",
    "Hit from behind at a mini roundabout in {town} by a driver who did not stop in time.",
)
TOWNS = ("Leeds", "York", "Bristol", "Derby", "Bath", "Hull", "Exeter", "Preston")


@dataclass(frozen=True)
class Person:
    """One synthetic policyholder per claim, so every claim's documents are unique (identical files would
    correctly be flagged as possible duplicates and send the claim to review)."""

    holder: str
    policy_number: str
    registration: str
    estimate_number: str
    narrative: str


def person_for(index: int, run: str) -> Person:
    holder = f"{FIRST[index % len(FIRST)]} {LAST[(index * 7) % len(LAST)]}"
    return Person(
        holder=holder,
        policy_number=f"NWM-{run[:4].upper()}-{24001 + index}",
        registration=f"LK{21 + index % 9}{chr(65 + index % 26)}{chr(66 + (index // 26) % 24)}C",
        estimate_number=f"EST-{55210 + index}",
        narrative=NARRATIVES[index % len(NARRATIVES)].format(town=TOWNS[index % len(TOWNS)]),
    )


def _ts(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def documents_for(
    profile: str, seed: int, person: Person | None = None
) -> list[tuple[str, tuple[str, bytes, str]]]:
    """Profiles: simple = born-digital PDFs + photo (reference workload); scanned = forms as clean scans;
    poor = degraded scans (blur, noise, rotation), reported separately and expected to escalate."""
    who = person or person_for(seed, "bench")
    form = motor_claim_form(holder=who.holder, policy=who.policy_number, reg=who.registration)
    estimate = motor_estimate(
        holder=who.holder, policy=who.policy_number, reg=who.registration, number=who.estimate_number
    )
    licence = driving_licence(holder=who.holder)
    photo = ("damage.jpg", photo_image(seed), "image/jpeg")
    if profile == "simple":
        pages = [
            ("claim-form.pdf", text_pdf(form), "application/pdf"),
            ("estimate.pdf", text_pdf(estimate), "application/pdf"),
            ("licence.pdf", text_pdf(licence), "application/pdf"),
        ]
    elif profile == "scanned":
        pages = [
            ("claim-form.png", scan_image(form, seed=seed), "image/png"),
            ("estimate.png", scan_image(estimate, seed=seed), "image/png"),
            ("licence.pdf", text_pdf(licence), "application/pdf"),
        ]
    elif profile == "poor":
        pages = [
            ("claim-form.jpg", scan_image(form, fmt="JPEG", degrade="blur", seed=seed), "image/jpeg"),
            (
                "estimate.jpg",
                scan_image(estimate, fmt="JPEG", degrade="noise", rotate=3, seed=seed),
                "image/jpeg",
            ),
            ("licence.png", scan_image(licence, degrade="faded", seed=seed), "image/png"),
        ]
    else:
        raise ValueError(f"unknown profile {profile}")
    return [("files", f) for f in [*pages, photo]]


def _clause(ref: str, section: str, title: str, text: str) -> dict[str, str]:
    return {"clause_ref": ref, "section": section, "title": title, "text": text}


def setup_product(client: BenchClient) -> dict[str, Any]:
    product: dict[str, Any] = client.ok(
        "POST",
        "/products",
        201,
        json={
            "code": f"MOT-{uuid.uuid4().hex[:5].upper()}",
            "name": "Motor (synthetic)",
            "line": "motor",
            "currency": "GBP",
        },
    )
    return product


def policyholder(client: BenchClient, product: dict[str, Any], person: Person) -> dict[str, Any]:
    customer = client.ok("POST", "/customers", 201, json={"full_name": person.holder, "is_synthetic": True})
    policy = client.ok(
        "POST",
        "/policies",
        201,
        json={
            "policy_number": person.policy_number,
            "customer_id": customer["id"],
            "product_id": product["id"],
            "is_synthetic": True,
            "publish_initial_version": True,
            "initial_version": {
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
                "sum_insured": "18000.00",
                "deductible_amount": "500.00",
                "clauses": [
                    _clause(
                        "2.1", "coverage", "Own damage", "Accidental damage by collision, fire or theft."
                    ),
                    _clause("4.2", "exclusion", "Unlicensed driver", "No cover without a valid licence."),
                    _clause("4.5", "exclusion", "Wear and tear", "No cover for wear, rust or breakdown."),
                ],
                "coverages": [
                    {
                        "coverage_code": "own_damage",
                        "perils": ["collision", "fire", "theft"],
                        "limit_amount": "18000.00",
                        "clause_ref": "2.1",
                    }
                ],
                "exclusions": [
                    {
                        "exclusion_code": "unlicensed_driver",
                        "applies_to": ["own_damage"],
                        "description": "Unlicensed driver",
                        "clause_ref": "4.2",
                    },
                    {
                        "exclusion_code": "wear_and_tear",
                        "applies_to": ["own_damage"],
                        "description": "Wear and tear",
                        "clause_ref": "4.5",
                    },
                ],
            },
        },
    )
    return {"customer": customer, "policy": policy}


def _wait_documents(
    client: BenchClient, claim_id: str, expected: int, timeout: float
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        docs: list[dict[str, Any]] = client.ok("GET", f"/claims/{claim_id}/documents")
        terminal = [
            d for d in docs if d["processing_status"] in ("processed", "needs_review", "rejected", "failed")
        ]
        if len(docs) >= expected and len(terminal) == len(docs):
            return docs
        time.sleep(0.5)
    raise TimeoutError(f"documents of {claim_id} not processed within {timeout}s")


def _follow(client: BenchClient, execution_id: str, timeout: float) -> list[float]:
    """Read the SSE stream until a stopping event; returns delivery lags in ms."""
    lags: list[float] = []
    with client.http.stream(
        "GET",
        f"/workflow-executions/{execution_id}/events",
        headers={"Cookie": client.cookie_header()},
        timeout=timeout,
    ) as stream:
        for line in stream.iter_lines():
            if not line.startswith("data: "):
                continue
            received = time.time()
            event = json.loads(line[6:])
            if event.get("occurred_at"):
                lags.append((received - _ts(event["occurred_at"])) * 1000)
            if event.get("type") in STOP_EVENTS:
                break
    return lags


def run_claim(
    client: BenchClient, product: dict[str, Any], run: str, profile: str, index: int, timeout: float
) -> dict[str, Any]:
    record: dict[str, Any] = {"index": index, "profile": profile, "ok": False}
    try:
        person = person_for(index, run)
        book = policyholder(client, product, person)  # set-up, not timed
        claim = client.ok(
            "POST",
            "/claims",
            201,
            headers={"Idempotency-Key": uuid.uuid4().hex},
            json={
                "customer_id": book["customer"]["id"],
                "policy_id": book["policy"]["id"],
                "loss_type": "collision",
                "incident_date": "2026-10-08",
                "is_synthetic": True,
                "description": person.narrative,
                "claimed_amount": {"amount": "1475.00", "currency": "GBP"},
            },
        )
        record["claim_id"] = claim["id"]
        files = documents_for(profile, seed=index + 1, person=person)
        record["documents"] = len(files)
        record["upload_bytes"] = sum(len(f[1][1]) for f in files)
        t_upload = time.time()
        client.ok("POST", f"/claims/{claim['id']}/documents", 201, files=files)
        record["upload_s"] = time.time() - t_upload
        docs = _wait_documents(client, claim["id"], len(files), timeout)
        record["documents_s"] = time.time() - t_upload
        record["document_statuses"] = sorted(d["processing_status"] for d in docs)

        started = client.ok(
            "POST", f"/claims/{claim['id']}/processing", 202, headers={"Idempotency-Key": uuid.uuid4().hex}
        )
        record["sse_lag_ms"] = _follow(client, started["id"], timeout)
        ex = client.ok("GET", f"/workflow-executions/{started['id']}")
        events = ex["events"]
        stop = next((e for e in reversed(events) if e["type"] in STOP_EVENTS), None)
        if stop is None:
            raise RuntimeError(f"execution {started['id']} has no stopping event (status {ex['status']})")
        created = _ts(ex["created_at"])
        record["process_s"] = _ts(stop["occurred_at"]) - created
        record["end_to_end_s"] = _ts(stop["occurred_at"]) - t_upload
        record["stop_event"] = stop["type"]
        record["execution_status"] = ex["status"]
        record["recommendation"] = (ex.get("interrupt") or {}).get("recommendation")
        began: dict[str, float] = {}
        nodes: dict[str, float] = {}
        for e in events:
            if e["type"] == "node_started":
                began.setdefault(e["node"], _ts(e["occurred_at"]))
            elif e["type"] == "node_completed" and e["node"] in began and e["node"] not in nodes:
                nodes[e["node"]] = _ts(e["occurred_at"]) - began[e["node"]]
        record["node_s"] = nodes
        runs = client.ok("GET", f"/claims/{claim['id']}/agent-runs")
        record["agents"] = [
            {
                "agent": r["agent_key"],
                "duration_ms": r["duration_ms"],
                "used_llm": r["used_llm"],
                "llm_note": r["llm_note"],
                "model": r["model_slug"],
                "tokens": r["prompt_tokens"] + r["completion_tokens"],
                "cost_usd": float(r["cost_usd"]),
            }
            for r in runs
        ]
        record["llm_calls"] = sum(1 for r in runs if r["used_llm"])
        record["ok"] = ex["status"] != "failed"
    except Exception as exc:  # recorded as a failed sample, never dropped
        record["error"] = f"{type(exc).__name__}: {exc}"[:500]
    return record


def aggregate(records: list[dict[str, Any]], warmup: int) -> dict[str, Any]:
    measured = [r for r in records if r["index"] >= warmup]
    good = [r for r in measured if r["ok"]]
    nodes: dict[str, list[float]] = defaultdict(list)
    for r in good:
        for node, seconds in r.get("node_s", {}).items():
            nodes[node].append(seconds)
    process = [r["process_s"] for r in good]
    return {
        "claims": len(measured),
        "succeeded": len(good),
        "failure_rate": (len(measured) - len(good)) / len(measured) if measured else None,
        "stop_events": {
            s: sum(1 for r in good if r["stop_event"] == s) for s in sorted({r["stop_event"] for r in good})
        },
        "process_s": summary(process),
        "documents_s": summary([r["documents_s"] for r in good]),
        "end_to_end_s": summary([r["end_to_end_s"] for r in good]),
        "upload_s": summary([r["upload_s"] for r in good]),
        "sse_lag_ms": summary([lag for r in good for lag in r["sse_lag_ms"]]),
        "nodes_s": {node: summary(v) for node, v in sorted(nodes.items())},
        "llm_calls_per_claim": summary([float(r["llm_calls"]) for r in good]),
        "cost_usd_per_claim": summary([sum(a["cost_usd"] for a in r["agents"]) for r in good]),
        "target": {
            "p50_le_180s": meets(process, 0.50, TARGET_P50_S),
            "p90_le_240s": meets(process, 0.90, TARGET_P90_S),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://nginx")
    parser.add_argument("--profile", choices=["simple", "scanned", "poor"], default="simple")
    parser.add_argument("--claims", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=1, help="leading claims excluded from the statistics")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--label", default="")
    parser.add_argument(
        "--email", default=None, help="sign in to an existing benchmark organisation instead of creating one"
    )
    parser.add_argument("--out", default="-")
    args = parser.parse_args(argv)

    admin = BenchClient(args.base_url)
    if args.email:  # an organisation whose agents have models activated (needed for the live-model tier)
        admin.login(args.email)
        me = {"email": args.email}
    else:
        me = admin.register(f"Benchmark {args.profile} c{args.concurrency} {uuid.uuid4().hex[:6]}")
    product = setup_product(admin)
    run = uuid.uuid4().hex[:8]
    ai = admin.ok("GET", "/ai/status")
    workers = [admin] + [BenchClient(args.base_url) for _ in range(args.concurrency - 1)]
    for w in workers[1:]:
        w.login(me["email"])

    started = time.time()
    records: list[dict[str, Any]] = []
    if args.warmup:
        records += [run_claim(admin, product, run, args.profile, i, args.timeout) for i in range(args.warmup)]
    queue = list(range(args.warmup, args.warmup + args.claims))
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(run_claim, workers[n % len(workers)], product, run, args.profile, i, args.timeout)
            for n, i in enumerate(queue)
        ]
        records += [f.result() for f in futures]
    wall = time.time() - started

    result = {
        "label": args.label,
        "profile": args.profile,
        "concurrency": args.concurrency,
        "warmup": args.warmup,
        "started_at": datetime.fromtimestamp(started, UTC).isoformat(),
        "wall_s": wall,
        "throughput_claims_per_min": 60 * args.claims / wall if wall else None,
        "edge_throttled_requests": sum(w.throttled for w in workers),
        "ai": {k: ai.get(k) for k in ("provider", "key_status", "key_detail", "default_model")},
        "admin_email": me["email"],  # synthetic benchmark account, reused by benchmarks.api_load
        "summary": aggregate(records, args.warmup),
        "records": records,
    }
    text = json.dumps(result, indent=2, default=str)
    if args.out == "-":
        print(text)
    else:
        Path(args.out).write_text(text, encoding="utf-8")
        s = result["summary"]
        print(
            f"{args.profile} c={args.concurrency}: {s['succeeded']}/{s['claims']} ok, process P50 "
            f"{s['process_s']['p50']} P90 {s['process_s']['p90']} s -> {args.out}",
            file=sys.stderr,
        )
    for w in workers:
        w.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
