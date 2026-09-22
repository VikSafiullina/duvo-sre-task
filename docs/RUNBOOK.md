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
1. Worker logs: `{service_name="duvo-worker"} |= "failed permanently"`.
2. A shared cause (bad input, downstream outage) or random?

**Mitigate:** fix/rollback · pause producers · re-enqueue failed items once fixed (`POST /items/{id}/process` is idempotent).

## QueueBacklogGrowing
1. Are workers up (`up{job="duvo-worker"}`) and processing (jobs/s panel)?
2. Did the arrival rate spike, or did job duration grow?

**Mitigate:** scale the worker pool · find the slow dependency · shed or defer low-priority work.

## TargetDown
1. `make ps` — container crashed or restarting? `docker compose logs <svc> | tail`.
2. Network/DNS between Prometheus and the target.

**Mitigate:** restart · roll back · fix config.

<!-- TASK: add entries for any new alerts/failure modes. -->
