# 14. Production readiness (Phase 13)

Phase 13 adds what an insurer's security and operations review asks for before go-live: two-factor
authentication, data retention, continuous audit-log verification, secrets handling, monitoring,
deployment manifests, a proven backup/restore, and a security test pass.

## Two-factor authentication (ADR-024)

| Piece | Where |
|---|---|
| TOTP (RFC 6238), verified against the RFC vectors | `app/security/totp.py`, `tests/unit/test_totp.py` |
| Enrolment, challenge, recovery codes, policy, admin reset | `app/services/mfa_service.py` |
| Sign-in: password step returns `{mfa_required, challenge}` instead of a session | `auth_service.login`, `complete_mfa` |
| Policy enforcement: users in required roles without MFA are refused by every permission check | `app/api/deps.py` |
| API | `POST /auth/mfa/verify`, `GET /auth/mfa`, `POST /auth/mfa/{setup,confirm,disable}`, `POST /users/{id}/mfa/reset`, `GET/PUT /organization/mfa-policy` |
| UI | Settings → Security (enrol, recovery codes, turn off, organisation policy); login code step |

Storage (migration 0012): `user_mfa` (encrypted secret, `last_used_step`, recovery-code hashes),
`mfa_challenges` (hash of the challenge token, expiry, attempts), `organizations.mfa_required_roles`. Both
tables have row-level security. Audit actions: `auth.mfa_enabled`, `auth.mfa_disabled`, `auth.mfa_reset`,
`auth.mfa_recovery_code_used`, `org.mfa_policy_changed`; `auth.login_succeeded` records the method.

Rules: a challenge lives 5 minutes, allows 5 attempts and works once; a TOTP code must be from a later
time step than the last accepted one (replay refused); a recovery code works once; turning MFA off needs
the password and a current code and is refused while the organisation policy requires it; an admin cannot
reset their own factor.

## Retention and audit verification (ADR-026)

Beat runs `system.retention` at 02:15 UTC and `system.verify_audit_chains` at 02:45 UTC
(`tasks/maintenance.py` → `app/services/maintenance_service.py`).

| Data | Deleted when | Setting (days) |
|---|---|---|
| expired idempotency keys | `expires_at` passed | n/a |
| MFA challenges | expired over a day ago | n/a |
| revoked or expired sessions | older than | `RETENTION_SESSION_DAYS` (30) |
| login attempts | older than | `RETENTION_LOGIN_ATTEMPT_DAYS` (90) |
| sent/failed email and SMS; read in-app notifications | older than | `RETENTION_NOTIFICATION_DAYS` (180) |
| delivered or dead webhook deliveries | older than | `RETENTION_WEBHOOK_DELIVERY_DAYS` (30) |
| checkpoints of finished executions | older than | `RETENTION_CHECKPOINT_DAYS` (30) |

Claims, decisions, documents, reports and audit events are never deleted by the job. The deletes run in
one `SECURITY DEFINER` function (`claimos_retention`); the counts go to `retention_runs`. The audit job
lists organisations through `claimos_active_organization_ids()`, re-hashes each chain, writes
`audit_verifications` and, on a break, notifies every user with `audit.read` in-app and by email.

## Secrets (ADR-025)

`Settings` reads `/run/secrets/<setting>` files (or `SECRETS_DIR`); environment variables override files.
The production compose file and the Kubernetes manifests pass every secret as a file.

## Metrics and monitoring (ADR-027)

`/metrics` (Prometheus text format) on the API port: `claimos_http_requests_total{method,route,status}`,
`claimos_http_request_duration_seconds` (histogram), `claimos_app_info`, and process metrics. Routes are
templates (`/api/v1/claims/{claim_id}`). `METRICS_TOKEN` enables bearer authentication. Nginx and the
ingress never route `/metrics`. Prometheus config, four alert rules and a Grafana dashboard are in
`infrastructure/monitoring` (validated with `promtool`).

## Deployment

- `docker-compose.prod.yml`: TLS Nginx (`infrastructure/nginx/nginx.prod.conf`), internal data network,
  secrets as files, a worker pool per queue, one beat, Prometheus and Grafana on localhost.
- `infrastructure/k8s`: Kustomize base with a restricted-PSS namespace, Deployments with read-only root
  filesystems, HPA and PDB for the API, a migrate Job, ingress with TLS, default-deny network policies.
- Runbooks: `docs/runbooks/production-deployment.md`, `docs/runbooks/backup-restore.md`.

## Backup and restore

`scripts/backup.sh` (pg_dump custom format and checksum) and `scripts/restore_drill.sh` (restore into a
scratch database, row-count and schema comparison, audit-chain re-hash with the application code). The
first drill passed on 2026-11-13: 326 organisations and 5,945 audit events, 26 seconds.

## Known limitations

- No WebAuthn/passkeys or SSO (SAML/OIDC) yet; MFA recovery for a lost phone is an admin reset.
- Retention covers platform data only; claim-level retention schedules and legal holds are not modelled.
- Metrics cover the API; PostgreSQL, Redis and Celery need their exporters (postgres_exporter,
  redis_exporter, a Celery exporter).
- Point-in-time recovery and S3 object restore depend on the hosting platform and were not drilled here.
- The Kubernetes manifests were schema-validated, not applied to a live cluster.
