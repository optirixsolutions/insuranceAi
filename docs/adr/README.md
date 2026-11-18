# Architecture Decision Records

Format: context → decision → consequences. Status values: `proposed`, `accepted`, `superseded`.
All records below are **proposed** pending Phase 0 approval.

---

## ADR-001 Deterministic supervisor, not an LLM supervisor
**Context.** The Supervisor must pick agents, parallelise, retry and route exceptions without infinite loops.
**Decision.** The supervisor is LangGraph conditional edges plus a deterministic `supervisor` module
(routing policy, output validation, retry/escalation rules). No LLM chooses the next node.
**Consequences.** Predictable, testable routing; bounded loops; LLM cost limited to perception/explanation.

## ADR-002 LLMs propose, deterministic code decides
**Decision.** Coverage status, financial amounts, fraud score, decision gating and approval eligibility are
computed by the rules engine. The Decision agent's LLM step writes the explanation and may only *escalate*.
**Consequences.** Auditable, reproducible outcomes. LLM failure degrades to `MANUAL_REVIEW_REQUIRED`, never to an
unsafe decision.

## ADR-003 Structured policy versions are the source of truth
**Decision.** Coverage terms come from `policy_versions` (+ coverages, exclusions, limits, clauses) selected by
`effective_from <= incident_date < effective_to`. Uploaded policy PDFs are evidence only.
**Consequences.** Requires policy data entry/import (UI, JSON import, PAS adapter). Prevents document-defined coverage.

## ADR-004 Run LangGraph inside a Celery "workflow" queue with a Postgres checkpointer
**Decision.** API enqueues `run_workflow(execution_id)`; the orchestrator worker runs the graph with
`langgraph-checkpoint-postgres`. Human review uses `interrupt()`; resume enqueues `resume_workflow(execution_id,
payload)`. OCR runs in a separate `documents` queue/pool and starts at upload time.
**Consequences.** Graph survives worker restarts; no worker waits on its own queue; resume is idempotent.
Alternative considered: LangGraph Platform/Server — rejected for self-hosting simplicity and licensing control.

## ADR-005 Server-Sent Events for live processing
**Decision.** Workflow nodes append to `workflow_events` (transactional outbox) and publish to Redis channel
`claim:{org}:{claim}`. `GET /claims/{id}/processing/stream` is SSE with `Last-Event-ID` replay from the table.
**Consequences.** One-way, cookie-authenticated, proxy-friendly; events are durable and replayable.
WebSockets not needed (no client→server streaming).

## ADR-006 Opaque server-side sessions in HttpOnly cookies (not JWT)
**Decision.** 256-bit random session token, SHA-256 hash stored in `user_sessions`; cookie `__Host-claimos_session`
(`HttpOnly; Secure; SameSite=Lax; Path=/`). CSRF double-submit token for unsafe methods.
**Consequences.** Instant revocation, idle/absolute expiry, no token-in-JS. Requires session lookup per request
(cached in Redis for 60 s, keyed by token hash).

## ADR-007 Tenant isolation: repository scoping + composite FKs + Row-Level Security
**Decision.** Every tenant table has `organization_id`; repositories require a `TenantContext`; child tables use
composite FK `(organization_id, parent_id)`; RLS policies use `current_setting('app.org_id')` set per transaction.
**Consequences.** A missed filter in code is still blocked by the database. Platform super-admin operations use a
separate DB role with `BYPASSRLS`, only in audited admin services.

## ADR-008 Money as Decimal with explicit currency and one rounding point
**Decision.** `Money(amount: Decimal, currency: str)`; DB `NUMERIC(19,4)` + `CHAR(3)`; intermediate results
unrounded; final payout rounded once to the currency exponent with `ROUND_HALF_EVEN` (configurable per product).
Cross-currency arithmetic raises.
**Consequences.** Exact, reproducible calculations with a stored calculation trace.

## ADR-009 Document processing is LLM-free up to text/layout
**Decision.** Format sniffing, rendering, text-layer extraction, OCR, hashing and quality scoring are pure
Python/CV. LLMs receive page text (and page images only if the assigned model has verified vision capability and
the data policy allows it).
**Consequences.** OCR output is reproducible evidence; grounding checks have a stable reference text.

## ADR-010 Model access through a single gateway with a registry
**Decision.** All LLM calls go through `model_gateway` (OpenAI-compatible client against OpenRouter). Agents request
a *role* (`fast`, `reasoning`, `vision`) and the gateway resolves the per-agent assignment from `agent_configs`
→ `model_registry`, enforcing capability, data policy, budget and rate limits.
**Consequences.** Admins change models without code; tests use the deterministic mock provider.

## ADR-011 pgvector deferred to Phase 12
**Decision.** Install the extension in Phase 1, but v1 policy verification passes the *complete* clause set of the
applicable policy version section (small, bounded) rather than semantic retrieval. Vector search is used for
similar-claim retrieval in Phase 12.
**Consequences.** Deterministic, auditable clause context; removes an embedding-provider dependency from the critical path.

## ADR-012 Documents in object storage, metadata in PostgreSQL
**Decision.** Objects keyed `org/{org_id}/claims/{claim_id}/docs/{document_id}/v{n}/{sha256}`; SSE-S3/KMS at rest;
downloads streamed through the API (authorisation + audit) rather than long-lived presigned URLs.
**Consequences.** No binary blobs in the DB; every access is authorised and logged.

## ADR-013 Append-only, hash-chained audit log
**Decision.** `audit_events` is insert-only (no UPDATE/DELETE grants + trigger guard) and each row stores
`prev_hash`/`hash` per organisation chain.
**Consequences.** Tamper-evident history; verification job in Phase 13.

## ADR-014 Models may only add grounded evidence or escalate
**Decision.** Every agent has a deterministic core that produces its result without a model. A model step
may only (a) propose values that are then grounded in stored page text and normalised by code, or (b) make
an outcome more cautious (covered → potentially covered, approve/reject → manual review), always citing
evidence. Amounts are never taken from a model. Answers that fail these checks are repaired, retried on the
next model, then dropped in favour of the deterministic result, with the reason recorded.
**Consequences.** Prompt injection and hallucination cannot raise a payout, clear an exclusion or upgrade a
recommendation; the evaluation sets gate this (injection: 0 instruction-following). Model quality affects
completeness and explanations, not safety.

## ADR-015 Immutable prompt versions and append-only agent runs
**Decision.** Prompts are source-controlled files mirrored into `prompt_versions`; content is immutable per
version and a drifted file stops the agent. `agent_runs` are finalised exactly once and never deleted;
`agent_outputs` are append-only and re-validated against the strict schema before storage.
**Consequences.** Every recommendation can be traced to the exact prompt, rule set, model and input hash
that produced it.

## ADR-016 Checkpointed executions with an event outbox and heartbeat recovery
**Decision.** One LangGraph thread per execution, checkpointed in PostgreSQL (`AsyncPostgresSaver`).
Workflow events are written to an append-only `workflow_events` outbox with a gap-free sequence and only
then published to Redis; the SSE endpoint treats the table as the source of truth. Workers heartbeat; a beat
task re-enqueues stale executions and LangGraph replays from the last checkpoint. Each Celery delivery carries
the execution's current task id and is ignored otherwise.
**Consequences.** A killed worker loses at most the node in flight (agent runs are re-run as a new attempt;
the orphan is marked WORKER_LOST); the UI never misses a step; interrupts hold no worker while people decide.

## ADR-017 Configuration changes numbers, code owns the order of operations
**Decision.** Product rule sets are strictly validated JSON (`RuleSetConfig`) versioned per product and immutable
once published; calculators, gates and the decision matrix stay in code and only read the numbers. STP is a
pure gate re-evaluated from persisted data and recorded with every check in an append-only decision.
**Consequences.** A product owner can tune depreciation, sub-limits, weights and STP limits without a release,
but cannot create an unreviewable calculation; every payout is reproducible from its rule-set version.

## ADR-018 People decide; authority is checked from stored facts at decision time
**Decision.** Approvals and rejections are taken only through the review API. Each one is checked by pure
rules (`rules_engine/review.py`) against the reviewer's stored authorities, the claim's assigned adjuster, the
persisted recommendation, payout and fraud level, and the rule set's four-eyes threshold, never against
anything the client sends. A decision that needs four eyes waits in `pending_decisions`. The append-only
`claim_decisions` row and the claim's status change are written in one transaction, and the workflow is
resumed only after commit. The generic resume endpoint refuses human-review steps.
**Consequences.** Every final decision names a person, and a second person where required, with reasons
and overrides recorded. A limit or permission change applies to the next decision immediately. A stale
browser cannot approve against a recommendation it has not seen.

## ADR-019 Metrics are queries with definitions, scoped like the data
**Decision.** Every dashboard and analytics figure is one SQL aggregate over the claims the caller may read
(tenant, assignment, deletion, synthetic switch) and is returned with a plain-language definition. Rates over
zero rows are null. Charts are dependency-free SVGs that also print their values. Each KPI key must have a
query test against an exact fixture dataset in PostgreSQL.
**Consequences.** Figures cannot leak across tenants or past assignment rules, a definition can always be
checked against its query, and an empty organisation shows "—" instead of a flattering 0%. Heavy history
(months of runs) will need pre-aggregated tables later; Phase 14 measures this.

## ADR-020 Reports are rendered with ReportLab, not WeasyPrint
**Context.** The roadmap named WeasyPrint. It needs the Pango and GTK system libraries, which cannot be
installed on the Windows host where the integration tests run, and which add a large native dependency to
the backend image.
**Decision.** Render PDFs with ReportLab (BSD licence, pure Python) from the same JSON document that is stored
as the JSON report version. The PDF carries the same evidence labels and an evidence appendix. Tests read
the PDF text back with pypdfium2 and require every evidence label to be present.
**Consequences.** There is no HTML or CSS templating: the layout lives in code (`report_pdf.py`), and the
standard fonts cover Latin-1 only (other scripts would need an embedded TTF). Reports are reproducible
from stored rows plus the template version.

## ADR-021 Events write notifications and webhook deliveries in the same transaction
**Decision.** Business changes call `events.emit`, which inserts in-app notifications, pending email and SMS
notifications, and pending webhook deliveries in the caller's transaction. Beat-driven dispatchers send them
afterwards with row locks, retries and backoff. Webhook secrets are encrypted at rest, payloads carry
identifiers only, and every delivery re-checks that the target resolves to a public address.
**Consequences.** No notification or webhook is lost or sent for a change that rolled back. Delivery is at
least once with up to 10 s latency, so receivers must deduplicate on `X-ClaimOS-Delivery` or on the event id.

## ADR-022 No prompt or model change without a passing regression run
**Decision.** Prompt versions are files, selected per organisation per agent. A prompt version, or a model an
agent did not use before, can be activated only with a passed `eval_runs` row from the last 30 days for that
organisation, agent and exact prompt text or model. A run passes when the deterministic set and the live
injection and own-set parts pass, the candidate actually answered, and no gated metric regressed from the
last passed run. The checks live in the services, not the UI.
**Consequences.** An unavailable or rate-limited model can never be activated by default, because a run that
never reached the model ends in `error`. Free-tier quotas make this slow but safe. The Phase 6 baseline
(prompt v1 and the probe-verified default model) is grandfathered.

## ADR-023 Local lexical embeddings first
**Decision.** Similar-claim search uses a deterministic local feature-hashing embedding stored in pgvector,
with the model id kept on every vector.
**Consequences.** No claim text leaves the platform, and results are reproducible. Re-used and templated
narratives are caught; paraphrases are not. A neural embedding model can be added later by writing vectors
under a new model id.


## ADR-024 Built-in TOTP for two-factor authentication
**Context.** Enterprise customers require MFA, often only for some roles. An external identity provider
(SAML/OIDC) is the long-term answer but cannot be assumed for every tenant.
**Decision.** TOTP per RFC 6238 (SHA-1, 6 digits, 30 s, the defaults every authenticator app uses),
implemented with the standard library and verified against the RFC test vectors. Secrets are AES-GCM
encrypted with a per-user context; ten one-time recovery codes are stored as SHA-256 hashes. After a
correct password, a user with MFA gets a short-lived challenge (5 minutes, 5 attempts, hash stored), never a
session. A code must belong to a later time step than the last accepted one, so a code cannot be replayed.
An organisation can require MFA per role; until such a user enrols, every permission check refuses.
**Consequences.** No new dependency and no third party sees sign-ins. WebAuthn/passkeys and SSO remain
future work; admins reset a lost factor (audited) instead of a self-service recovery flow.

## ADR-025 Secrets as files, environment variables still win
**Decision.** Settings read `/run/secrets/<setting name>` (Docker secrets, Kubernetes Secret volumes, or a
secrets-manager CSI driver) when the directory exists, or `SECRETS_DIR`. An environment variable with the
same name overrides the file. The production compose file and Kubernetes manifests pass every secret as a
file; none appear in `docker inspect` or the pod spec.
**Consequences.** Any secrets manager that can project files works without code changes. Rotation is a
file change plus a rolling restart.

## ADR-026 Retention and audit verification run in the database's terms
**Decision.** A nightly beat task calls one `SECURITY DEFINER` function that deletes only expired platform
data (sessions, login attempts, sent notifications, finished webhook deliveries, old checkpoints, expired
idempotency keys and MFA challenges) and returns counts, recorded in `retention_runs`. Business records and
audit events are never deleted by it. A second nightly task re-hashes every organisation's audit chain,
stores the result in `audit_verifications` and alerts audit readers (in-app and email) when it breaks.
**Consequences.** The runtime role cannot delete across tenants by itself; only this narrow function can.
Retention periods are settings. Legal holds and claim-level retention schedules are not modelled yet.

## ADR-027 Metrics labelled by route template, never exposed at the edge
**Decision.** The API exposes Prometheus metrics at `/metrics` on its own port, labelled by method, route
template and status. Nginx and the Kubernetes ingress route only `/api/` and the web app, and `/metrics`
returns 404 at the edge. An optional `METRICS_TOKEN` bearer token protects scrapes.
**Consequences.** Label cardinality stays bounded (no ids in labels). Celery queue depth and database
metrics need exporters (postgres_exporter, redis_exporter, celery-exporter); they are listed as follow-up.

## ADR-028 Lock parent rows FOR NO KEY UPDATE
**Context.** The Phase 14 benchmark found the parallel policy and fraud branches deadlocking. A node held
`SELECT ... FOR UPDATE` on the execution row and waited for the per-execution advisory lock; the other branch
held that advisory lock and waited on a foreign-key check (`KEY SHARE`) against the same row, which
`FOR UPDATE` blocks. PostgreSQL aborted one branch and LangGraph retried it after 2 s.
**Decision.** Rows that other rows reference and whose keys never change (`claims`, `workflow_executions`)
are locked with `FOR NO KEY UPDATE` (`with_for_update(key_share=True)`). It still serialises writers of
the row but does not block inserts that reference it. A test fails if a bare `FOR UPDATE` on those tables
returns.
**Consequences.** No more deadlock retries in the benchmark (P95 at 8 concurrent claims 3.3 s → 1.8 s). Code
that changes a primary key of these tables would need `FOR UPDATE` again; none does.

## ADR-029 Performance claims are published only from measured runs
**Decision.** The "4 minutes per claim" statement is published only from a benchmark run with live models,
per model tier, using `backend/benchmarks` and the definitions in 06-roadmap section 1. Phase 14 measured
the platform with no model calls (key rejected; no model activated by the evaluation gate), so the
statement stays unpublished.
**Consequences.** Sales and product material may quote the measured platform figures only as platform
overhead. The live-model run is a go-live task.
