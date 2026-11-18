#!/usr/bin/env bash
# Logical backup of the ClaimOS database (Phase 13).
#
#   scripts/backup.sh [compose-file]        default compose file: docker-compose.yml
#
# Writes backups/claimos-<UTC timestamp>.dump (pg_dump custom format, compressed) and a .sha256 next to
# it. Run nightly from cron; copy the files off the host (object storage with versioning and a retention
# lock). Point-in-time recovery additionally needs WAL archiving or a managed database's PITR (see
# docs/runbooks/backup-restore.md). Uploaded documents and reports live in S3 and are protected there by
# bucket versioning and replication, not by this script.
set -euo pipefail

COMPOSE_FILE="${1:-docker-compose.yml}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p backups
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="backups/claimos-${STAMP}.dump"

echo "Backing up database 'claimos' with ${COMPOSE_FILE} -> ${OUT}"
docker compose -f "$COMPOSE_FILE" exec -T postgres \
  pg_dump --username claimos_migrator --dbname claimos --format custom --compress 6 --no-password \
  > "${OUT}.partial"
mv "${OUT}.partial" "$OUT"

# The dump must be readable and complete before it counts as a backup.
docker compose -f "$COMPOSE_FILE" exec -T postgres pg_restore --list < "$OUT" > /dev/null
(cd backups && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")

echo "OK $(du -h "$OUT" | cut -f1) $(cut -d' ' -f1 "${OUT}.sha256")"
echo "$OUT"
