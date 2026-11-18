# Backup and restore

What is protected, how, and the proof that a restore works. The security model (04-security-model.md)
requires a documented restore test every quarter.

| Data | Where | Protection | Restore point |
|---|---|---|---|
| Claims, policies, decisions, audit log, users, settings | PostgreSQL | nightly `scripts/backup.sh` (logical dump) + WAL archiving or managed PITR | PITR: seconds; dump: last night |
| Uploaded documents, rendered reports | S3 buckets | bucket versioning, object lock (compliance mode) and cross-region replication | any object version |
| Queues, caches, sessions | Redis | none needed: tasks are re-driven from PostgreSQL (outbox, heartbeat recovery) | n/a |
| Secrets | secrets manager / `./secrets` | the secrets manager's own backup; the data encryption key must be escrowed | n/a |

Targets: **RPO 15 minutes** with WAL archiving/PITR (24 hours with dumps alone), **RTO 1 hour** for the
database. The data encryption key (`DATA_ENCRYPTION_KEY`) must be restorable separately; without it,
encrypted MFA secrets and webhook secrets in a restored database cannot be read.

## Taking a backup

```bash
scripts/backup.sh                              # development stack
scripts/backup.sh docker-compose.prod.yml      # single-host production
```

The script writes `backups/claimos-<UTC stamp>.dump` (pg_dump custom format) and a `.sha256`, and checks
that the dump can be listed before it reports success. Schedule it nightly (for example 01:30 UTC, before
the 02:15 retention job) and copy both files to an object store with versioning and a retention lock.
`backups/` is in `.gitignore`. On Kubernetes with a managed database, use the provider's automated backups
and PITR instead, and keep this script for the quarterly logical copy.

## Restore drill (quarterly, and after every major upgrade)

```bash
scripts/restore_drill.sh backups/claimos-<stamp>.dump
```

The drill never touches the live database. It checks the checksum, restores into a scratch database
`claimos_restore_drill`, compares row counts of the core tables and the schema version with the live
database, re-hashes every organisation's audit chain with the application's own code
(`scripts/restore_check.py`, run inside the API container), and drops the scratch database. It exits
non-zero if any check fails. Paste the output into the log below.

## Real restore (incident)

1. Stop writers: `docker compose -f docker-compose.prod.yml stop api worker-system worker-workflow worker-reports worker-documents beat`.
2. Keep the damaged database for investigation: `ALTER DATABASE claimos RENAME TO claimos_damaged_<date>;`
3. `CREATE DATABASE claimos OWNER claimos_migrator;` then
   `docker compose exec -T postgres pg_restore -U claimos_migrator -d claimos --exit-on-error --single-transaction < backups/<dump>`.
   With PITR, restore the provider snapshot to the chosen time instead.
4. Re-grant the runtime role (`infrastructure/postgres/init/10-roles.sh` grants; the dump keeps object
   grants) and run `alembic upgrade head` through the migrate service.
5. Start the stack, check `/api/v1/health/ready`, run `GET /api/v1/audit-logs/verify` per organisation (or
   wait for the 02:45 job) and record the incident.

## Drill log

### 2026-11-13: development stack, Phase 13 acceptance

Backup: `backups/claimos-20261113T150959Z.dump`, 2.8 MB, sha256 `925325e3…28c4b117`.

```text
== Restore drill 2026-11-13T15:10:06Z from claimos-20261113T150959Z.dump
checksum: OK
restore: OK in 4 s
table                        live   restored
organizations                 326        326
users                         426        426
audit_events                 5945       5945
customers                     215        215
policies                      214        214
claims                        291        291
claim_documents               461        461
claim_decisions                35         35
agent_runs                    734        734
human_reviews                  51         51
report_versions                14         14
notifications                  68         68
webhook_deliveries              0          0
schema version: live 0012, restored 0012
audit chains: 326/326 organisations valid, 5945 events re-hashed
total drill time: 26 s
RESTORE DRILL PASSED
```

Not covered by this drill: S3 object restore (bucket versioning is a provider setting) and point-in-time
recovery (needs WAL archiving or a managed database). Both must be drilled on the production platform.
