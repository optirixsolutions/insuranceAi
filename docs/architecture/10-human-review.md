# 10 — Human review (Phase 9 implementation)

A recommendation that is not approved straight through (09) waits for a person. Phase 9 adds the review
queue, the review workspace, every reviewer action, decision authority, segregation of duties, four-eyes
approval and override reasons. Nothing in this phase lets software approve or reject on its own.

## Data (migration 0009)

| Table | Purpose |
|-------|---------|
| `human_reviews` | One row per HUMAN_REVIEW interrupt (unique `execution_id + interrupt_id`); at most one open review per claim (partial unique index). Holds the recommendation, calculated payout, fraud level, reasons, assignee, priority, due time, `four_eyes_required`, outcome. Never deleted by the app role. |
| `reviewer_authorities` | Per user: `max_approval_amount` in a currency, optional product scope, `can_reject`, `can_override_fraud`, `valid_from`/`valid_to`, `granted_by`. Replaced as a whole and audited. |
| `pending_decisions` | A first approval or rejection that needs a second approver (one per review). |
| `claim_decisions` (+ `review_id`, `second_approver_id`, `reason`, `reason_code`, `clause_ids`) | Append-only; human rows record who decided, who confirmed, the reason and any override. |

All tables use row-level security on `app.org_id`. The rule set gains `four_eyes_threshold` (default 5,000)
and `review_sla_hours` (default 48), editable under Settings → Products & rules.

## Workflow

`workflows/review.py` (the HUMAN_REVIEW node) opens the review before interrupting. The interrupt id is
derived from `review_round` (`review-{n}`), so the node's re-run after resume computes the same id, finds the
same row and never opens a second review. Facts come from persisted agent outputs: decision recommendation,
assessment payout, fraud level and score, mandatory review reasons, failed STP gates and contradictions.

| Reviewer action | Recorded by the review service | Workflow after resume |
|-----------------|--------------------------------|-----------------------|
| approve / reject | `claim_decisions` (human) and claim → APPROVED / REJECTED, **same transaction** | routes to END; run completes |
| request information | `claim_decisions` `more_information` | MORE_INFO interrupt; claim → AWAITING_INFORMATION |
| return | `claim_decisions` `returned` | CLAIM_REASSESSMENT → … → a new review (`review-{n+1}`) |
| escalate | review reassigned (or back to the queue), priority +20 | none: the run keeps waiting |
| second approval | the final `claim_decisions` row, with both people | as approve / reject |

The generic `POST /workflow-executions/{id}/resume` refuses human-review steps (422). They can only be
resolved through the review API, which enforces authority and records reasons. Cancelling processing
cancels the open review; a new run supersedes a stale one.

## Decision rules (`rules_engine/review.py`, pure, 100% branch coverage)

* **Authority**, checked at decision time from stored rows, never from the request:
  * an approval needs `amount ≤ max_approval_amount` in the claim currency, for the claim's product, on a valid date;
  * a rejection needs `can_reject`;
  * approving a claim with `high` or `critical` fraud risk needs `can_override_fraud`.
* **Segregation of duties:** the claim's assigned adjuster cannot make its final decision. Nobody can
  change their own authority.
* **Overrides:** an action that differs from the recommendation, or approving a different amount than the
  calculated payout, needs an override reason (422 without one).
* **Four-eyes:** a second, different, authorised person must confirm when any of these holds:
  * the approval is above the rule set's `four_eyes_threshold`;
  * the approval is an override;
  * the fraud risk is high;
  * the rejection goes against the recommendation.

  The second approver's own authority is checked too. They may confirm, or send the decision back to the
  first reviewer.
* Denials return `403 AUTHORITY_EXCEEDED` with every reason; missing permissions return `403 FORBIDDEN`;
  a review someone else holds, or one that is no longer waiting, returns `409`.

## API (05 §2.8)

`GET /reviews` (filters `status`, `assignee=me`, `risk_level`, `product_id`, `overdue`) ·
`GET /reviews/deciders` · `GET|POST /claims/{id}/reviews` · `GET /claims/{id}/reviews/{rid}` (workspace) ·
`POST …/claim | approve | reject | request-information | return | escalate | second-approval` ·
`GET|PUT /users/{id}/authorities`.

Decision endpoints require an `Idempotency-Key`. The stored response is written in the decision's
transaction, and the workflow resume is enqueued only after commit. A reviewer without a claim assignment
can see a claim while it has an open review; taking ownership records them as the claim's reviewer.

## UI

* **Human Review** (`/reviews`): the queue, highest priority first, with status, mine and overdue filters.
* **Review workspace** (`/reviews/{claim}/{review}`, also the claim's **Decision** tab):
  * the recommendation, why a person must decide, your authority and the decision history;
  * forms for approve, reject (reason code and clauses), request information, return and escalate;
  * the override-reason field becomes required when needed;
  * a second approver sees the pending decision with *Confirm* or *Send back*.
* **Fraud Investigation** (`/fraud`): the same queue pinned to high, critical or medium risk.
* The processing panel links a waiting human-review step to its workspace.

## Tests

* `tests/rules/test_phase9_review.py` (16): the authority, segregation, four-eyes and override rules, including denials.
* `tests/integration/test_reviews_integration.py` (9, real PostgreSQL + Redis, the real graph):
  * opening the review and listing it in the queue;
  * idempotent approval through to run completion;
  * every denied path: no authority, over limit, cannot reject, fraud override, missing override reason,
    adjuster deciding, taken by someone else, missing permission, missing Idempotency-Key;
  * four-eyes (same person, under-authorised second approver, decline then confirm);
  * an override rejection;
  * request information, escalation and return for reassessment;
  * cancel;
  * authority management.
* `tests/system/test_reviews_live.py` (2, running stack, real documents and agents): approving the
  recommended payout; changing the amount needs a second approver.
* `frontend/tests/e2e/reviews.spec.ts`: a handler escalates, a reviewer is refused above their limit, then
  approves; the claim is approved and the run completes.
