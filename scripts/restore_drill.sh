#!/usr/bin/env bash
# Restore drill (Phase 13): proves a backup can be restored and is complete, without touching the live DB.
#
#   scripts/restore_drill.sh backups/claimos-<stamp>.dump [compose-file]
#
# 1. checks the .sha256, 2. restores into a scratch database claimos_restore_drill, 3. compares row counts
# of the core tables with the live database and checks the schema version, 4. re-verifies every
# organisation's audit hash chain on the restored copy, 5. drops the scratch database. Exit code 0 only if
# every check passes. Paste the output into docs/runbooks/backup-restore.md (drill log).
set -euo pipefail

DUMP="${1:?usage: scripts/restore_drill.sh <dump file> [compose-file]}"
COMPOSE_FILE="${2:-docker-compose.yml}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DRILL_DB=claimos_restore_drill
PSQL=(docker compose -f "$COMPOSE_FILE" exec -T postgres psql --username claimos_migrator -v ON_ERROR_STOP=1 -qtA)

started=$(date +%s)
echo "== Restore drill $(date -u +%Y-%m-%dT%H:%M:%SZ) from $(basename "$DUMP")"
(cd "$(dirname "$DUMP")" && sha256sum --check --quiet "$(basename "$DUMP").sha256") && echo "checksum: OK"

cleanup() { "${PSQL[@]}" --dbname postgres -c "DROP DATABASE IF EXISTS ${DRILL_DB} WITH (FORCE)" >/dev/null; }
trap cleanup EXIT
cleanup
"${PSQL[@]}" --dbname postgres -c "CREATE DATABASE ${DRILL_DB} OWNER claimos_migrator"
docker compose -f "$COMPOSE_FILE" exec -T postgres \
  pg_restore --username claimos_migrator --dbname "$DRILL_DB" --exit-on-error --no-password --single-transaction \
  < "$DUMP"
restored=$(date +%s)
echo "restore: OK in $((restored - started)) s"

TABLES="organizations users audit_events customers policies claims claim_documents claim_decisions agent_runs human_reviews report_versions notifications webhook_deliveries"
failed=0
printf '%-22s %10s %10s\n' table live restored
for t in $TABLES; do
  live=$("${PSQL[@]}" --dbname claimos -c "SELECT count(*) FROM $t")
  copy=$("${PSQL[@]}" --dbname "$DRILL_DB" -c "SELECT count(*) FROM $t")
  mark=""
  # The live DB may have grown since the dump; the copy must never have more rows or be empty when live is not.
  if [ "$copy" -gt "$live" ] || { [ "$live" -gt 0 ] && [ "$copy" -eq 0 ]; }; then mark=" <-- MISMATCH"; failed=1; fi
  printf '%-22s %10s %10s%s\n' "$t" "$live" "$copy" "$mark"
done
live_v=$("${PSQL[@]}" --dbname claimos -c "SELECT version_num FROM alembic_version")
copy_v=$("${PSQL[@]}" --dbname "$DRILL_DB" -c "SELECT version_num FROM alembic_version")
echo "schema version: live ${live_v}, restored ${copy_v}"
[ "$live_v" = "$copy_v" ] || failed=1

# Audit chains, verified by the application's own code against the restored copy.
docker compose -f "$COMPOSE_FILE" exec -T -e RESTORE_DRILL_DB="$DRILL_DB" api python - < scripts/restore_check.py || failed=1

echo "total drill time: $(( $(date +%s) - started )) s"
if [ "$failed" -ne 0 ]; then echo "RESTORE DRILL FAILED"; exit 1; fi
echo "RESTORE DRILL PASSED"
