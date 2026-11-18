# 12 — Reports, notifications and webhooks (Phase 11 implementation)

## Reports

`POST /claims/{id}/reports {format: pdf|json}` (Idempotency-Key, `reports.generate`) inserts a **queued**
`report_versions` row, numbered per claim and format. The `reports` worker then does the following:

1. builds the content from persisted rows (`app/services/report_builder.py`): the claim, its policy version
   and cited clauses, documents and current fields, the latest output of each agent, decisions with
   decider and second approver, status history and agent runs;
2. renders the PDF when asked (`report_pdf.py`, ReportLab, see ADR-020);
3. stores the bytes in the reports bucket with their SHA-256;
4. marks the version **ready** and emits `report.ready`, which sends the requester an in-app notification
   and fires any webhooks.

**Evidence.** Every statement that rests on evidence cites labels from a single, deduplicated `evidence` index
(`E1`, `E2`, …). Each label points at one of: a document page (with an excerpt), a field, a policy clause (with
its text), a fraud indicator or a line item. The PDF prints the same labels next to each statement, plus an
evidence appendix. Both the integration and the live tests check that every label in the JSON appears in the
PDF text.

**Integrity.** A trigger allows one change only, queued to ready or failed; after that the row is immutable, and
the app role cannot delete it. A download re-hashes the stored object and refuses with 409 on a mismatch.
Every download is audited (`report.downloaded`) and served with `Cache-Control: no-store` and
`X-Content-SHA256`.

## Notifications

`events.emit(...)` writes an event's notifications and webhook deliveries in the transaction that causes the
event. Sending happens later, so a failed send can never roll back a decision.

| Event | In-app (and email) to |
|-------|-----------------------|
| `review.opened` | Everyone who can decide reviews |
| `review.escalated` | The new assignee, also by **email** (or every decider when returned to the queue) |
| `review.second_approval_requested` | Every other decider, also by **email** |
| `claim.decided` (human or straight-through) | The claim's adjuster and its creator |
| `report.ready` | The requester |
| `claim.processing_failed` | Whoever started the run |
| `claim.status_changed` | Webhooks only |

* **In-app centre:** the bell in the header, `/notifications`, mark read and read all.
* **Email:** SMTP. Development uses the bundled **Mailpit** server (UI <http://127.0.0.1:8025>). The beat
  task `notifications.dispatch` sends pending email every 10 s, locking each row with `SKIP LOCKED`.
* **SMS:** adapter only. The `log` provider records a message as sent without contacting a carrier
  (`/integrations/status` says so).
* `POST /integrations/test-message` (`org.manage`) sends a test email to yourself or a test SMS.

## Webhooks

* **Subscriptions:** `org.manage` creates them with an https URL and event types. The signing secret
  (`whsec_…`) is shown **once** and stored encrypted with AES-256-GCM (`app/core/crypto.py`), with the row id
  bound as associated data.
* **Signature:** `X-ClaimOS-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>`, plus
  `X-ClaimOS-Event` and `X-ClaimOS-Delivery`. Receivers should use `app/integrations/webhook_signing.verify`:
  it compares in constant time and rejects anything older than five minutes.
* **Payloads:** identifiers and outcomes only (claim id and reference, status, decision, amount), never
  customer details or text.
* **Delivery:** one row per subscription and event; a 2xx response counts as delivered. Retries follow
  backoff of 30 s × 4^(attempt−1), and a delivery is `dead` after `WEBHOOK_MAX_ATTEMPTS` (6). Beat runs
  `webhooks.dispatch` every 10 s. `POST /webhooks/{id}/ping` sends a test event.
* **SSRF guard:** the URL must be https without credentials. At every delivery the host must resolve only to
  public addresses, and redirects are not followed. Hosts in `WEBHOOK_ALLOWED_HOSTS` may use http and
  private addresses; this is a development adapter that production refuses.

## Configuration

| Setting | Default | Notes |
|---------|---------|-------|
| `DATA_ENCRYPTION_KEY` | unset | 64 hex chars. Without it, development derives a key with HKDF from the database secret (reported as the `derived_encryption_key` development adapter; production refuses this). |
| `EMAIL_PROVIDER` / `SMTP_*` | compose: `smtp` → `mailpit:1025` | `SMTP_STARTTLS`, `SMTP_USERNAME`, `SMTP_PASSWORD` for real relays |
| `SMS_PROVIDER` | `log` | `disabled` turns it off |
| `WEBHOOK_ALLOWED_HOSTS` | empty | development only |

## Tests

* `tests/integration/test_communications_integration.py` (4 tests, on real PostgreSQL, S3, Redis and Mailpit):
  * **evidence:** JSON evidence covers pages and clauses, the same page keeps the same label, and every label
    appears in the PDF text (read back with pypdfium2);
  * **versions:** idempotent request, one queued report per format, version numbering, immutability and no
    deletion (trigger and grants), tampered object refused, tenant isolation;
  * **notifications:** delivered in-app and by email over SMTP, checked through the Mailpit API;
  * **webhooks:** delivered to a real local HTTP receiver whose **signature verifies**; a tampered body, wrong
    secret or replay fails; backoff timing and the dead state are checked, and the SSRF guard blocks a
    private address.
* `tests/system/test_reports_live.py`: on the running stack, real agents produce evidence from real
  documents; the worker generates both formats, the PDF carries every evidence label, and an escalation email
  arrives through the beat dispatcher.
* `frontend/tests/e2e/communications.spec.ts`: covers generate and download from the UI, the bell, the
  Reports page, a test email, and webhook validation, the one-time secret and disabling.
