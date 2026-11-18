# 09 — Rule sets and straight-through processing (Phase 8 implementation)

## Rule sets

Each product has versioned rule sets (`rule_sets`, migration 0008); the published version applies,
otherwise the built-in defaults (`rules_engine.rule_set.DEFAULT_CONFIG`, identical to the Phase 6
constants). A configuration changes numbers, never the order of operations (03 §5):

| Setting | Used by |
|---------|---------|
| `motor_parts_depreciation` (bands by vehicle age; must start at 0 and never decrease) | motor calculator |
| `health_sub_limits` (per category; policy endorsements override) | health calculator |
| `fraud_weights` (overrides per indicator), `fraud_llm_cap` (0–30) | fraud scoring |
| `validation_amount_tolerance_pct` | cross-document validation |
| `automated_approval_allowed` (compliance profile), `stp` (enabled, max amount, max fraud score, claimant flag window) | compliance, STP gate |

Lifecycle: one draft at a time per product → `PATCH` with `If-Match` → `publish` (needs `rules.publish`;
the previous version is retired). A trigger makes published versions immutable even for the schema
owner; drafts may be deleted, published versions never. Every agent run records the version it used
(`rules_v{n}:{id}` or `rules_v1-default`). UI: Settings → Products & rules.

## Straight-through processing

COMPLIANCE_CHECK routes to `AUTO_APPROVAL_ELIGIBILITY` only when the product allows it and the
recommendation is APPROVE without model escalation. That node re-evaluates the 12 gates of
`rules_engine.gates.stp_gate` from persisted outputs and the published rule set (never from graph
state): STP enabled, profile allows, approve (matrix and final), no escalation, fraud score below the
limit, 0 < payout ≤ max, currency, no fields awaiting verification, validation at most `info`, claimant
not flagged (high/critical fraud on another claim within the window), synthetic or AI-approved data,
compliance not failed. All pass → `FINALIZATION` writes an append-only `claim_decisions` row
(`decision_mode = stp`, gates recorded, `decided_by` null) and moves the claim to APPROVED in the same
transaction. Any failure → HUMAN_REVIEW. STP can only approve (a check constraint); nothing rejects
automatically.

## Quality gates

`pytest tests/rules tests/agents --cov=rules_engine --cov-branch --cov-fail-under=95` runs in CI
(97.4% today). Property-based tests (Hypothesis) cover money arithmetic, rounding, precision and both
calculators under randomly generated valid rule sets.
