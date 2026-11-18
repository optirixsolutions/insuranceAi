# 13 — Advanced intelligence (Phase 12 implementation)

## Similar claims (pgvector)

Every claim gets a 256-dimension vector in `claim_embeddings` (migration 0011, HNSW cosine index). The vector is
computed on create, and again when the description, category or place changes.

* **Model:** `claimos-hash-256-v1` is a local, deterministic feature-hashing embedding: words, word pairs and
  character trigrams, signed and L2-normalised.
* **Privacy:** nothing is sent to an external service, and the same text always gives the same vector.
* **Limitation:** it measures *lexical* similarity, so it finds re-used and templated narratives well and
  paraphrases poorly. A neural embedding model can replace it behind the same table; the stored `model`
  column says which one produced each vector.

`GET /claims/{id}/similar` returns the nearest claims the caller may read, with the similarity, whether the
customer is the same, and the wording the two claims share. The UI shows it on the claim's Fraud tab.
`POST /intelligence/embeddings/backfill` (`ai.manage`) embeds older claims.

## Portfolio anomalies

`GET /analytics/anomalies` (on the Fraud Investigation page) runs four detectors, each one query over the
visible claims:

| Type | Rule |
|------|------|
| `amount_outlier` | Robust z-score (median and MAD) of the claimed amount within the product is at least 3.5. It needs at least 30 claims, otherwise the product is reported as "insufficient baseline". |
| `frequent_claimant` | At least 3 claims by one customer within 90 days. |
| `similar_narrative` | Two claims of **different** customers with embedding similarity of at least 0.9. |
| `shared_document` | The same file (SHA-256) attached to several claims. |

Anomalies are leads for a person: nothing changes a score, status or decision.

## Feedback loop

`agent_feedback` holds labels on agent runs:

* **Automatic:** when a person approves or rejects a claim (in the decision transaction), the latest decision
  recommendation is labelled `agree`/`disagree`. A high or critical fraud analysis is labelled
  `false_positive` (approved anyway) or `true_positive` (rejected).
* **Personal:** thumbs up or down on each agent run in the claim's Agents tab. Each person has one label per
  run; a new one replaces the old.
* `GET /analytics/feedback` shows agreement with people, fraud precision and the helpful rate per agent.
  `GET /intelligence/curation-cases` lists disagreements, false positives and unhelpful outputs: the claims
  worth turning into new labelled evaluation cases.

## Evaluation pipeline and the activation gate

An `eval_runs` row tests **one candidate for one agent**: a prompt version (source-controlled files
`prompts/{agent}.vN.md`, mirrored into `prompt_versions`) or a model. It runs on the `system` queue:

1. **Deterministic part.** The agent's labelled set must still pass with the candidate selected.
2. **Live part**, through the real model gateway, synthetic data only. **Only the candidate agent** uses the
   candidate prompt or model. It covers the injection set (all agents, adversarial documents) and the agent's
   own set in live mode (document, policy, assessment).
3. **Verdict:**
   * `passed`: every gated metric passed, the candidate actually answered (`model_calls > 0`), and no gated
     metric is worse than in the latest passed run for the agent;
   * `failed`: a metric failed or regressed;
   * `error`: the model never answered (unavailable, rate-limited or out of quota). A run that never reached
     the model proves nothing, so it never counts.

**Gate.** Both checks are in the services, so no route around them exists:

* **Prompts:** `POST /prompts/{id}/activate` sets the organisation's `agent_configs.prompt_version`. It needs
  a passed run from the last 30 days for exactly that prompt **text** (SHA-256).
* **Models:** `PATCH /agents/{key}` refuses any model the agent did not already use, unless that agent and
  model have a passed run from the last 30 days.

Runs from other organisations, older runs, and runs for a different prompt text never count. Agents read
their prompt version from the organisation's configuration on every run (a context variable in
`agents/base.load_prompt`), and each `agent_runs` row records the `prompt_version_id` it used.

`POST /evals/compare` queues one run per model on the same cases for a side-by-side table (AI Agents →
Evaluations).

**Baseline.** Prompt `v1` and the model assigned by default from `OPENROUTER_DEFAULT_MODEL` (verified by
capability probes) are the Phase 6 baseline, evaluated in `docs/evaluations/phase6-report.md`. They are not
re-gated. Every *change* from that baseline is gated.

The first candidate is `prompts/fraud.v2.md`, a tighter narrative check: formatting differences are not
conflicts, both sources must be named, and high confidence is reserved for conflicts stated outright. It
stays inactive until a run passes.

## Tests

* `tests/integration/test_intelligence_integration.py` (4 tests, real PostgreSQL and pgvector):
  * **similar claims:** pgvector nearest neighbours, re-embedding after an edit, the assignment scope and
    tenant isolation;
  * **anomalies:** all four detectors, plus the insufficient-baseline note;
  * **feedback:** labels written by a four-eyes approval, personal labels and replacement, curation cases;
  * **the gate**, with a **real evaluation run**: the deterministic fraud set passes with v2, and the live part
    goes through OpenRouter. Both outcomes were observed. While the free quota was exhausted the run ended
    `error` and activation stayed refused. With quota available (2026-11-07) the run `passed`: 53 cases, the
    candidate answered, no regressions, and activation was allowed. Stale, other-prompt-text and other-tenant
    passed runs are refused, and the model gate covers both the first model and other agents.
* `frontend/tests/e2e/intelligence.spec.ts`: two claims with the same story show as 100% similar, the
  anomaly is listed, and prompt activation is refused in the UI.
