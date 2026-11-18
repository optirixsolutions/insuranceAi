"""Read-API latency under concurrent load (Phase 14 target: P95 < 300 ms for reads, excluding exports).

    python -m benchmarks.api_load --base-url http://api:8000 --concurrency 16 --seconds 60

Each worker signs in as the same synthetic organisation's admin and loops over the read endpoints the UI
uses most, recording the server round trip per request. ``--base-url http://api:8000`` measures the API
itself (inside the compose network); ``http://nginx`` measures through the edge, whose per-client rate
limit (20 r/s) returns 429s that are counted separately, not as latency.
Run ``benchmarks.claims_bench`` first (or pass ``--email``) so lists contain real synthetic rows.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from benchmarks.client import BenchClient
from benchmarks.stats import summary

TARGET_P95_MS = 300.0


def endpoints(client: BenchClient) -> list[str]:
    """The UI's common reads; one claim's detail pages are included when the organisation has claims."""
    paths = [
        "/auth/me",
        "/claims?page_size=25",
        "/dashboard/summary",
        "/dashboard/activity",
        "/notifications",
        "/reviews?status=open",
        "/analytics/processing-times",
    ]
    claims = client.ok("GET", "/claims?page_size=1").get("items", [])
    if claims:
        cid = claims[0]["id"]
        paths += [
            f"/claims/{cid}",
            f"/claims/{cid}/documents",
            f"/claims/{cid}/timeline",
            f"/claims/{cid}/agent-runs",
        ]
    return paths


def label(path: str) -> str:
    """Group detail paths by template so results do not depend on ids."""
    parts = path.split("?")[0].split("/")
    return "/".join("{id}" if len(p) == 36 and p.count("-") == 4 else p for p in parts)


def worker(
    client: BenchClient,
    paths: list[str],
    until: float,
    out: dict[str, list[float]],
    errors: dict[str, int],
    lock: threading.Lock,
) -> int:
    count, i = 0, 0
    local: dict[str, list[float]] = defaultdict(list)
    local_errors: dict[str, int] = defaultdict(int)
    while time.monotonic() < until:
        path = paths[i % len(paths)]
        i += 1
        started = time.perf_counter()
        response = client.http.get(path, headers={"Cookie": client.cookie_header()})
        elapsed = (time.perf_counter() - started) * 1000
        key = label(path)
        if response.status_code == 200:
            local[key].append(elapsed)
        else:
            local_errors[f"{key} {response.status_code}"] += 1
        count += 1
    with lock:
        for k, v in local.items():
            out[k].extend(v)
        for k, n in local_errors.items():
            errors[k] = errors.get(k, 0) + n
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://api:8000")
    parser.add_argument(
        "--email", default=None, help="existing benchmark admin (password from benchmarks.client)"
    )
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--label", default="")
    parser.add_argument("--out", default="-")
    args = parser.parse_args(argv)

    first = BenchClient(args.base_url, timeout=30)
    if args.email:
        first.login(args.email)
        email = args.email
    else:
        email = first.register("Benchmark API load")["email"]
    clients = [first] + [BenchClient(args.base_url, timeout=30) for _ in range(args.concurrency - 1)]
    for c in clients[1:]:
        c.login(email)
    paths = endpoints(first)
    for p in paths:  # warm caches and connections; not measured
        first.http.get(p, headers={"Cookie": first.cookie_header()})

    samples: dict[str, list[float]] = defaultdict(list)
    errors: dict[str, int] = {}
    lock = threading.Lock()
    until = time.monotonic() + args.seconds
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        total = sum(pool.map(lambda c: worker(c, paths, until, samples, errors, lock), clients))
    wall = time.monotonic() - started

    every = [v for values in samples.values() for v in values]
    per_endpoint = {k: summary(v) for k, v in sorted(samples.items())}
    result = {
        "label": args.label,
        "base_url": args.base_url,
        "concurrency": args.concurrency,
        "seconds": wall,
        "requests": total,
        "throughput_rps": total / wall if wall else None,
        "errors": errors,
        "all_ms": summary(every),
        "endpoints_ms": per_endpoint,
        "target": {
            "overall_p95_lt_300ms": (summary(every)["p95"] or 0) < TARGET_P95_MS if every else None,
            "endpoints_over_target": [k for k, s in per_endpoint.items() if (s["p95"] or 0) >= TARGET_P95_MS],
        },
    }
    text = json.dumps(result, indent=2)
    if args.out == "-":
        print(text)
    else:
        Path(args.out).write_text(text, encoding="utf-8")
        print(
            f"api c={args.concurrency}: {total} requests, {result['throughput_rps']:.0f} r/s, "
            f"P95 {result['all_ms']['p95']:.0f} ms, errors {sum(errors.values())} -> {args.out}",
            file=sys.stderr,
        )
    for c in clients:
        c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
