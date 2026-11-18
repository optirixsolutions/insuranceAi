# 07 — Agents (Phase 6 implementation)

Phase 6 implements the eight agents of [03 — Agent contracts](03-agent-contracts.md) individually. Each
runs on one claim, on demand, and its validated output is stored. Orchestration (LangGraph graph,
checkpoints, parallel branches, resume) is Phase 7; until then `POST /claims/{id}/agent-runs/pipeline`
runs the agents **sequentially** in pipeline order, each as its own committed run.

## 1. Layout

| Path | Contents |
|------|----------|
| `backend/agents/contracts.py` | Strict output models (`extra="forbid"`) and the `AgentResult` envelope |
| `backend/agents/snapshot.py` | Read-only `ClaimSnapshot` and `ScopedToolbox` (allow-listed, read-only tools) |
| `backend/agents/base.py` | `Agent` base class, `AgentContext`, `LlmClient` protocol, prompt loading |
| `backend/agents/<key>/agent.py` | The eight agents: intake, document, policy, fraud, assessment, validation, decision, compliance |
| `backend/agents/injection.py` | Detector for text addressed to automated systems (F-INJECTION) |
| `backend/rules_engine/` | Deterministic cores: `text`, `policy_validity`, `coverage`, `calculation`, `fraud_scoring`, `decision` |
| `backend/document_processing/line_items.py`, `grounding.py` | Rule-based line items; amount/date grounding |
| `backend/prompts/<agent>.v1.md` | Versioned prompts for the five model-backed agents |
| `backend/app/services/agent_service.py`, `agent_snapshot.py` | Run lifecycle, gateway adapter, snapshot loading |
| `backend/evaluations/` | Labelled evaluation sets, harness and `python -m evaluations.run` |

## 2. What the model may and may not do

Every agent has a deterministic core and works without a model. A model step is used only through the
Phase 5 gateway (verified capabilities, data-policy gate, budgets, recording) and **only to add grounded
evidence or to escalate**:

| Agent | Model role | Guard |
|-------|-----------|-------|
| Intake, Validation, Compliance | none | — |
| Document | find expected fields the rules missed; classify when rules cannot | excerpt must occur on the cited page (exact or token-set ≥ 0.90), value must be derivable from the excerpt and normalisable by code; confidence = min(self, OCR, grounding, normaliser); otherwise discarded and listed in `rejected_candidates` |
| Policy | read the narrative as perils; flag exclusions needing interpretation | perils from an allowed list; concerns must cite a verbatim excerpt; can only move *covered → potentially covered* and *not triggered → needs interpretation* |
| Fraud | narrative inconsistencies (F-NARRATIVE) | verbatim excerpt required; total model contribution capped at 15 points |
| Assessment | categorise line items the rules left as `other` | never sees or returns amounts; all arithmetic in `rules_engine.calculation` |
| Decision | write the explanation; escalate | outcome is the deterministic matrix; escalation only approve/reject → manual review, with evidence ids from the index; amounts in the explanation must appear in the analyses |

A model answer that fails these checks is sent back for repair, then the next model, then the agent
falls back to its deterministic core and says so in `llm_note`. Text in documents is always treated as
data; instruction-like text raises F-INJECTION, is never a payable line item, and forces manual review.

## 3. Persistence and immutability (migration 0006)

* `agent_runs`: created `running` by the API (one running run per claim and agent, unique index),
  finalised exactly once by the worker; a trigger refuses any later change and all deletes. Records the
  input hash, prompt version, rule-set version, model, tokens, cost, findings, missing items and errors.
* `agent_outputs`: the validated output (strict schema re-validation before storage) with its hash;
  append-only.
* `prompt_versions`: mirrors `prompts/*.md`; content is immutable per version. A prompt edited without a
  version bump stops the agent (`PromptDriftError`).
* `model_invocations.agent_run_id` links every model call to its run. Audit events
  `agent.run.started` / `agent.run.<status>` are written to the hash-chained audit log.

## 4. API

| Method | Path | Permission |
|--------|------|-----------|
| GET | `/agent-catalog` | `claims.read` |
| POST | `/claims/{id}/agent-runs` `{agent_key}` | `processing.start` (202; 409 if already running) |
| POST | `/claims/{id}/agent-runs/pipeline` | `processing.start` (202; sequential) |
| GET | `/claims/{id}/agent-runs` | `claims.read` (object-level claim access) |
| GET | `/agent-runs/{id}` | `claims.read` (object-level claim access) |

## 5. Evaluation (03 §7 acceptance)

`python -m evaluations.run` (inside the backend container for the OCR variants) runs all sets and writes
`evaluations/reports/phase6-*.{json,md}`; `--live --org <uuid>` additionally runs the injection and
document sets through the organisation's verified models. The deterministic gates also run in the test
suite (`tests/agents/test_acceptance.py`). Results: [Phase 6 evaluation report](../evaluations/phase6-report.md).
