# duvo-sre-task

> **Pre-task scaffold.** Everything up to tag `pre-task-scaffold` was prepared *before* the timed
> window: a generic, production-shaped service skeleton (API + worker, health probes, metrics,
> traces, logs, SLO alerts, Docker, Terraform for Cloud Run, CI). Work on the actual task starts
> after that tag — `git diff pre-task-scaffold..main` is exactly what was built in the hour.

## Summary
<!-- TASK: 2–3 sentences — what this does, for whom, and what "done" means here. -->

## Run it
```bash
make up      # api, worker, postgres, redis, grafana + prometheus + loki + tempo
make smoke   # end-to-end check
make test    # tests against real Postgres/Redis
make load    # k6 load test (p95 < 300ms, errors < 1%)
make chaos   # inject 30% job failures to watch retries/alerts (make chaos RATE=0 to stop)
make sandboxes  # list sandbox containers the worker launched
make down
```

| What | Where |
|---|---|
| API docs (OpenAPI) | http://localhost:8000/docs |
| Grafana — "Duvo service" dashboard, traces, logs | http://localhost:3000 |
| Prometheus alerts | http://localhost:9090/alerts |

## Architecture
```
client ──POST /sandboxes {"type":"http","ttl_s":600}──▶ api (producer) ──▶ Postgres (sandbox row = source of truth)
       ──DELETE /sandboxes/{id}──▶   │  enqueue start_sandbox:{id} / stop_sandbox:{id} (deterministic ids)
                                     ▼
                  Redis (arq queue) ──▶ worker (consumer) ──docker.sock──▶ sandbox-{id} containers
                                          │  every 30s: reaper (TTL, orphans, lost stops, vanished)
                                          └─ readiness probe ──▶ [duvo-sandboxes network: no route to Postgres/Redis]
  queued → starting → running → stopping → stopped      (failed on exhausted retries; 429 at SANDBOX_MAX_ACTIVE)
api + worker ──OTLP (traces, logs)──▶ OTel Collector ─▶ Tempo / Loki ─▶ Grafana
Prometheus ──scrape /metrics──▶ api :8000, worker :9100   (SLO burn-rate alerts)
```

## Decisions & trade-offs
| Decision | Why | Trade-off / revisit when |
|---|---|---|
| Sandbox row written *before* enqueue; enqueue failure marks it `failed` + 503 | Worker always finds its row; no sandbox is left looking `queued` forever | Caller retry creates a new sandbox — an `Idempotency-Key` header would fix that |
| `asyncio.timeout` around enqueue | arq sets only a *connect* timeout; a hung Redis would otherwise hang the request | No retry on enqueue: fail fast, caller retries |
| Worker skips sandboxes not in `queued`/`starting` | Queue delivery is at-least-once; a redelivered job must not start a sandbox twice | Relies on the DB row, not on arq's result store (`keep_result=0`) |
| `jobs_enqueued_total{outcome}` on the producer | Producer health visible separately from HTTP 5xx | 503s also burn the API SLO; no separate alert yet |
| `url`/`error` columns added in slice 1 | `create_all` never alters existing tables | Slice 2 still needed `DROP TABLE` on the dev DB for `expires_at`; tests now drop+create. Alembic before production |
| **SHORTCUT:** worker mounts the Docker socket, runs as root | Fastest real "container per job"; the socket is root-equivalent whatever uid holds it | Not an isolation boundary. Real platform: gVisor/Firecracker/k8s behind a narrow API; worker can't run on Cloud Run (Terraform left as-is) |
| Sandboxes on their own `duvo-sandboxes` network, published on 127.0.0.1 only | Untrusted agent code can't reach Postgres/Redis/API (verified with `nc`) | No egress policy yet (sandboxes can reach the internet) |
| Hardened containers: uid 65534, read-only FS, `cap_drop=ALL`, no-new-privileges, 64 MiB / 0.25 CPU / 64 pids | Cheap doors closed; one sandbox can't starve the host | Limits are global, not per sandbox type |
| Container name = `sandbox-{id}`, create-or-reuse | A job retried after a worker crash adopts the first attempt's container instead of leaking a second | Exited leftovers are replaced, not revived |
| `running` only after an HTTP readiness probe answers | "Container started" ≠ "server serving"; the URL we hand out works | Probe needs the worker on the sandbox network |
| Status changes are compare-and-set (`UPDATE … WHERE status IN …`) | DELETE, reaper and jobs race on the same row; a plain write could resurrect a stopped sandbox | — |
| Reaper = reconcile loop (Docker labels vs DB) every 30s | One safety net under every fast path: TTL, crash leaks, lost stop jobs, containers killed underneath us | TTL precision ±30s; one worker's cron — revisit with A/B (two queues) |
| DELETE never talks to Docker; enqueue failure keeps `stopping` + 503 | API stays unprivileged; stop intent is durable and the reaper finishes it | — |
| Lifecycle gauges sampled from Postgres at scrape time | The DB is the source of truth; no background poller that can silently die; exactly as fresh as the scrape | One cheap `GROUP BY` per scrape per API replica; partial index on `status` if the table grows large |
| Stall alert on *oldest queued age*, not queue depth | The cap bounds depth (~50), and a queue with zero throughput never shows up in a histogram | — |
| Freshness SLI = request → serving (`sandbox_time_to_running_seconds`) | What agents actually wait for; one number covers queue, container start and readiness | Queue wait and job duration panels split it when it goes red |
| Metric label `task`, never `job` | Prometheus owns `job` and renamed ours to `exported_job`, which silently emptied every per-task query (caught by querying live Prometheus, not by unit tests) | — |
| Failed scrape-time sampler clears its gauges | A gap reads "unknown"; a frozen last value would read "fine" | Alerts on those gauges resolve during a DB outage; DB alerts cover it |
| Queue wait measured on first try only | Retry deferral is our own backoff, not queue pressure | — |
| Admission cap (`SANDBOX_MAX_ACTIVE`, 429 + Retry-After) | A burst can't exhaust the host; k6 treats 429 as healthy backpressure | Count-then-insert is a soft cap under concurrency |
| Liveness (`/healthz`) ≠ readiness (`/readyz`) | A DB blip must not trigger a restart storm | — |
| Deterministic job ids | Duplicate requests don't double-process while a job is queued/running | Window ends when the job finishes; true exactly-once needs DB-level guards |
| Write status *before* enqueue, roll back on failure | A fast worker can't be overwritten by a late API write | Brief "queued" state if the process dies between the two steps |
| Prometheus pull for metrics, OTel for traces + logs | Matches a Prometheus/Grafana shop; deterministic metric names | On Cloud Run use Managed Service for Prometheus or OTLP metrics |
| Route templates as metric labels | Bounded cardinality whatever callers send | — |
| No tracing of the worker's Redis poll loop / `/metrics` scrapes | Keeps traces about real work, not polling | Redis latency comes from metrics instead |
| `create_all` at startup | Speed for a 1-hour task | Alembic migrations as a release step before >1 replica |
| One uvicorn process per container | Simple metrics + predictable memory; scale with replicas | — |

## Reliability & observability
- **SLOs:** [docs/SLO.md](docs/SLO.md) — alerts in `observability/prometheus/rules.yml`, runbook in [docs/RUNBOOK.md](docs/RUNBOOK.md).
- **Dashboard** (Grafana home, "Duvo service"): Overview stats (running, capacity used, start success,
  p95 time-to-running, oldest queued, firing alerts) → Sandbox lifecycle → Queue & producer → API → Logs.
- **Signals:** producer (`jobs_enqueued_total`, 429s), queue (`queue_depth{queue}`, `job_queue_wait_seconds`),
  lifecycle (`sandboxes_active{status}`, `sandbox_oldest_in_status_seconds{status}`, `sandbox_time_to_running_seconds`,
  `jobs_total{task,outcome}`, `sandboxes_reaped_total{reason}`), API RED; JSON logs with `trace_id`; one trace spans
  API → worker → Docker/readiness calls.
- **Failure modes considered:**

| Failure | Detection | Mitigation |
|---|---|---|
| Worker down / not consuming | `SandboxQueueStalled` (oldest queued > 60s, pages), `TargetDown` | Restart/roll back; requests past their TTL are skipped, not started late |
| Docker daemon unreachable | Worker fails fast at startup → `TargetDown`; mid-run → `JobFailureRateHigh` | Fix the daemon; jobs retry with backoff |
| Broken sandbox image / server never answers | Readiness probe fails → retries → `failed`; `JobFailureRateHigh`, `SandboxStartSlow` | Roll back `SANDBOX_IMAGE` |
| Slow starts (image pull, saturated host) | `SandboxStartSlow` on the freshness SLI (p95 > 10s) | Pre-pull (done at startup), add capacity |
| Redis down | Enqueue fails → sandbox `failed` + 503 → API burn-rate alerts; `/readyz` | Restore Redis; callers retry |
| Postgres down | `/readyz` 503, API burn-rate alerts; lifecycle gauges vanish instead of freezing | Restore Postgres |
| Worker crash between `docker run` and the DB write | Reaper removes the orphan → `SandboxLeaked` | Automatic |
| Container OOM-killed / removed underneath us | Reaper marks it `failed` ("container disappeared") → `SandboxLeaked` | Raise `SANDBOX_MEMORY` |
| Reaper broken (TTLs not enforced) | `SandboxReaperFailing` (pages) | Restore Docker/DB; manual cleanup in runbook |
| Job killed mid-start (`job_timeout`) | `SandboxStuckInTransition` | `DELETE` or TTL cleanup |
| Demand burst | `SandboxCapacityHigh` at 80%; 429 line on "Producer / s" | Raise cap / add hosts |
| Duplicate delivery / concurrent DELETE | — (by design) | Compare-and-set transitions, redelivery is a no-op |

## Deploying to GCP (Cloud Run)
```bash
cd infra/terraform && cp terraform.tfvars.example terraform.tfvars   # set project_id, image
terraform init && terraform apply
gcloud secrets versions add duvo-app-database-url --data-file=- <<< "postgresql+asyncpg://..."
```
Not provisioned here (next steps): Cloud SQL, Memorystore, Serverless VPC access, OTel collector.

## What I'd do next
1. Real isolation: gVisor (`runsc`) or Firecracker microVMs, behind a small runtime API so the worker loses the Docker socket.
2. Egress policy for the sandbox network (deny by default) plus per-sandbox auth on the URL.
3. `Idempotency-Key` on `POST /sandboxes` so client retries don't create duplicates.
4. Alembic migrations instead of `create_all`.
5. Hard admission cap (Postgres advisory lock or a Redis semaphore) instead of count-then-insert.

## Time log
- **Plan** — `PLAN.md`: thinnest end-to-end slice first, then containers → observability → A/B → metric cutover.
- **Slice 1 (step 1)** — replaced placeholder `items` with `sandboxes`: `POST/GET /sandboxes`, `start_sandbox` job, worker logs the work and marks `running`; producer metric; tests, smoke, k6 updated.
- **Slice 2 (step 2)**: the worker launches one hardened `traefik/whoami` container per job on an isolated network, probes readiness and records/logs the URL. Added `DELETE`, TTL, admission cap, and a reaper reconcile loop with 2 new alerts and runbook entries. Verified live: URL serves, stop removes it, TTL expiry and orphan cleanup work, sandbox can't reach Postgres/Redis.
- **Slice 3 (step 3)**: lifecycle metrics (freshness SLI, queue wait, per-status counts and ages, capacity, per-queue depth), a rebuilt dashboard (overview row + lifecycle / queue & producer / API / logs), 4 new alerts replacing the depth alert that could no longer fire, and SLO + runbook entries. Found and fixed the `job` label collision by running every panel query against live Prometheus. Verified an alert fires with the worker stopped.
