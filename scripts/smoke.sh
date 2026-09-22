#!/usr/bin/env bash
# End-to-end check: API -> Postgres -> Redis queue -> worker -> Docker container -> URL,
# then stop -> container gone. Then A/B: the same through the canary pool. Plus metrics.
set -euo pipefail
BASE_URL="${BASE_URL:-http://localhost:8000}"
WORKER_METRICS="${WORKER_METRICS:-http://localhost:9100/metrics}"
CANARY_METRICS="${CANARY_METRICS:-http://localhost:9101/metrics}"

step() { printf '\n> %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
set_weight() {
  curl -fsS -X PUT "$BASE_URL/rollout" -H 'content-type: application/json' -d "{\"canary_weight\":$1}"
}
wait_status() {  # wait_status <id> <wanted> <terminal-regex>
  local status=""
  for _ in $(seq 1 60); do
    status=$(curl -fsS "$BASE_URL/sandboxes/$1" | jq -r .status)
    [[ "$status" =~ $3 ]] && break
    sleep 0.5
  done
  [[ "$status" == "$2" ]] || fail "sandbox $1 ended in status '$status', wanted '$2'"
}
# lifecycle <pool>: create -> running -> serves HTTP -> stop -> gone, on the given pool
lifecycle() {
  step "[$1] request sandbox (producer enqueues job)"
  accepted=$(curl -fsS -X POST "$BASE_URL/sandboxes" -H 'content-type: application/json' -d '{"type":"http","ttl_s":120}')
  echo "$accepted" | jq -c .
  id=$(echo "$accepted" | jq -r .sandbox_id)
  [[ $(echo "$accepted" | jq -r .deployment) == "$1" ]] || fail "routed to the wrong pool, wanted $1"

  step "[$1] worker starts the container"
  wait_status "$id" running '^(running|failed)$'
  url=$(curl -fsS "$BASE_URL/sandboxes/$id" | jq -r .url)
  echo "url=$url"

  step "[$1] sandbox serves HTTP on its URL"
  curl -fsS --max-time 3 "$url" | grep -q '^Hostname:' || fail "sandbox at $url did not answer"
  curl -fsS --max-time 3 "$url" | head -1

  step "[$1] stop sandbox"
  curl -fsS -X DELETE "$BASE_URL/sandboxes/$id" | jq -c '{id, status}'
  wait_status "$id" stopped '^(stopped|failed)$'
  curl -fsS --max-time 2 "$url" >/dev/null 2>&1 && fail "sandbox still answering after stop"
  echo "container gone"
}

step "readiness"
curl -fsS "$BASE_URL/readyz" | jq -c .

step "rollout: all traffic on stable (and back to stable however this script exits)"
trap 'set_weight 0 >/dev/null 2>&1 || true' EXIT
set_weight 0 | jq -c .

step "retry with the same Idempotency-Key returns the same sandbox"
key="smoke-$(date +%s)-$$"
first=$(curl -fsS -X POST "$BASE_URL/sandboxes" -H 'content-type: application/json' -H "Idempotency-Key: $key" -d '{"type":"http","ttl_s":60}' | jq -r .sandbox_id)
again=$(curl -fsS -X POST "$BASE_URL/sandboxes" -H 'content-type: application/json' -H "Idempotency-Key: $key" -d '{"type":"http","ttl_s":60}' | jq -r .sandbox_id)
[[ "$first" == "$again" ]] || fail "idempotent retry created a second sandbox ($first vs $again)"

step "invalid requests are 4xx"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/sandboxes" -H 'content-type: application/json' -d '{"type":"ftp"}')
[[ "$code" == "422" ]] || fail "expected 422 for unknown type, got $code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X PUT "$BASE_URL/rollout" -H 'content-type: application/json' -d '{"canary_weight":101}')
[[ "$code" == "422" ]] || fail "expected 422 for canary_weight 101, got $code"

lifecycle stable

step "rollout: 100% canary, the canary pool (worker-b) serves it"
set_weight 100 | jq -c .
lifecycle canary
set_weight 0 | jq -c .

step "metrics exposed"
api=$(curl -fsS "$BASE_URL/metrics")
worker=$(curl -fsS "$WORKER_METRICS")
canary=$(curl -fsS "$CANARY_METRICS")
grep -q 'jobs_enqueued_total{deployment="stable",outcome="enqueued",task="start_sandbox"}' <<<"$api" || fail "producer metrics missing"
grep -q 'jobs_enqueued_total{deployment="canary",outcome="enqueued",task="start_sandbox"}' <<<"$api" || fail "canary routing metrics missing"
grep -q 'http_requests_total{method="POST",route="/sandboxes"' <<<"$api" || fail "api metrics missing"
grep -q '^rollout_canary_weight 0.0' <<<"$api" || fail "canary weight gauge missing or not reset"
grep -q 'queue_depth{deployment="stable"}' <<<"$api" || fail "per-pool queue depth missing (stable)"
grep -q 'queue_depth{deployment="canary"}' <<<"$api" || fail "per-pool queue depth missing (canary)"
grep -q 'sandboxes_active{status="running"}' <<<"$api" || fail "lifecycle gauges missing"
grep -q 'jobs_total{deployment="stable",outcome="success",task="start_sandbox"}' <<<"$worker" || fail "start job metrics missing"
grep -q 'jobs_total{deployment="stable",outcome="success",task="stop_sandbox"}' <<<"$worker" || fail "stop job metrics missing"
grep -q 'sandbox_reconcile_runs_total{outcome="success"}' <<<"$worker$canary" || fail "reaper metrics missing"
grep -q 'sandbox_time_to_running_seconds_count{deployment="stable"}' <<<"$worker" || fail "freshness SLI missing"
grep -q 'job_queue_wait_seconds_count{deployment="stable",task="start_sandbox"}' <<<"$worker" || fail "queue wait missing"
grep -q 'jobs_total{deployment="canary",outcome="success",task="start_sandbox"}' <<<"$canary" || fail "canary start metrics missing"
grep -q 'sandbox_time_to_running_seconds_count{deployment="canary"}' <<<"$canary" || fail "canary freshness SLI missing"
grep -q 'worker_info{deployment="canary",version="v2"} 1.0' <<<"$canary" || fail "canary worker_info missing"

printf '\nOK: smoke passed\n'
