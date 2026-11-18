# ClaimOS

Multi-agent insurance claims automation platform (LangGraph · FastAPI · Next.js · OpenRouter).

**Current status: Phase 14 (performance) complete: all planned phases are built.**
Phase 14 adds a benchmark harness (`backend/benchmarks`, `scripts/run_benchmarks.sh`) and a
[performance report](docs/performance/phase14-report.md). It found and fixed a deadlock between parallel
workflow branches. The platform core processes a simple claim in about 1–2 s (P90 1.65 s at 8 concurrent
claims), SSE events arrive within 30 ms, and read APIs meet P95 < 300 ms when scaled to 3 replicas. The
4-minute target is **not yet verified with live models**: the development OpenRouter key is rejected, so no
model calls were measured.

Phase 13 added:
- two-factor authentication (TOTP with recovery codes), with an organisation policy that can require it per role;
- nightly data retention and audit-chain verification that alerts on tampering;
- secrets read from files (Docker/Kubernetes secrets);
- Prometheus metrics, alert rules and a Grafana dashboard;
- a production compose file and Kubernetes manifests;
- backup and restore-drill scripts with a recorded drill;
- a security scan pass and checklist. See [production readiness](docs/architecture/14-production-readiness.md).

Phase 12 added:
- similar-claim search with pgvector;
- portfolio anomalies (amount outliers, frequent claimants, near-identical narratives across customers,
  shared files);
- a feedback loop that labels agent outputs from people's decisions and ratings;
- evaluation runs and model comparison;
- a gate under which no prompt version or model change is activated without a passing regression run.

Phase 11 added versioned, immutable claim reports in PDF and JSON whose statements cite an evidence index
(document pages, fields, clauses, indicators, line items). It also adds an in-app notification centre with
email over SMTP (Mailpit in development) and an SMS adapter, and outbound webhooks signed with HMAC-SHA256,
stored-encrypted secrets, retries and an SSRF guard.

Phase 10 added the operational dashboard. It shows 14 KPIs, live activity and exceptions, claim and decision
charts, and analytics for processing-time percentiles, agent and model monitoring, fraud (including a
labelled false-positive rate) and extraction accuracy, plus an assessments register. Every figure is one
query over the claims you can see, shown with its definition, and each one has a query test.

Phase 9 added the human review queue and workspace. Reviewers can approve, reject, request information,
return for reassessment and escalate. Decision authority is enforced per user, product and currency, the
assigned adjuster cannot decide their own claim, and four-eyes approval with recorded override reasons
applies above the threshold, for high fraud risk and for overrides.

Phase 8 added versioned product rule sets (depreciation, sub-limits, fraud weights, tolerances, STP limits),
a configuration screen, and straight-through approval behind 12 re-verified gates with an append-only decision.

Phase 7 runs the agents as a checkpointed LangGraph workflow: parallel policy and fraud branches,
interrupts for document review, information requests and human review, resume, retry, cancel, crash
recovery from checkpoints, and live progress over server-sent events.

Phase 6 adds the eight agents (intake, document intelligence, policy verification, fraud, assessment,
validation, decision recommendation, compliance) with deterministic cores, grounded and escalation-only
model steps, versioned prompts, append-only agent runs and labelled evaluation sets that gate each agent.

Phase 5 added the OpenRouter model gateway (verified capabilities, data-policy gates, retries, JSON repair,
fallbacks, throttling, budgets, usage tracking).

Claims, versioned policies and intake (Phase 3) plus secure document upload, ClamAV scanning,
text-layer and Tesseract OCR processing, rule-based classification and grounded field extraction,
duplicate detection and audited human corrections.

## Quick start

```bash
python scripts/dev_setup.py
docker compose up -d --build --wait
```

Open <http://localhost:8080>. To load a realistic synthetic book through the real API, run
`python scripts/seed_synthetic.py` (sign-in details are written to the git-ignored `.seed-credentials.json`). See the [local development runbook](docs/runbooks/local-development.md) for tests.

## Documentation

- [Architecture overview & risks](docs/architecture/00-overview.md)
- [Database schema & ERD](docs/architecture/01-database-schema.md)
- [Claim state machine & LangGraph workflow](docs/architecture/02-workflow.md)
- [Agent contracts, rules engine, fraud scoring](docs/architecture/03-agent-contracts.md)
- [Security model](docs/architecture/04-security-model.md)
- [API specification](docs/architecture/05-api-specification.md)
- [Performance targets, roadmap, structure, dependencies](docs/architecture/06-roadmap-and-dependencies.md)
- [Performance report (Phase 14)](docs/performance/phase14-report.md)
- [Production readiness (Phase 13)](docs/architecture/14-production-readiness.md) · [deployment](docs/runbooks/production-deployment.md) · [backup & restore](docs/runbooks/backup-restore.md) · [security checklist](docs/security/checklist.md)
- [Advanced intelligence (Phase 12)](docs/architecture/13-intelligence.md)
- [Reports, notifications and webhooks (Phase 11)](docs/architecture/12-reports-notifications.md)
- [Dashboard and analytics (Phase 10)](docs/architecture/11-dashboard.md)
- [Human review (Phase 9)](docs/architecture/10-human-review.md)
- [Rule sets and STP (Phase 8)](docs/architecture/09-rules-and-stp.md)
- [Orchestration (Phase 7)](docs/architecture/08-orchestration.md)
- [Agents (Phase 6) and evaluation report](docs/architecture/07-agents.md) · [results](docs/evaluations/phase6-report.md)
- [Architecture decision records](docs/adr/README.md)
"# insuranceAi" 
