# 06 — Performance Targets, Roadmap, Structure & Dependencies

## 1. Performance targets

Measured from `POST /process` accepted to the execution reaching `HUMAN_REVIEW` / `DECISION_PENDING` (or
`APPROVED` under STP). Reference workload "simple claim": ≤ 5 documents, ≤ 20 pages total, ≥ 50% born-digital.

| Stage | Budget (P50) | Hard timeout | Technique |
|-------|-------------|--------------|-----------|
| Intake | 2 s | 15 s | deterministic; LLM categorisation optional/async |
| Document processing (OCR) | 45 s | 180 s | starts at upload; text-layer first; page-parallel OCR; content-hash cache |
| Document intelligence (LLM extraction) | 40 s | 120 s/doc | docs in parallel (bounded by model rate limit); one call per doc with page batching |
| Document validation | 1 s | 10 s | deterministic |
| Policy verification ‖ Fraud stage 1 | 20 s | 90 s | parallel branches |
| Assessment + fraud stage 2 | 20 s | 90 s | one LLM normalisation call; Decimal engine |
| Cross-validation + rules | 2 s | 15 s | deterministic |
| Decision explanation | 15 s | 60 s | single `reasoning` call; template fallback |
| Compliance | 1 s | 10 s | deterministic |
| **Total** | **≈ 146 s** | 15 min watchdog | headroom for retries |

Targets to verify in Phase 14: **simple claims P50 ≤ 180 s, P90 ≤ 240 s**; complex / poor-quality claims are
reported separately and are expected to escalate rather than meet the target. Benchmarks are reported per model
tier (free vs paid) because free-tier rate limits dominate latency.

Tracked metrics (OpenTelemetry → Prometheus; persisted in `agent_runs`/`workflow_executions`): time to first
result, OCR time/page, per-agent latency, DB query time, total processing time, failure rate, retry rate, human
intervention rate, tokens and cost per claim.

## 2. Implementation roadmap

Each phase ends with: tests passing, completion report (works / limitations), and user approval.

| Phase | Deliverable | Exit criteria (abridged) |
|-------|-------------|--------------------------|
| **0** Architecture | these documents, ADRs, skeleton | internally consistent; approved |
| **1** Foundation | compose (postgres+pgvector, redis, minio, api, workers, frontend, nginx), settings, structlog, error handler, health checks, Alembic baseline, Next.js shell + design tokens + shadcn/ui, CI lint/type/test | `docker compose up` → UI shows live `/health/ready`; CI green |
| **2** Auth & orgs | register/login/logout, sessions, CSRF, RBAC catalogue, users/roles admin, RLS, audit base, login protection | cross-tenant + role-escalation test suite green |
| **3** Claims | products (basic), customers, policies + versions (manual/JSON import), claims CRUD, assignment, status machine, notes, timeline, search/filters, seed synthetic data | API + Playwright create/list/filter flows |
| **4** Documents | upload pipeline, AV adapter, storage, page rendering, text layer, PaddleOCR + Tesseract, quality scoring, hashing/duplicates, viewer, field viewer, manual corrections (extraction initially rules/regex-based, LLM in Phase 6) | multi-format + degraded-scan fixtures pass; corrections audited |
| **5** Model gateway | OpenRouter client, registry, capability probes, structured output validation/repair, retries/backoff, fallbacks, throttling, budgets, usage tracking, mock provider, admin model registry UI | probes run against real free models; results recorded; mock-based tests deterministic |
| **6** Agents | 8 agents individually, prompts, eval sets (03 §7) | per-agent acceptance thresholds met |
| **7** Orchestration | LangGraph graph, state, parallel branches, checkpointer, interrupts/resume, retry, cancel, crash recovery, workflow events + SSE, live processing UI | end-to-end synthetic claims; kill-worker recovery test |
| **8** Rules engine | full rule-set versioning, calculators, gates, STP, configurable products UI | ≥ 95% branch coverage; property-based money tests |
| **9** Human review | queue, workspace, all actions, authority, four-eyes, override reasons | every transition tested incl. denied paths |
| **10** Dashboard | KPIs, charts, agent monitoring, exceptions, system health | every metric traced to a query test |
| **11** Reports & notifications | PDF (WeasyPrint) + JSON, versioning, email/SMS adapters, in-app centre, webhooks | report contains evidence refs; signature verification test |
| **12** Advanced intelligence | pgvector similar claims, advanced anomalies, feedback loop, eval pipelines, model comparison, prompt regression gate | no prompt/model activation without passing regression |
| **13** Production readiness | MFA, security testing (ZAP, dependency audit), backup/restore drill, retention jobs, audit-chain verification, secrets manager, prod compose/k8s manifests, monitoring dashboards | documented restore; security checklist signed |
| **14** Performance | benchmark harness, concurrency tests, report with median/P90/P95 | evidence-backed statement on the 4-minute target |

## 3. Project structure

```
claimos/
├── frontend/                      Next.js (App Router, strict TS)
│   ├── app/                       routes: (auth)/, (app)/overview, claims, claims/[id], new-claim, processing,
│   │                              documents, fraud, policies, assessments, reviews, reports, analytics,
│   │                              audit, agents, integrations, settings
│   ├── components/                ui/ (shadcn), layout/, data-display/, charts/
│   ├── features/                  claims/, documents/, processing/, reviews/, ... (feature hooks + components)
│   ├── hooks/  lib/  services/    api client (generated types), query keys, SSE client
│   ├── types/                     generated OpenAPI types
│   └── tests/                     vitest unit/component; e2e/ (Playwright)
├── backend/
│   ├── pyproject.toml  uv.lock  alembic.ini
│   ├── app/
│   │   ├── api/v1/                routers per resource
│   │   ├── core/                  settings, logging, errors, telemetry, tenancy context
│   │   ├── models/                SQLAlchemy models
│   │   ├── schemas/               Pydantic API schemas
│   │   ├── repositories/          tenant-scoped data access
│   │   ├── services/              business services (claims, documents, reviews, audit, …)
│   │   ├── middleware/            request id, security headers, rate limit, CSRF
│   │   ├── security/              sessions, passwords, permissions, access policies, crypto
│   │   ├── integrations/          storage, av, email, sms, pas, crm, payment, fraud_data, webhooks
│   │   └── main.py
│   ├── migrations/                Alembic versions
│   ├── agents/                    intake/ document/ policy/ fraud/ assessment/ validation/ decision/
│   │                              compliance/ supervisor/  + base.py, tools/
│   ├── workflows/                 claim_graph.py, state.py, nodes/, events.py, checkpointing.py
│   ├── rules_engine/              money.py, policy_validity.py, coverage.py, calculation/, gates.py, fraud_scoring.py
│   ├── document_processing/       detect.py, render.py, text_layer.py, ocr/ (paddle, tesseract), quality.py, hashing.py
│   ├── model_gateway/             client.py, registry.py, capabilities.py, routing.py, budgets.py, mock.py
│   ├── prompts/                   <agent>/v<N>.md (version-controlled)
│   ├── tasks/                     celery_app.py, workflow.py, documents.py, reports.py, beat.py
│   ├── evaluations/               datasets/ (synthetic), runners/, reports/
│   └── tests/                     unit/ integration/ security/ agents/ workflow/ rules/ fixtures/
├── infrastructure/                docker/ (Dockerfiles), nginx/, postgres/ (init, roles, rls), monitoring/
├── docs/                          architecture/, adr/, runbooks/ (later)
├── scripts/                       dev helpers (seed, reset, probe models, benchmark)
├── docker-compose.yml             + profiles: av, observability, langfuse
├── .env.example
└── README.md
```

## 4. External dependencies

### 4.1 Services

| Service | Version | Required | Notes |
|---------|---------|----------|-------|
| PostgreSQL + pgvector | 17, `pgvector/pgvector:0.8.7-pg17-trixie` | yes | |
| Redis | 8.6 (`redis:8.6.8-alpine`) | yes | broker, pub/sub, rate limits, cache (non-sensitive) |
| SeaweedFS (S3-compatible) | 4.50 | yes (dev) | MinIO no longer publishes container images (checked 2026-11-04); AWS S3 / other S3 in production |
| Nginx | 1.30 | yes | TLS, proxy, limits |
| ClamAV (clamd) | 1.4 | prod: yes, dev: optional profile | |
| OpenRouter | API v1 | yes for live AI; mock provider for tests | requires `OPENROUTER_API_KEY` |
| Langfuse | v3 | optional | self-host profile or cloud; off for real data unless approved |
| OpenTelemetry Collector + Prometheus/Grafana | current | optional (Phase 10/13) | |
| SMTP / SMS / payment / PAS / CRM / fraud data | — | adapters only | configured per org via `integration_connections` |

### 4.2 Backend (Python 3.12)

fastapi, uvicorn[standard], pydantic v2, pydantic-settings, sqlalchemy 2 (asyncio), asyncpg, psycopg[binary]
(checkpointer), alembic, redis, celery, langgraph, langgraph-checkpoint-postgres, langchain-core (message types
only where useful), openai (OpenAI-compatible client), httpx, tenacity, argon2-cffi, cryptography, itsdangerous,
structlog, opentelemetry-sdk + instrumentations, langfuse (optional), pymupdf, paddleocr + paddlepaddle (CPU),
pytesseract, pillow, pillow-heif, opencv-python-headless, python-docx, openpyxl, pandas, numpy, rapidfuzz,
imagehash, python-magic/filetype, boto3 (S3), weasyprint + jinja2 (reports), sse-starlette.
Dev/test: pytest, pytest-asyncio, pytest-cov, hypothesis, httpx (test client), testcontainers, factory-boy,
ruff, mypy (strict), bandit, pip-audit.

### 4.3 Frontend (Node 24 LTS, pnpm)

next, react, react-dom, typescript (strict), tailwindcss v4, shadcn/ui (radix primitives), class-variance-authority,
tailwind-merge, lucide-react, framer-motion, gsap (limited use), recharts, @tanstack/react-query,
@tanstack/react-table, react-hook-form, zod, @hookform/resolvers, react-pdf / pdfjs-dist (viewer), date-fns,
openapi-typescript + openapi-fetch. Dev/test: vitest, @testing-library/react, playwright, eslint, prettier.

### 4.4 Phase 4 implementation choices (2026-11-06)

* **PDF text and rendering: pypdfium2** (Apache-2.0/BSD) instead of PyMuPDF, avoiding AGPL obligations.
* **OCR: Tesseract 5.5** in the backend image (default). PaddleOCR is implemented behind the same engine
  interface but its packages are not installed in the default image (several hundred MB); it is not yet verified.
* **Image pre-processing: Pillow + NumPy** (greyscale, upscaling, autocontrast, median filter, Tesseract
  orientation detection). OpenCV was not needed so far.
* **Malware scanning: ClamAV 1.4 (clamd INSTREAM)** runs in the development stack; `AV_PROVIDER=disabled_dev`
  remains available, is reported as a development adapter and is refused in production.

### 4.5 Licensing notes

PyMuPDF is **AGPL-3.0** (commercial licence available from Artifex). If ClaimOS is distributed or offered as a
network service under non-AGPL terms, a commercial licence is required, or the text-layer/rendering step must be
swapped (e.g. `pypdfium2`, Apache/BSD). Decision needed before Phase 4. PaddleOCR (Apache-2.0), Tesseract
(Apache-2.0), WeasyPrint (BSD), LangGraph (MIT) are permissive.

## 5. Configuration (`.env.example`, created in Phase 1)

```
APP_ENV=development            # development | test | production
APP_BASE_URL=http://localhost
DATABASE_URL=postgresql+asyncpg://claimos_app:...@postgres:5432/claimos
REDIS_URL=redis://redis:6379/0
S3_ENDPOINT_URL=http://minio:9000  S3_BUCKET_DOCUMENTS=claimos-documents  S3_BUCKET_REPORTS=claimos-reports
SESSION_IDLE_MINUTES=30  SESSION_ABSOLUTE_HOURS=12
ENCRYPTION_MASTER_KEY_REF=     # secret-manager reference; dev: generated local key file
AV_PROVIDER=clamav|disabled_dev
OPENROUTER_API_KEY=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_DEFAULT_MODEL=openrouter/free
OPENROUTER_VISION_MODEL=
OPENROUTER_REASONING_MODEL=
OPENROUTER_FAST_MODEL=
AI_PROVIDER=openrouter|mock
LANGFUSE_ENABLED=false  LANGFUSE_HOST=  LANGFUSE_PUBLIC_KEY=  LANGFUSE_SECRET_KEY=
OTEL_EXPORTER_OTLP_ENDPOINT=
```

## 6. Decisions requested from you before Phase 1

1. **Approve** the architecture, ADR-001…013, and the first-release product scope (motor own-damage + health).
2. **PyMuPDF licence** (AGPL) — accept for now, or substitute `pypdfium2` from the start?
3. **STP default** — keep straight-through approval disabled by default (proposed), enabled per organisation?
4. **Git** — initialise a repository in this folder (and a remote?) before Phase 1.
5. **Docker Desktop** must be running for Phase 1 verification (daemon currently not reachable).
