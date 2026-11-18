# Security checklist (Phase 13 go-live gate)

Results recorded on 2026-11-13 against the development stack. **To be reviewed and signed by the product
owner / security officer at the bottom.** Each item names the evidence; "user action" items cannot be done
in code.

## Automated scans

| Scan | Scope | Result |
|---|---|---|
| `pip-audit` 2.10.1 | 300 locked backend packages (`uv export --frozen --no-dev`) | **0 known vulnerabilities** |
| `bandit` (all severities) | `app/`, `tasks/` (15,854 lines) | **0 medium, 0 high.** 15 low: 14 `assert` (B101; images never run with `-O`), 1 B406 false positive (`xml.sax.saxutils.escape` escapes text, it does not parse XML) |
| `pnpm audit --prod` | frontend runtime dependencies | **0 known vulnerabilities** |
| `pnpm audit` (incl. dev) | frontend toolchain | 1 moderate: `minimatch` ReDoS via `eslint-config-next` (lint-time only, not shipped). Accepted; upgrade with the next ESLint release |
| OWASP ZAP baseline | `http://localhost:8080` (passive scan + spider) | **0 fail, 63 pass, 5 warn** after fixes (see below) |
| `kubeconform -strict` | 21 Kubernetes resources | 21 valid, 0 invalid |
| `promtool check config` | Prometheus config + 4 alert rules | valid |

### OWASP ZAP baseline (stable image, 2026-11-13)

First run: 0 fail, 61 pass, 7 warn. Fixed in this phase:

| Finding | Cause | Fix |
|---|---|---|
| Sensitive information in URL (10024) | a login/register form submitted before JavaScript loaded used GET, putting the email and password in the URL | `method="post"` on every auth form (`features/auth/auth-forms.tsx`) |
| X-Content-Type-Options missing (10021), Permissions-Policy missing (10063), CORP missing (90004) | Next.js static assets bypass `proxy.ts` | headers for `/_next/static/*` in `next.config.ts` |
| Cross-Origin-Opener-Policy missing (90004) | pages | `COOP: same-origin` in `proxy.ts` |

Second run: **0 fail, 63 pass, 5 warn**, all accepted:

| Warning | Why it is accepted |
|---|---|
| Absence of Anti-CSRF Tokens (10202) | ZAP looks for a hidden form field; ClaimOS sends a double-submit token in the `X-CSRF-Token` header from JavaScript and the API also checks `Origin` (tested in `test_auth.py` and e2e) |
| Non-Storable Content (10049) | intentional `Cache-Control: no-store` on authenticated responses |
| Content-Type Header Missing (10019) | only on 307 redirects with an empty body |
| Cross-Origin-Embedder-Policy (90004) | `require-corp` would block legitimate cross-origin resources; COOP and CORP are set |
| Authentication Request Identified (10111) | informational |

An authenticated active scan (ZAP full scan) is part of the external penetration test.

## Controls verified by tests on the real stack

| Control | Evidence |
|---|---|
| Two-factor authentication (TOTP, RFC 6238 vectors) | `tests/unit/test_totp.py`, `tests/integration/test_mfa_integration.py` (4), `frontend/tests/e2e/mfa.spec.ts` |
| MFA replay, brute force (5 attempts), expiry, one-time recovery codes | `test_mfa_integration.py` |
| MFA policy per role blocks every permission until enrolment | `test_organisation_policy_and_admin_reset` |
| MFA secret encrypted at rest | `test_enrolment_and_two_step_sign_in` (no plaintext in `user_mfa`) |
| Tenant isolation (RLS) and cross-tenant probes | `tests/integration/test_tenancy_and_escalation.py` (Phase 2+) |
| Session cookies `__Host-`, Secure, HttpOnly; CSRF; origin check; lockout | `tests/integration/test_auth.py`, e2e CSRF test |
| Audit chain tamper detection, nightly verification and alert | `test_audit_chain_verifies_and_detects_tampering`, `test_audit_chain_check_records_and_alerts_on_tampering` |
| Retention deletes only expired platform data, never audit events | `test_retention_deletes_only_expired_platform_data` |
| Restore works and the restored audit chains verify | `docs/runbooks/backup-restore.md` drill log (326 organisations) |
| `/metrics` not exposed at the edge; token enforced when set | `tests/unit/test_metrics.py`; live check through Nginx |
| Secrets readable from files; env overrides | `tests/unit/test_config.py::test_secrets_are_read_from_files` |
| Production refuses development adapters | `tests/unit/test_config.py` |
| Webhook SSRF guard, HMAC signatures | Phase 11 integration tests |
| Upload validation and ClamAV | Phase 4 tests, `tests/system/test_documents_system.py` |
| OpenRouter key never reaches the browser | server-side gateway only; frontend has no key setting |

## Configuration items (user action before go-live)

- [ ] Real secrets generated and stored as files / in the secrets manager; none in `.env` on servers.
- [ ] `DATA_ENCRYPTION_KEY` generated (64 hex), escrowed separately from database backups.
- [ ] TLS certificate installed; HSTS confirmed at the public host name.
- [ ] `METRICS_TOKEN` set; Prometheus/Grafana reachable only from the operations network.
- [ ] MFA policy turned on for at least `org_admin`, `claims_manager` and `reviewer` (Settings → Security).
- [ ] S3 buckets: versioning, SSE-KMS, object lock, public access blocked.
- [ ] WAL archiving / PITR enabled; first production restore drill recorded.
- [ ] Alert routing (Alertmanager → on-call) tested.
- [ ] A new, valid OpenRouter key in place (the current development key is rejected by OpenRouter:
      "unauthorized: User not found") and the chosen production model passed the evaluation gate.
- [ ] `ai_data_processing_approved` reviewed with the data protection officer before real claim data.
- [ ] External penetration test scheduled (not part of this phase).

## Accepted risks

| Risk | Reason | Revisit |
|---|---|---|
| No SSO / WebAuthn | TOTP covers the MFA requirement; SSO depends on each customer's IdP | Phase 14+ |
| Lost MFA phone needs an admin reset | audited; avoids weaker self-service recovery | with SSO |
| Dev-only `minimatch` advisory | not in the runtime image | next ESLint upgrade |
| No database/Celery exporters | API metrics and health checks cover availability | before scale testing |

## Sign-off

| Role | Name | Date | Signature |
|---|---|---|---|
| Product owner | | | |
| Security officer | | | |
