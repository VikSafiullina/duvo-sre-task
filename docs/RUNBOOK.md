# Runbook

General first steps: `make ps` · `make logs` · Grafana "Duvo service" dashboard · `curl localhost:8000/readyz`.
In a trace, check which span is slow or failing; in Loki filter by `trace_id`.

## ApiErrorBudgetFastBurn
**Meaning:** 5xx ratio is high enough to burn 2% of the monthly budget per hour. **Impact:** users see errors now.
1. Dashboard → which route is failing? Did it start with a deploy?
2. `/readyz` — is Postgres or Redis unhealthy?
3. Logs: `{service_name="duvo-api"} | severity_text="ERROR"`.

**Mitigate:** roll back the last deploy · restore the dependency · shed load (rate limit upstream).

## ApiErrorBudgetSlowBurn
**Meaning:** a steady error trickle. **Impact:** budget runs out in days.
Look for one route/error class dominating; open a ticket, fix in normal hours.

## ApiLatencyHigh
1. Is it all routes (saturation — CPU, DB pool) or one route (a slow query)?
2. Traces: sort by duration and look at the slowest span.

**Mitigate:** scale out · add an index / cache · tighten timeouts so callers fail fast.

## JobFailureRateHigh
1. Worker logs: `{service_name="duvo-worker"} |= "failed permanently"` (the `error` field names the cause).
2. A shared cause (bad input, downstream outage) or random?

**Mitigate:** fix/rollback · pause producers · request new sandboxes once fixed (failed rows keep the `error`; `GET /sandboxes/{id}` shows why).

## SandboxQueueStalled
**Meaning:** a sandbox request has waited > 60s for a worker to pick it up. Agents are blocked.
1. Dashboard → "Queue depth by queue" and "Start outcomes / s": is anything being consumed at all?
2. Worker up? `up{job="duvo-worker"}`, `make ps`, `make logs`. A crash loop at startup is often
   Docker: the worker fails fast if the socket is unreachable.
3. Throughput dropped but not zero → "Job p95 duration": are starts slow (image pull, readiness)?

**Mitigate:** restart/roll back the worker · scale workers (`max_jobs`) · if Docker is the cause,
fix the daemon. Requests past their TTL are skipped automatically, not started late.

## SandboxStartSlow
**Meaning:** p95 request → serving is above the 10s freshness SLO for 10m.
1. Split the time: "Queue wait p95" (queueing) vs "Job p95 duration" (container start + readiness).
2. Queueing → see SandboxQueueStalled / SandboxCapacityHigh. Starting → worker logs
   `|= "sandbox running"` show `time_to_running_s` per sandbox; traces show slow Docker calls vs probes.
3. Image pulls: did `SANDBOX_IMAGE` change or the host lose its cache? (`image pre-pull failed` log)

**Mitigate:** pre-pull the image · add worker capacity · roll back a slow sandbox image.

## SandboxStuckInTransition
**Meaning:** a sandbox has been `starting` or `stopping` for > 2 min, longer than any legit start
(~45s worst case) or stop. Usually a job killed by `job_timeout` mid-flight.
1. `GET /sandboxes?limit=100` → find it; worker logs by `sandbox_id`.
2. `make sandboxes` → does its container exist?

**Mitigate:** `stopping` resolves on the next reaper sweep. A stuck `starting` is cleaned up at
TTL; to free it now, `DELETE /sandboxes/{id}`.

## SandboxCapacityHigh
**Meaning:** > 80% of `SANDBOX_MAX_ACTIVE` in use for 10m. At 100% `POST /sandboxes` answers 429.
1. Real demand (request rate up) or leak (long TTLs, stops not completing)? "Sandboxes by status".
2. Many `stopping` → the stop path is broken (see SandboxReaperFailing).

**Mitigate:** raise the cap if the host has headroom (memory ≈ cap × `SANDBOX_MEMORY`) · add hosts ·
ask heavy callers for shorter `ttl_s`.

## TargetDown
1. `make ps` — container crashed or restarting? `docker compose logs <svc> | tail`.
2. Network/DNS between Prometheus and the target.

**Mitigate:** restart · roll back · fix config.

## SandboxLeaked
**Meaning:** the reaper removed containers with no live DB row (`orphan`: worker crashed between
`docker run` and the status write, or a stop was lost) or failed rows whose container vanished
(`vanished`: OOM-kill, manual `docker rm`, daemon restart). Already cleaned up; this is a signal.
1. Logs: `{service_name="duvo-worker"} |= "sandbox reaped"` → which `reason`, which ids?
2. `vanished` in bulk → `docker events --filter label=duvo.sandbox.id` / host memory (OOM).
3. `orphan` in bulk → worker restarts (`make ps`, TargetDown) around the same time.

**Mitigate:** raise `SANDBOX_MEMORY` if OOM · fix the crash loop · nothing to clean by hand.

## SandboxReaperFailing
**Meaning:** reconcile sweeps error out, so TTLs, leak cleanup and lost stops aren't handled.
Containers pile up until the cap (`SANDBOX_MAX_ACTIVE`) answers 429 to everyone.
1. Logs: `{service_name="duvo-worker"} |= "reconcile failed"`. The `exc` field has the cause.
2. Docker daemon reachable from the worker? `docker compose exec worker python -c "import docker; docker.from_env().ping()"`.
3. `make sandboxes`: how many are running and how old are they?

**Mitigate:** restore Docker/DB access · manual cleanup: `docker rm -f $(docker ps -q --filter label=duvo.sandbox.id)`
(rows are corrected on the next successful sweep).
