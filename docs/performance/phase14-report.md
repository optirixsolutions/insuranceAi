# Phase 14 performance report

Measured on 2026-11-18 against the running development stack with synthetic data only. Raw results (one
JSON file per run, every claim recorded) are in `backend/benchmarks/results/`; reproduce with
`scripts/run_benchmarks.sh full` and render with `python -m benchmarks.report <results dir>`.

## Verdict on the 4-minute target

**Not yet supported by evidence. The "4 minutes per claim" statement is not published.**

| Target (06-roadmap, section 1) | Result | Status |
|---|---|---|
| Simple claim P50 ≤ 180 s, P90 ≤ 240 s, `POST /processing` → decision or review | **deterministic core only**: P50 1.27 s, P90 1.65 s, P95 1.77 s at 8 concurrent claims | met for the platform; **not measured with live models** |
| Read API P95 < 300 ms | 1 API process: 121 ms at 8 clients, 525 ms at 32 clients; 3 processes: **210 ms at 32 clients** | met with horizontal scaling (production runs ≥ 2 replicas) |
| SSE event latency < 1 s | P95 ≤ 30 ms in every run | met |
| Report per model tier (free / paid) | free tier: not measurable (see below); paid tier: not available | **open** |

Why the model tier is missing: the development OpenRouter key is rejected by OpenRouter (`unauthorized: User
not found`), and a new organisation has no model activated for any agent until a passing evaluation run
exists for it (ADR-022). Every agent therefore ran its deterministic core (`used_llm = false`, 0 tokens,
$0.00 per claim). The figures above are the platform's own cost: orchestration, database, documents,
rules, events. They are the floor under any model-assisted run, not a substitute for it.

What the model tier adds, per the stage budgets in 06-roadmap (P50): document intelligence 40 s, policy and
fraud 20 s (parallel), assessment 20 s, decision explanation 15 s, about 95 s in total. With the measured
platform time of about 2–10 s end to end, a run that meets those budgets would come in around 100–110 s.
That is a projection, not a measurement. To close the gap: put a valid key in `.env`, pass the evaluation
gate for each agent in a benchmark organisation, then run
`python -m benchmarks.claims_bench --email <that admin> ...` for the free and for a paid model.

## Environment

Windows 11 host, Docker Desktop with 12 CPUs and 7.7 GB for containers. Single PostgreSQL 17, Redis, one
API process (uvicorn), one workflow worker (concurrency 2), one document worker (concurrency 2), ClamAV,
Tesseract OCR. The harness runs inside the compose network (`docker compose run ... api`), so its clock is
the workers' clock (exact SSE lag) and it can call the API directly as well as through Nginx.

## Workloads

| Profile | Documents | Notes |
|---|---|---|
| simple | claim form, estimate, licence as born-digital PDFs + damage photo (4 documents, 75 % born-digital) | the roadmap's reference workload |
| scanned | claim form and estimate as clean 150 dpi scans, licence PDF, photo | OCR on two pages |
| poor | blurred, noisy and rotated, and faded scans + photo | reported separately; expected to escalate |

Each claim gets its own synthetic policyholder, policy number, registration, estimate number and narrative.
Identical files across claims are, correctly, flagged as possible duplicates and would send every claim to
review. Each run creates a new organisation; the first claim is a warm-up and is not counted.

## Results after the fix

`process` = `POST /processing` accepted → the stopping event (human review, document review or failure),
taken from the server's event timestamps. `documents` = upload start → every document processed (0.5 s
polling). `end-to-end` = upload start → stopping event.

| Profile | Concurrency | Claims ok | Process P50 / P90 / P95 / max (s) | Documents P50 / P90 (s) | End-to-end P50 / P90 / P95 (s) | Claims/min | SSE lag P95 (ms) |
|---|---|---|---|---|---|---|---|
| simple | 1 | 10/10 | 1.10 / 1.22 / 1.22 / 1.22 | 1.61 / 1.64 | 2.66 / 2.85 / 2.85 | 19.3 | 20 |
| simple | 4 | 12/12 | 0.97 / 1.07 / 1.10 / 1.12 | 2.63 / 3.14 | 3.59 / 4.12 / 4.47 | 50.2 | 17 |
| simple | 8 | 16/16 | 1.27 / 1.65 / 1.77 / 2.08 | 7.39 / 8.41 | 8.88 / 9.78 / 9.87 | 41.0 | 30 |
| scanned | 2 | 6/6 | 0.54 / 0.67 / 0.68 / 0.68 | 3.17 / 3.71 | 3.73 / 4.26 / 4.30 | 24.1 | 19 |
| poor | 2 | 4/4 | 0.56 / 0.78 / 0.82 / 0.86 | 5.73 / 10.26 | 6.47 / 10.79 / 11.63 | 10.0 | 21 |

Every simple claim ended at human review with `APPROVE_RECOMMENDED`. Every scanned and poor claim stopped
earlier, at **document review**: OCR read the policyholder name with low confidence, so a person verifies
it before the claim proceeds. That is the intended escalation; their `process` time is time to escalation,
not to a decision. They were also flagged `possible_duplicate`, because all synthetic scans share one page
template; real scans differ more.

Time per workflow node, simple claims (P50 / P95, seconds):

| Node | c1 | c4 | c8 |
|---|---|---|---|
| INTAKE | 0.147 / 0.217 | 0.119 / 0.146 | 0.173 / 0.286 |
| DOCUMENT_PROCESSING | 0.084 / 0.100 | 0.080 / 0.094 | 0.112 / 0.171 |
| DOCUMENT_VALIDATION | 0.012 / 0.020 | 0.011 / 0.012 | 0.013 / 0.022 |
| POLICY_VERIFICATION | 0.122 / 0.142 | 0.103 / 0.144 | 0.137 / 0.197 |
| FRAUD_ANALYSIS | 0.097 / 0.115 | 0.090 / 0.118 | 0.108 / 0.164 |
| CLAIM_ASSESSMENT | 0.074 / 0.090 | 0.059 / 0.064 | 0.085 / 0.108 |
| RESULT_AGGREGATION | 0.024 / 0.030 | 0.021 / 0.027 | 0.026 / 0.042 |
| CROSS_VALIDATION | 0.059 / 0.078 | 0.056 / 0.141 | 0.081 / 0.117 |
| DECISION_RECOMMENDATION | 0.067 / 0.084 | 0.059 / 0.076 | 0.081 / 0.138 |
| COMPLIANCE_CHECK | 0.067 / 0.111 | 0.059 / 0.064 | 0.083 / 0.129 |

## Finding 1: parallel branches deadlocked (fixed)

The baseline run showed `FRAUD_ANALYSIS` taking 2–4 s while its agent took about 30 ms. The worker log
showed PostgreSQL `deadlock detected` between the parallel `POLICY_VERIFICATION` and `FRAUD_ANALYSIS`
branches, after which LangGraph retried the aborted node after its 2 s back-off.

Cause: nodes lock the execution and claim rows with `SELECT ... FOR UPDATE` and then take the
per-execution advisory lock that numbers events. Inserting a row that references those rows (agent runs,
events) takes a `KEY SHARE` lock for the foreign-key check, which `FOR UPDATE` blocks. One branch held the
row lock and waited for the advisory lock; the other held the advisory lock and waited on the
foreign-key check.

Fix: every lock on a `claims` or `workflow_executions` row now uses `FOR NO KEY UPDATE` (11 sites). It
still serialises writers but does not block foreign-key checks, and those keys never change (ADR-028).
`tests/integration/test_lock_ordering_integration.py` shows on the real database that an insert waits
under `FOR UPDATE` and does not under `FOR NO KEY UPDATE`, and fails if a bare `FOR UPDATE` on those tables
returns.

| Simple claims | Before: process P50 / P90 / P95 / max (s) | After | Deadlocks in the run (worker log) |
|---|---|---|---|
| concurrency 1 | 0.84 / 1.12 / 1.13 / 1.14 | 1.10 / 1.22 / 1.22 / 1.22 | before 2 (whole matrix), after 0 |
| concurrency 4 | 1.07 / 1.75 / **3.32 / 5.16** | 0.97 / 1.07 / 1.10 / 1.12 | |
| concurrency 8 | 1.32 / 2.25 / **3.33 / 6.12** | 1.27 / 1.65 / 1.77 / 2.08 | |
| `FRAUD_ANALYSIS` P95 at c4 / c8 | 2.08 / 2.11 | 0.12 / 0.16 | node retries: before 2, after 0 |

The tail dropped by two thirds at 4 and 8 concurrent claims. The single-claim figures differ only by noise;
there is no contention to remove there. With live models each node holds its locks longer, so the retry
would have cost more, not less.

## Finding 2: one API process saturates near 115 requests per second

| API processes | Clients | Requests/s | P50 / P95 / P99 (ms) | Errors |
|---|---|---|---|---|
| 1 | 8 | 104 | 72 / 121 / 162 | 0 |
| 1 | 32 | 116 | 241 / 525 / 733 | 0 |
| 3 | 32 | 286 | 107 / 210 / 306 | 0 |

Throughput is flat between 8 and 32 clients on one process, so the extra latency is queueing in that
process, not database time. Each read runs 9 short statements, 7 of them authentication (session, user,
roles, permissions, organisation). Three replicas gave 2.5 times the throughput and brought P95 under the
target. Production already scales out: `docker-compose.prod.yml` runs 2 API replicas by default and
Kubernetes scales 2 to 10 on CPU. The slowest read is `/dashboard/summary` (P95 289 ms on 3 replicas), which
is the first candidate for caching if reads grow. Through Nginx, the per-client limit of 20 requests/s
answered 22,771 of 23,203 requests from one load-generator address with `429`, as designed; the 432 that
passed had P95 43 ms.

## Finding 3: document processing is the platform's largest share

With 8 concurrent claims (32 documents), documents took 7.4 s at P50 against 1.6 s for a single claim,
because the development document worker runs 2 processes. The pool scales independently (ADR-004:
`worker-documents` in compose, `worker-documents` with 2 replicas in Kubernetes). Under the roadmap
budget of 45 s for OCR this is not a constraint; with live document-intelligence calls it is the first
pool to size.

## Limitations

- No live-model measurement (see the verdict); token usage and cost per claim are $0.00 because no model
  was called.
- One host, one PostgreSQL; no network latency between services. Production numbers need a run on the
  target platform with the same harness.
- Small samples (10–16 claims per configuration); P95 and max of 12 or 16 samples are indicative only.
- Synthetic scans share one template, so the duplicate check flags them; this inflates escalations in the
  scanned and poor profiles.
- The edge rate limiter was measured from a single address only.
