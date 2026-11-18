# Production deployment

Two supported shapes: a single host with `docker-compose.prod.yml`, or Kubernetes with
`infrastructure/k8s` (Kustomize) and managed PostgreSQL, Redis and S3. Both run the same images.

## Before the first deployment

- [ ] PostgreSQL 17 with the `vector` and `citext` extensions; WAL archiving or PITR enabled.
- [ ] S3 buckets `claimos-documents` and `claimos-reports` with versioning, default encryption (SSE-KMS)
      and object lock; an IAM user or role limited to those buckets.
- [ ] A production OpenRouter model chosen and passed through the evaluation gate (ADR-022).
- [ ] SMTP relay credentials; TLS certificate for the public host name.
- [ ] `ai_data_processing_approved` reviewed per organisation before any real (non-synthetic) claim data.
- [ ] Security checklist (`docs/security/checklist.md`) signed.

## Secrets

Every secret is a file named after the setting (ADR-025). Generate strong values, for example
`python -c "import secrets; print(secrets.token_hex(32))"`; the data encryption key is exactly 64 hex
characters. Never commit `secrets/`, `tls/`, `backups/` or `prod.env` (all in `.gitignore`).

| File | Content |
|---|---|
| `postgres_password`, `claimos_app_db_password`, `redis_password` | single-host only: service passwords |
| `database_url` | `postgresql+asyncpg://claimos_app:<pw>@<host>:5432/claimos` (runtime role, RLS applies) |
| `migration_database_url` | same with `claimos_migrator` (schema owner, used only by `alembic`) |
| `redis_url` | `redis://:<pw>@<host>:6379/0` (`rediss://` for TLS) |
| `s3_access_key_id`, `s3_secret_access_key` | bucket credentials (omit with IAM roles for service accounts) |
| `openrouter_api_key` | server side only; never sent to the browser |
| `data_encryption_key` | 64 hex chars; escrow it, a restored database is unreadable without it |
| `smtp_password` | SMTP relay password |
| `metrics_token` | bearer token Prometheus sends to `/metrics` |
| `grafana_admin_password` | single-host only |

Kubernetes: `kubectl -n claimos create secret generic claimos-secrets --from-file=secrets/` (or a
secrets-manager CSI driver that projects the same file names).

## Single host

```bash
docker compose -f docker-compose.prod.yml --env-file prod.env up -d --wait
```

`prod.env` sets `CLAIMOS_IMAGE_REGISTRY`, `CLAIMOS_VERSION`, `APP_BASE_URL`, `S3_REGION`, `SMTP_HOST`,
`SMTP_FROM` and `OPENROUTER_DEFAULT_MODEL`; the file refuses to start without them. Only ports 80 and 443
are published; Prometheus (9090) and Grafana (3001) listen on 127.0.0.1 only (use an SSH tunnel). The
`data` network is internal: PostgreSQL and Redis cannot reach or be reached from outside.

## Kubernetes

```bash
kubectl kustomize infrastructure/k8s | less       # review
kubectl apply -k infrastructure/k8s
kubectl -n claimos wait --for=condition=complete job/migrate --timeout=10m
```

Set the image tags in `kustomization.yaml` and the host name in `ingress.yaml` and `config.yaml`. The
namespace enforces the `restricted` Pod Security Standard; every pod runs as uid 10001 with a read-only
root filesystem and no capabilities. Run the `migrate` Job before each rollout (CI step or an Argo CD
PreSync hook). Exactly one `beat` replica (`Recreate` strategy). Manifests were validated with
`kubeconform -strict` against Kubernetes 1.33 (21 resources, 0 invalid).

## Monitoring

Prometheus scrapes `api:8000/metrics` with the bearer token; rules in `infrastructure/monitoring/alerts.yml`
(API down, 5xx ratio above 2 %, p95 latency above 2 s, failed sign-in spikes). Grafana provisions the
"ClaimOS API" dashboard automatically. Route alerts through Alertmanager to the on-call channel.

## Nightly jobs (beat, UTC)

| Time | Task | Effect |
|---|---|---|
| 01:30 | `scripts/backup.sh` (cron, single host) | logical dump + checksum |
| 02:15 | `system.retention` | deletes expired platform data, row in `retention_runs` |
| 02:45 | `system.verify_audit_chains` | re-hashes every audit chain, row per organisation in `audit_verifications`, alert on a break |

## Upgrades and rollback

1. Take a backup. 2. Run the migrate Job/service with the new image. 3. Roll the API, workers and frontend.
Migrations are forward-only in production: roll back by restoring the pre-upgrade backup (see
backup-restore.md), not by `alembic downgrade`.
