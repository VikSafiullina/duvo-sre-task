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

## QueueBacklogGrowing
1. Which pool (`deployment` label)? Are its workers up (`up{job="duvo-worker"}`) and processing (jobs/s panel)?
2. Did the arrival rate spike, or did job duration grow?

**Mitigate:** scale the worker pool · find the slow dependency · shed or defer low-priority work.

## QueueNotDraining
**Meaning:** one pool (`deployment` label) has due jobs but finished none in 5 minutes: it is
down, crash-looping or wedged. Every job routed to it waits; sandboxes stay `queued`.
1. `curl localhost:8000/rollout`: how much traffic goes to the canary? `make ps`: is `worker-b` (canary) or `worker-a` (stable) up?
2. `docker compose logs --tail=100 worker-b` (every worker log line carries `deployment` and `version`): crash at startup (Docker, DB) or a stuck job?

**Mitigate (canary):** stop routing to it with `make rollout W=0` (new jobs and all stops go to stable at once).
Then move jobs already queued for the canary to stable, atomically:
`docker compose exec redis redis-cli EVAL "redis.call('ZUNIONSTORE', KEYS[1], 2, KEYS[1], KEYS[2], 'AGGREGATE', 'MIN'); return redis.call('DEL', KEYS[2])" 2 arq:queue arq:queue:canary`
(arq job ids are global, so the stable pool runs them as-is; status CAS makes a double start impossible).
**Mitigate (stable):** restart / roll back `worker-a`. Shifting traffic to an unproven canary is a last resort.

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
2. Docker daemon reachable from the worker? `docker compose exec worker-a python -c "import docker; docker.from_env().ping()"`.
3. `make sandboxes`: how many are running and how old are they?

**Mitigate:** restore Docker/DB access · manual cleanup: `docker rm -f $(docker ps -q --filter label=duvo.sandbox.id)`
(rows are corrected on the next successful sweep).
