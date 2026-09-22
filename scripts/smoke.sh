#!/usr/bin/env bash
# End-to-end check: API -> Postgres -> Redis queue -> worker -> Docker container -> URL,
# then stop -> container gone. Plus metrics.
set -euo pipefail
BASE_URL="${BASE_URL:-http://localhost:8000}"
WORKER_METRICS="${WORKER_METRICS:-http://localhost:9100/metrics}"

step() { printf '\n> %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
wait_status() {  # wait_status <id> <wanted> <terminal-regex>
  local status=""
  for _ in $(seq 1 60); do
    status=$(curl -fsS "$BASE_URL/sandboxes/$1" | jq -r .status)
    [[ "$status" =~ $3 ]] && break
    sleep 0.5
  done
  [[ "$status" == "$2" ]] || fail "sandbox $1 ended in status '$status', wanted '$2'"
}

step "readiness"
curl -fsS "$BASE_URL/readyz" | jq -c .

step "request sandbox (producer enqueues job)"
accepted=$(curl -fsS -X POST "$BASE_URL/sandboxes" -H 'content-type: application/json' -d '{"type":"http","ttl_s":120}')
echo "$accepted" | jq -c .
id=$(echo "$accepted" | jq -r .sandbox_id)

step "retry with the same Idempotency-Key returns the same sandbox"
key="smoke-$(date +%s)-$$"
first=$(curl -fsS -X POST "$BASE_URL/sandboxes" -H 'content-type: application/json' -H "Idempotency-Key: $key" -d '{"type":"http","ttl_s":60}' | jq -r .sandbox_id)
again=$(curl -fsS -X POST "$BASE_URL/sandboxes" -H 'content-type: application/json' -H "Idempotency-Key: $key" -d '{"type":"http","ttl_s":60}' | jq -r .sandbox_id)
[[ "$first" == "$again" ]] || fail "idempotent retry created a second sandbox ($first vs $again)"

step "invalid request is a 4xx"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/sandboxes" -H 'content-type: application/json' -d '{"type":"ftp"}')
[[ "$code" == "422" ]] || fail "expected 422 for unknown type, got $code"

step "worker starts the container"
wait_status "$id" running '^(running|failed)$'
url=$(curl -fsS "$BASE_URL/sandboxes/$id" | jq -r .url)
echo "url=$url"

step "sandbox serves HTTP on its URL"
curl -fsS --max-time 3 "$url" | grep -q '^Hostname:' || fail "sandbox at $url did not answer"
curl -fsS --max-time 3 "$url" | head -1

step "stop sandbox"
curl -fsS -X DELETE "$BASE_URL/sandboxes/$id" | jq -c '{id, status}'
wait_status "$id" stopped '^(stopped|failed)$'
curl -fsS --max-time 2 "$url" >/dev/null 2>&1 && fail "sandbox still answering after stop"
echo "container gone"

step "metrics exposed"
api=$(curl -fsS "$BASE_URL/metrics")
worker=$(curl -fsS "$WORKER_METRICS")
grep -q 'jobs_enqueued_total{outcome="enqueued",task="start_sandbox"}' <<<"$api" || fail "producer metrics missing"
grep -q 'http_requests_total{method="POST",route="/sandboxes"' <<<"$api" || fail "api metrics missing"
grep -q 'jobs_total{outcome="success",task="start_sandbox"}' <<<"$worker" || fail "start job metrics missing"
grep -q 'jobs_total{outcome="success",task="stop_sandbox"}' <<<"$worker" || fail "stop job metrics missing"
grep -q 'sandbox_reconcile_runs_total{outcome="success"}' <<<"$worker" || fail "reaper metrics missing"
grep -q 'sandbox_time_to_running_seconds_count' <<<"$worker" || fail "freshness SLI missing"
grep -q 'job_queue_wait_seconds_count{task="start_sandbox"}' <<<"$worker" || fail "queue wait missing"
grep -q 'sandboxes_active{status="running"}' <<<"$api" || fail "lifecycle gauges missing"
grep -q 'queue_depth{queue="arq:queue"}' <<<"$api" || fail "per-queue depth missing"

printf '\nOK: smoke passed\n'
