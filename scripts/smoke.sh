#!/usr/bin/env bash
# End-to-end check: API -> Postgres -> Redis queue -> worker -> Postgres, plus metrics.
set -euo pipefail
BASE_URL="${BASE_URL:-http://localhost:8000}"
WORKER_METRICS="${WORKER_METRICS:-http://localhost:9100/metrics}"

step() { printf '\n> %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

step "readiness"
curl -fsS "$BASE_URL/readyz" | jq -c .

step "create item"
id=$(curl -fsS -X POST "$BASE_URL/items" -H 'content-type: application/json' -d '{"name":"smoke"}' | jq -r .id)
echo "id=$id"

step "enqueue processing"
curl -fsS -X POST "$BASE_URL/items/$id/process" | jq -c .

step "wait for worker"
status=""
for _ in $(seq 1 40); do
  status=$(curl -fsS "$BASE_URL/items/$id" | jq -r .status)
  [[ "$status" == "done" || "$status" == "failed" ]] && break
  sleep 0.5
done
[[ "$status" == "done" ]] || fail "item ended in status '$status'"
echo "status=$status"

step "metrics exposed"
curl -fsS "$BASE_URL/metrics" | grep -q 'http_requests_total{method="POST",route="/items"' || fail "api metrics missing"
curl -fsS "$WORKER_METRICS" | grep -q 'jobs_total{job="process_item",outcome="success"}' || fail "worker metrics missing"

printf '\nOK: smoke passed\n'
