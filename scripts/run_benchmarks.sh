#!/usr/bin/env bash
# Phase 14 benchmark matrix against the running development stack.
#
#   scripts/run_benchmarks.sh [quick|full]
#
# Runs inside the compose network (same machine clock as the workers, so SSE lag is exact, and the API can
# be measured directly as well as through Nginx). Writes JSON to backend/benchmarks/results/<UTC stamp>/;
# render the report with:  python -m benchmarks.report backend/benchmarks/results/<stamp>
# Only synthetic data is created (one new organisation per run).
set -euo pipefail

MODE="${1:-full}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="backend/benchmarks/results/${STAMP}"
mkdir -p "$OUT"
R="/app/benchmarks/results/${STAMP}"

bench() {
  MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps -T \
    -v "$ROOT/backend/benchmarks:/app/benchmarks" \
    --entrypoint python api -m "$@"
}

if [ "$MODE" = "quick" ]; then
  bench benchmarks.claims_bench --profile simple --claims 2 --concurrency 1 --out "$R/claims-simple-c1.json"
  bench benchmarks.api_load --base-url http://api:8000 --concurrency 4 --seconds 10 --out "$R/api-direct-c4.json"
else
  bench benchmarks.claims_bench --profile simple --claims 10 --concurrency 1 --out "$R/claims-simple-c1.json"
  bench benchmarks.claims_bench --profile simple --claims 12 --concurrency 4 --out "$R/claims-simple-c4.json"
  bench benchmarks.claims_bench --profile simple --claims 16 --concurrency 8 --out "$R/claims-simple-c8.json"
  bench benchmarks.claims_bench --profile scanned --claims 6 --concurrency 2 --out "$R/claims-scanned-c2.json"
  bench benchmarks.claims_bench --profile poor --claims 4 --concurrency 2 --out "$R/claims-poor-c2.json"
  # Reads against the organisation the simple c8 run filled with claims (the reads include claim details).
  EMAIL="$(python -c "import json,sys; print(json.load(open(sys.argv[1]))['admin_email'])" "$OUT/claims-simple-c8.json")"
  bench benchmarks.api_load --base-url http://api:8000 --email "$EMAIL" --concurrency 8 --seconds 30 --out "$R/api-direct-c8.json"
  bench benchmarks.api_load --base-url http://api:8000 --email "$EMAIL" --concurrency 32 --seconds 30 --out "$R/api-direct-c32.json"
  bench benchmarks.api_load --base-url http://nginx --email "$EMAIL" --concurrency 4 --seconds 20 --out "$R/api-edge-c4.json"
fi
echo "$OUT"
