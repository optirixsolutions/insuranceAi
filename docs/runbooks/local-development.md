# Local development

## Prerequisites

* Docker Desktop (running) with ≥ 6 GB memory
* Python 3.12 + [uv](https://docs.astral.sh/uv/) 0.11 (backend tooling on the host)
* Node 24 LTS (`.nvmrc`) + pnpm 10 (frontend tooling on the host)

Celery workers and (from Phase 4) OCR only run in Linux containers; the host only needs the tooling above.

## Start the stack

```bash
python scripts/dev_setup.py          # writes .env with generated secrets (never committed)
docker compose up -d --build --wait  # postgres, redis, object-storage, migrate, storage-init, api, worker, frontend, nginx
```

Open <http://localhost:8080>. The Overview page shows live readiness from `GET /api/v1/health/ready`.
API docs (non-production only): <http://localhost:8080/api/docs>.

| Service | Purpose | Host port |
|---------|---------|-----------|
| nginx | single origin: `/` → frontend, `/api` → api | 8080 |
| postgres | PostgreSQL 17 + pgvector | 127.0.0.1:5433 |
| redis | broker, cache | 127.0.0.1:6380 |
| object-storage | SeaweedFS S3 gateway | 127.0.0.1:8333 |
| migrate / storage-init | one-shot: `alembic upgrade head`, bucket creation | — |
| api | FastAPI (uvicorn) | internal only |
| worker | Celery (all queues in dev) | internal only |
| frontend | Next.js standalone server | internal only |

Database roles: `claimos_migrator` owns the schema (Alembic); the API and workers connect as
`claimos_app` (DML only). A second database, `claimos_test`, is created for integration tests.

## Backend checks

```bash
cd backend
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy app tasks tests migrations/env.py
uv run pytest tests/unit
```

Integration tests run against the compose services and the `claimos_test` database. Export the
connection settings (values from your `.env`), then:

```bash
cd backend
CLAIMOS_INTEGRATION=1 APP_ENV=test \
MIGRATION_DATABASE_URL=postgresql+asyncpg://claimos_migrator:$POSTGRES_PASSWORD@127.0.0.1:5433/claimos_test \
DATABASE_URL=postgresql+asyncpg://claimos_app:$CLAIMOS_APP_DB_PASSWORD@127.0.0.1:5433/claimos_test \
REDIS_URL=redis://:$REDIS_PASSWORD@127.0.0.1:6380/1 \
STORAGE_PROVIDER=s3 S3_ENDPOINT_URL=http://127.0.0.1:8333 \
S3_ACCESS_KEY_ID=$S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY=$S3_SECRET_ACCESS_KEY \
uv run pytest -m integration tests/integration
```

After pulling a new migration, upgrade the test database once with the same two URLs:
`uv run alembic upgrade head` (from `backend/`, with `MIGRATION_DATABASE_URL` / `DATABASE_URL` pointing at
`claimos_test`).

System tests drive the running stack over HTTP (real uploads, ClamAV, OCR worker):

```bash
cd backend
CLAIMOS_SYSTEM_URL=http://127.0.0.1:8080 uv run pytest -m system tests/system -s
```

## Agent evaluation (Phase 6)

The deterministic acceptance gates run with the unit tests (`uv run pytest tests/agents`). The full set,
including the OCR document variants, runs inside the backend container (Git Bash: `MSYS_NO_PATHCONV=1`):

```bash
docker compose exec api python -m evaluations.run --out /tmp/eval
```

Add `--live --org <organisation id>` to also send the injection and document sets through that
organisation's verified models (free-tier daily limits apply; `--live-limit` caps the cases).
Results and interpretation: [Phase 6 evaluation report](../evaluations/phase6-report.md).

## Processing workflow (Phase 7)

`beat` schedules crash recovery; `worker` runs the `workflow` queue. Start a run from a claim's
**Processing** tab or `POST /api/v1/claims/{id}/processing`. A stuck run is re-enqueued automatically
after 60 s without a heartbeat (`WORKFLOW_STALE_SECONDS`). The crash-recovery acceptance test kills the
worker container:

```bash
cd backend
CLAIMOS_SYSTEM_URL=http://127.0.0.1:8080 uv run pytest -m system tests/system/test_workflow_live.py -s
```

Integration tests on a Windows host need the selector event loop for async psycopg; `tests/conftest.py`
sets it.

## Frontend checks

```bash
cd frontend
pnpm install
pnpm lint && pnpm typecheck && pnpm test && pnpm build
pnpm exec playwright install chromium   # once
pnpm e2e                                # requires the compose stack on :8080
```

For hot reload without rebuilding the frontend image: `API_PROXY_TARGET=http://localhost:8000 pnpm dev`
requires the API port to be published; the default workflow is to rebuild the `frontend` service.

## Reset

```bash
docker compose down -v   # deletes all local data volumes
```

## Email, webhooks and reports (Phase 11)

* Development email goes to Mailpit: open <http://127.0.0.1:8025> to read every message the platform sent.
* Host-run integration tests send email to `127.0.0.1:1025` and deliver webhooks to a local receiver. Export
  `EMAIL_PROVIDER=smtp SMTP_HOST=127.0.0.1 SMTP_PORT=1025 WEBHOOK_ALLOWED_HOSTS=127.0.0.1` alongside the
  integration settings above.
* Add these lines to `.env.example`, then run `python scripts/dev_setup.py` to append them to `.env` with a
  generated key:

  ```
  DATA_ENCRYPTION_KEY=change_me_data_key
  EMAIL_PROVIDER=smtp
  SMTP_HOST=mailpit
  SMTP_PORT=1025
  SMS_PROVIDER=log
  WEBHOOK_ALLOWED_HOSTS=
  ```

  Without `DATA_ENCRYPTION_KEY`, development uses a derived key (reported as a development adapter).

