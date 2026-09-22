#!/usr/bin/env bash
# End-to-end check: API -> Postgres -> Redis queue -> worker -> Postgres, plus metrics.
set -euo pipefail
BASE_URL="${BASE_URL:-http://localhost:8000}"
WORKER_METRICS="${WORKER_METRICS:-http://localhost:9100/metrics}"

step() { printf '\n> %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

step "readiness"
curl -fsS "$BASE_URL/readyz" | jq -c .

step "request sandbox (producer enqueues job)"
accepted=$(curl -fsS -X POST "$BASE_URL/sandboxes" -H 'content-type: application/json' -d '{"type":"http"}')
echo "$accepted" | jq -c .
id=$(echo "$accepted" | jq -r .sandbox_id)

step "invalid request is a 4xx"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/sandboxes" -H 'content-type: application/json' -d '{"type":"ftp"}')
[[ "$code" == "422" ]] || fail "expected 422 for unknown type, got $code"

step "wait for worker"
status=""
for _ in $(seq 1 40); do
  status=$(curl -fsS "$BASE_URL/sandboxes/$id" | jq -r .status)
  [[ "$status" == "running" || "$status" == "failed" ]] && break
  sleep 0.5
done
[[ "$status" == "running" ]] || fail "sandbox ended in status '$status'"
curl -fsS "$BASE_URL/sandboxes/$id" | jq -c .

step "metrics exposed"
curl -fsS "$BASE_URL/metrics" | grep -q 'jobs_enqueued_total{job="start_sandbox",outcome="enqueued"}' || fail "producer metrics missing"
curl -fsS "$BASE_URL/metrics" | grep -q 'http_requests_total{method="POST",route="/sandboxes"' || fail "api metrics missing"
curl -fsS "$WORKER_METRICS" | grep -q 'jobs_total{job="start_sandbox",outcome="success"}' || fail "worker metrics missing"

printf '\nOK: smoke passed\n'
