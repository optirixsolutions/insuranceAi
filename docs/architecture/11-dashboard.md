# 11 — Dashboard and analytics (Phase 10 implementation)

The Overview page and the Analytics and Assessments pages show operational figures. Every figure is a single
SQL aggregate over the caller's data, with its definition shown beside the value. Nothing is estimated or
sampled. A rate over zero rows is shown as "—", never as 0%.

## Scope of every query

`app/services/metrics_common.Scope` restricts every metric to the claims the caller may read. It applies
`claim_access.scope_claims`, which uses the tenant and RLS, excludes deleted claims, and limits users without
org-wide visibility to their assigned claims. Synthetic claims are included only when `include_synthetic=true`
(the default, since dev data is synthetic; the UI has a switch). An adjuster therefore sees figures for their
own claims, and another organisation sees none.

## Endpoints (05 §2.11)

| Endpoint | Figures |
|----------|---------|
| `GET /dashboard/summary?days=&include_synthetic=` | 14 KPIs, listed below |
| `GET /dashboard/activity` | Running and queued runs; runs waiting for people, by interrupt kind; the last 10 failures; reviews awaiting a second approver; overdue reviews |
| `GET /analytics/processing-times` | Per product: completed runs; wall-time average, p50, p90 and p95; processing-time p50, p90 and p95 |
| `GET /analytics/claims?bucket=day\|week` | Volume over time, by category, by current status, and decisions by outcome and mode |
| `GET /analytics/fraud` | Latest fraud analysis per claim by risk level; alerts; top indicators; labelled false-positive rate |
| `GET /analytics/agents` | Per agent: runs, failures, failure rate, retries, p50 and p95 duration, model-use rate, escalation rate, tokens, cost. Also cost per claim, and per model: calls, errors, error rate, p95 latency, cost |
| `GET /analytics/extraction-accuracy` | Per document type: machine-extracted fields, fields a person corrected, accuracy, ungrounded fields |
| `GET /assessments` | The latest assessment per claim, with the rule-set version used |

### Definitions that matter

| Metric | Definition |
|--------|------------|
| Claims received | Claims reported in the period. |
| Open claims | Claims not approved, rejected or completed (now). |
| In review, overdue, awaiting second approver | Open `human_reviews` rows (now), from Phase 9. |
| Approved, rejected | `claim_decisions` rows in the period. |
| Straight-through rate | `stp` approvals / all approvals. |
| Override rate | Human approve or reject decisions with `override` / all human approve or reject decisions. |
| End-to-end time | Execution `duration_ms`, which **includes waits for people**. |
| Processing time | The sum of `node_completed` step durations: system time only. |
| Fraud alerts | Claims whose fraud analysis in the period scored high or critical. |
| False-positive rate | Flagged (high or critical) claims with a final human decision; approved after a flag counts as a false positive. This is a proxy label, and claims without a human decision are excluded. |
| Agent failure | Run status `failed` or `timed_out` (skipped and partial runs are not failures). |
| Model error | Any model call whose status is not `ok`. |
| Extraction accuracy | 1 − corrected / extracted, for fields from rules or models (manual fields excluded). Stored evaluation runs are not included yet. |

## UI

* **Overview:**
  * period buttons (7, 30 or 90 days) and an *Include synthetic data* switch;
  * KPI cards, each with *How is this counted?*;
  * the live activity and exceptions panel;
  * claim volume, category and decision charts;
  * the system health panel.
* **Analytics:** processing-time percentiles, agent and model monitoring, fraud and extraction accuracy.
* **Assessments:** the assessment register.
* Charts are small dependency-free SVGs. Every chart prints its values as text, and the column chart has a
  screen-reader table.

## Tests (the exit criterion: every metric traced to a query test)

* `tests/integration/test_metrics_integration.py` (4 tests, real PostgreSQL):
  * claims are created through the API, and executions, step events, agent runs, model calls, decisions,
    reviews and extracted fields are inserted with exact values, so every expected figure can be worked out
    by hand (for example median wall time 90 s, p90 114 s, false-positive rate 0.5, extraction accuracy 0.6667);
  * `SUMMARY_KEYS` must equal the set of tested keys, so a new KPI without a query test fails;
  * also covered: excluding synthetic data (counts 0, rates unknown), assigned-claims scope, another tenant
    (all zero), activity lists and parameter validation.
* `frontend/features/dashboard/dashboard.test.ts`: unit formatting, including an unknown value shown as "—".
* `frontend/tests/e2e/dashboard.spec.ts`: a claim processed by the real worker appears in the KPIs and in
  the waiting activity; the synthetic switch and the period buttons change the figures; the Analytics and
  Assessments pages render real results.
