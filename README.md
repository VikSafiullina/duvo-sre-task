# duvo-sre-task

> **Pre-task scaffold.** Everything up to tag `pre-task-scaffold` was prepared *before* the timed
> window: a generic, production-shaped service skeleton (API + worker, health probes, metrics,
> traces, logs, SLO alerts, Docker, Terraform for Cloud Run, CI). Work on the actual task starts
> after that tag — `git diff pre-task-scaffold..main` is exactly what was built in the hour.

## Summary
A small sandbox orchestrator for AI agents: `POST /sandboxes` puts a job on a Redis queue, a worker starts
one hardened HTTP container per job and returns its URL, and a reaper enforces TTLs and cleans up leaks. It
ships with SLOs, a lifecycle dashboard, alerts and a runbook, plus A/B worker pools (stable/canary) with
deterministic, weight-based routing. "Done" here means task steps 1–4 on `main`, with `make lint test smoke`
green. Step 5 (metric-driven cutover) was cut. See [Time log](#time-log).

## Run it
```bash
make up      # api, worker, postgres, redis, grafana + prometheus + loki + tempo
make smoke   # end-to-end check
make test    # tests against real Postgres/Redis
make load    # k6 load test (p95 < 300ms, errors < 1%)
make rollout W=25  # route 25% of new sandboxes to the canary pool (worker-b)
make chaos   # inject 30% job failures into the canary pool (WORKER=worker-a for stable, RATE=0 to stop)
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
       ──PUT /rollout {"canary_weight":25}──▶ Redis   │  route: crc32(id) % 100 < canary_weight ? canary : stable
                                     ▼
       Redis  arq:queue ─────────▶ worker-a (stable, v1) ─┬─docker.sock──▶ sandbox-{id} containers
              arq:queue:canary ──▶ worker-b (canary, v2) ─┘
                                          │  every 30s: reaper (TTL, orphans, lost stops, vanished), one pool per tick
                                          └─ readiness probe ──▶ [duvo-sandboxes network: no route to Postgres/Redis]
  queued → starting → running → stopping → stopped      (failed on exhausted retries; 429 at SANDBOX_MAX_ACTIVE)
api + worker ──OTLP (traces, logs)──▶ OTel Collector ─▶ Tempo / Loki ─▶ Grafana
Prometheus ──scrape /metrics──▶ api :8000, worker :9100   (SLO burn-rate alerts)
```

## Decisions & trade-offs
| Decision | Why | Trade-off / revisit when |
|---|---|---|
| Scope: steps 1–4 done properly, one end-to-end slice each; step 5 cut | A working, observable core is worth more than five half-built steps. Each slice shipped green before the next started | A/B was built in parallel and merged afterwards; the cutover exists only as a design in `PLAN.md` |
| Sandbox row written *before* enqueue; enqueue failure marks it `failed` + 503 | Worker always finds its row, a fast worker can't be overwritten by a late API write, no sandbox sits `queued` forever | Retry is safe with an `Idempotency-Key`; without one, a retry creates a new sandbox. An API crash between commit and enqueue leaves a `queued` row (limitation #2) |
| Optional `Idempotency-Key` on `POST /sandboxes`: unique column, insert first, replay on conflict | A client retry (timeout, 503, agent loop) must not start a second container. The DB constraint settles concurrent duplicates, so there's no check-then-insert race | Keys are global (no tenants yet) and never expire. `create_all` won't add the column to an existing DB: run `ALTER TABLE sandboxes ADD COLUMN idempotency_key VARCHAR(64) UNIQUE` or `make down && make up` |
| Launch bounded by `LAUNCH_TIMEOUT_S` (45s) inside arq's `job_timeout` (90s), checked at startup | When arq's timeout fires it *cancels* the job: no retry, the row stays `starting`, and nothing reaches `jobs_total` or the alerts. Our timeout goes through retry → `failed` | The validator requires launch + container cleanup (3 × `DOCKER_TIMEOUT_S`) + 2 DB writes ≤ `job_timeout`, hence 90s |
| Worker drains for 8s on SIGTERM (`job_completion_wait`) | Deploys, including the A/B cutover, stop cancelling jobs in the middle of a launch | Jobs still running after 8s are cancelled and re-run (`starting` is re-startable) |
| `asyncio.timeout` around enqueue | arq sets only a *connect* timeout; a hung Redis would otherwise hang the request | No retry on enqueue: fail fast, caller retries |
| Worker skips sandboxes not in `queued`/`starting` | Queue delivery is at-least-once; a redelivered job must not start a sandbox twice | Relies on the DB row, not on arq's result store (`keep_result=0`) |
| `jobs_enqueued_total{outcome}` on the producer | Producer health visible separately from HTTP 5xx | 503s also burn the API SLO; no separate alert yet |
| **SHORTCUT:** worker mounts the Docker socket, runs as root | Fastest real "container per job"; the socket is root-equivalent whatever uid holds it | Not an isolation boundary. Real platform: gVisor/Firecracker/k8s behind a narrow API; worker can't run on Cloud Run (Terraform left as-is) |
| Sandboxes on their own `duvo-sandboxes` network, published on 127.0.0.1 only | Untrusted agent code can't reach Postgres/Redis/API (verified with `nc`) | No egress policy yet (sandboxes can reach the internet) |
| Hardened containers: uid 65534, read-only FS, `cap_drop=ALL`, no-new-privileges, 64 MiB / 0.25 CPU / 64 pids | Cheap doors closed; one sandbox can't starve the host | Limits are global, not per sandbox type |
| Container name = `sandbox-{id}`, create-or-reuse | A job retried after a worker crash adopts the first attempt's container instead of leaking a second | Exited leftovers are replaced, not revived |
| `running` only after an HTTP readiness probe answers | "Container started" ≠ "server serving"; the URL we hand out works | Probe needs the worker on the sandbox network |
| Status changes are compare-and-set (`UPDATE … WHERE status IN …`) | DELETE, reaper and jobs race on the same row; a plain write could resurrect a stopped sandbox | — |
| Reaper = reconcile loop (Docker labels vs DB) every 30s | One safety net under every fast path: TTL, crash leaks, lost stop jobs, containers killed underneath us | TTL precision ±30s |
| A/B = two worker pools (`worker-a` stable, `worker-b` canary), each consuming its own arq queue | A pool is a unit you can compare, drain and roll back. Traffic split happens at the producer, so no service mesh is needed | Locally both pools run the same image and `VERSION` is nominal. A real rollout ships a new image tag to `worker-b` |
| Routing `crc32(sandbox_id) % 100 < canary_weight` | Deterministic: a retried request lands on the same pool. Monotonic: raising the weight only moves sandboxes *into* the canary | Splits jobs, not load; a canary that's slower per job gets the same share |
| Weight in Redis, set by `PUT /rollout` (strict int 0–100); unreadable weight → stable | Every API replica routes identically and the weight survives restarts. Failing safe means the canary never gets traffic by accident | **SHORTCUT:** `/rollout` has no auth. Real: admin-only, or writable only by the rollout controller |
| Stable keeps arq's default queue name | Jobs enqueued by the previous release are still consumed after the deploy | — |
| Stop jobs route by the *current* weight, not the pool that started the sandbox | After a rollback, stops reach stable at once. Any pool can remove any sandbox (same host, same labels) | The row's `deployment` records where the *start* ran |
| `deployment` is a metric label set in code (`jobs_total`, `job_duration_seconds`, `jobs_enqueued_total`, `queue_depth`) + `worker_info{deployment,version}` | The canary-vs-stable comparison needs no scrape-config conventions, and it survives a move to another metrics backend | Adds one label value per pool, so cardinality stays bounded |
| `queue_depth` counts *due* jobs only (`ZCOUNT … -inf now`) | Deferred retries and the next reaper tick also sit in the queue; counting them made an idle pool look backlogged and fired `QueueNotDraining` | Deferred work is invisible in this gauge |
| Reaper cron on both pools, deduped by arq's global cron job id | One sweep per tick across pools, with no leader election; if a pool dies, the other one still sweeps | Startup sweeps can double up, which is harmless (CAS writes, idempotent removes) |
| `deployment` column added by an idempotent `ADD COLUMN IF NOT EXISTS` at startup | `create_all` never alters tables. Expand-only means old and new code both work mid-rollout | Still no real migration tool (Alembic is on the list) |
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
| Prometheus pull for metrics, OTel for traces + logs | Matches a Prometheus/Grafana shop; deterministic metric names | On Cloud Run use Managed Service for Prometheus or OTLP metrics |
| Route templates as metric labels, unknown HTTP methods → `OTHER` | Bounded cardinality whatever callers send (the method is caller input: `curl -X <random>` would otherwise add a series per request) | — |
| No tracing of the worker's Redis poll loop / `/metrics` scrapes | Keeps traces about real work, not polling | Redis latency comes from metrics instead |
| **SHORTCUT:** `create_all` at startup, no migrations | Speed for a 1-hour task | `create_all` never alters tables: adding `expires_at` / `idempotency_key` meant `DROP TABLE` or a manual `ALTER` on the dev DB (tests drop and recreate). Alembic as a release step before >1 replica |
| Burn-rate alerts for the API SLOs, threshold alerts for the sandbox SLOs Faster to build in the hour. Start failures page at 5× burn (> 5% failed vs a 1% budget), and freshness opens a ticket at 1× burn (p95 > 10s) | Noisy at low traffic and blind to a slow trickle. See [SLO known gaps](docs/SLO.md#known-gaps) |
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
| Launch hangs / job cancelled mid-start | Bounded by `launch_timeout_s` (fails visibly, retried); a last-try cancel or SIGKILL → `SandboxStuckInTransition` | `DELETE` or TTL cleanup; auto-sweep is limitation #2 |
| Demand burst | `SandboxCapacityHigh` at 80%; 429 line on "Producer / s" | Raise cap / add hosts |
| Duplicate delivery / concurrent DELETE | — (by design) | Compare-and-set transitions, redelivery is a no-op |

## Deploying to GCP (Cloud Run)
```bash
cd infra/terraform && cp terraform.tfvars.example terraform.tfvars   # set project_id, image
terraform init && terraform apply
gcloud secrets versions add duvo-app-database-url --data-file=- <<< "postgresql+asyncpg://..."
```
Not provisioned here (next steps): Cloud SQL, Memorystore, Serverless VPC access, OTel collector.

## Known limitations & bugs (SRE review)
From a review for timeouts, retries, idempotency, unbounded inputs, isolation, secrets, shutdown
and cardinality. The top 3 are fixed (see Decisions): launches could get stuck in `starting`,
`POST` wasn't idempotent, and the HTTP method metric label had no cardinality bound. Still open,
most severe first:

| # | Area | Issue | Impact | Fix |
|---|---|---|---|---|
| 1 | Tenant isolation | No authn and no owner on a sandbox: any caller can list and read every sandbox, including its URL. `error` returns raw exception text | Cross-tenant data exposure. Deliberately not patched: a tenant header without authn is theatre | Authn at the edge (IAP / JWT) → `tenant_id` claim → owner column, every query scoped, 404 for other tenants' ids, idempotency unique per `(tenant_id, key)` |
| 2 | Idempotency / stuck state | Nothing sweeps `queued`/`starting` rows that have no live job. That happens when the API dies between commit and enqueue, or when a job is cancelled on its last try (arq then drops it without calling our handler) | The sandbox never resolves and holds an admission-cap slot forever: the reaper only reconciles containers and `running`/`stopping` rows. Enough of them and every `POST` gets 429 | **Detected since Slice 3:** `SandboxQueueStalled` / `SandboxStuckInTransition` on `sandbox_oldest_in_status_seconds`. Still open, the automatic fix: reconciler re-enqueues stale `queued` rows (safe thanks to the deterministic job id and the status guard) and fails `starting` rows older than `job_timeout × max_tries` |
| 3 | Timeouts | arq's Redis pool only has a *connect* timeout. `/metrics` is bounded now (Slice 4: `asyncio.timeout` around the per-pool `zcount`), but the worker's poll can still block on a half-open connection | A hung Redis stalls the worker without any error | A pool with `socket_timeout` |
| 4 | Unbounded inputs | The global cap (`SANDBOX_MAX_ACTIVE`) counts and then inserts in separate steps, and there's no per-caller quota | Concurrent `POST`s overshoot the cap, and one caller can take every slot | Count and insert atomically (advisory lock or a counter row), plus a per-tenant quota |
| 5 | Unsafe retries | The worker retries *every* exception, including ones that can't succeed (bad input, bugs) | Wasted attempts, and the `failed` signal arrives late | Classify errors and fail non-retryable ones on the first try |
| 6 | Idempotency | An `Idempotency-Key` isn't bound to the request body: reusing a key with a different `ttl_s` silently returns the original | The caller gets a sandbox it didn't ask for | Store a body hash with the key and return 422 on mismatch |
| 7 | Timeouts / reaper | `asyncio.timeout` around `asyncio.to_thread` stops waiting but not the Docker call, which keeps holding a default-executor thread. One failed `runtime.remove` aborts the whole reconcile sweep | A slow daemon can exhaust the thread pool, and one stuck container blocks every other cleanup | A dedicated bounded executor for Docker calls. Catch per-container errors inside the sweep and keep going |
| 8 | Unbounded queries | `GET /sandboxes` orders by `created_at` with no index, has no cursor, and rows are never deleted | Every list call is a top-N sort over the whole table | Index on `created_at`, keyset pagination, a retention job |
| 9 | Unbounded inputs | `x-request-id` is echoed and logged without length or charset checks. No request body size cap | Log bloat and log injection | Accept `^[A-Za-z0-9-]{1,64}$`, otherwise generate one. Cap body size at the LB |
| 10 | Graceful shutdown / health | The worker healthcheck only probes the metrics thread, so a wedged arq loop looks healthy. The Cloud Run worker pool has no probe. `/readyz` stays 200 while the API drains | A dead consumer goes undetected until the backlog alert fires | Heartbeat from arq's health key → gauge + alert. Flip readiness on SIGTERM |
| 11 | Secrets | `database_url` is a plain `str` (use `SecretStr` so repr and tracebacks can't leak it). Defaults embed `app:app` credentials, so prod with a missing env var silently targets localhost. `REDIS_URL` is a plain Terraform env var, so an AUTH password would end up in TF state. `/metrics` is served on the public API port | Credential leakage, exposure of internals | No DSN defaults outside local. `REDIS_URL` in Secret Manager like `DATABASE_URL`. Metrics on an internal port. (Compose publishes Postgres and Redis without auth and gives Grafana anonymous Admin: local-only shortcut) |

## What I'd do next
Most important first. Item 1 finishes the brief. Items 2–4 block real traffic.
1. **Metric-driven cutover (step 5).** A controller that steps the weight 10→25→50→100 when the canary has ≥ N starts,
   a success ratio ≥ stable and p95 time-to-running ≤ 1.2× stable, and sets it to 0 on a breach. No data means hold
   (never promote on silence), and a dead controller leaves the weight where it is. Rollback moves queued canary
   jobs back to stable. The per-pool signals and the "Rollout (A/B)" dashboard row already exist; add a
   stale-rollout alert.
2. **Authn and tenant scoping** (limitation #1), including `PUT /rollout`. This is the only open item that exposes data.
3. **Stuck-sandbox sweep** (#2). Alerts fire today, but a stuck row holds an admission slot forever, so enough of them
   and every `POST` gets 429.
4. **Real isolation.** Use gVisor or Firecracker behind a small runtime API so the worker no longer holds the Docker
   socket. Deny egress by default and put auth on each sandbox URL.
5. **Bounded dependencies:** a Redis pool with `socket_timeout` for the worker's poll (#3; `/metrics` is already
   bounded) and an atomic admission cap (#4).
6. **SLO hygiene:** burn-rate alerts for the sandbox SLOs and a `10`s histogram bucket (see [SLO known gaps](docs/SLO.md#known-gaps)).
7. Alembic migrations instead of `create_all`.

## Time log
Task received 16:58. Write-up at 18:02, about 65 min in. Times come from commits. The scaffold (16:00–16:28) was done
before the window.

| Time | Step | Result |
|---|---|---|
| 16:58–17:05 | Plan | `PLAN.md` |
| 17:05–17:19 | Slice 1: step 1 queue | on `main` |
| 17:19–17:41 | Slice 2: step 2 container per job | on `main` |
| 17:29–17:34 | SRE review (parallel worktree) | merged 17:58 |
| 17:41–17:56 | Slice 3: step 3 metrics, dashboard, alerts | on `main` |
| 17:45–17:59 | Slice 4: step 4 A/B (parallel worktree) | merged after the write-up (below) |
| — | Step 5 metric cutover | **cut** (not started) |
| 17:59–18:04 | README, SLO, runbook | on `main` |
| 18:04–18:20 | Merge Slice 4 into `main` (past the window) | on `main`, `make lint test smoke` green |

**What I cut, and why:**
- **Step 5 (automated cutover).** It depends on step 4, and there wasn't time for both. The design (gates,
  hold-on-no-data, fail-static weight, rollback drain) is in `PLAN.md` and What I'd do next #1.
- **Isolation, auth, multi-host, sandboxes on Cloud Run:** out of scope on purpose (`PLAN.md` "Deliberately not doing").
  Each is labelled **SHORTCUT** or listed under Known limitations.
- **Migrations and sandbox burn-rate alerts:** swapped for `create_all` and threshold alerts (see Decisions).

**Slice notes:**
- **Plan**: `PLAN.md`. Thinnest end-to-end slice first, then containers → observability → A/B → metric cutover.
- **Slice 1 (step 1)**: replaced placeholder `items` with `sandboxes`: `POST/GET /sandboxes`, `start_sandbox` job, worker logs the work and marks `running`; producer metric; tests, smoke, k6 updated.
- **Slice 2 (step 2)**: the worker launches one hardened `traefik/whoami` container per job on an isolated network, probes readiness and records/logs the URL. Added `DELETE`, TTL, admission cap, and a reaper reconcile loop with 2 new alerts and runbook entries. Verified live: URL serves, stop removes it, TTL expiry and orphan cleanup work, sandbox can't reach Postgres/Redis.
- **Slice 3 (step 3)**: lifecycle metrics (freshness SLI, queue wait, per-status counts and ages, capacity, per-queue depth), a rebuilt dashboard (overview row + lifecycle / queue & producer / API / logs), 4 new alerts replacing the depth alert that could no longer fire, and SLO + runbook entries. Found and fixed the `job` label collision by running every panel query against live Prometheus. Verified an alert fires with the worker stopped.
- **SRE review** (parallel worktree, branch `sre-review-fixes`): fixed launches stuck in `starting` (own launch timeout plus SIGTERM drain), `POST` idempotency (`Idempotency-Key`), and HTTP method label cardinality. The other findings are listed under Known limitations. Merged after Slice 2: the capacity check now lets keyed retries through, and `JOB_TIMEOUT_S` went to 90s so launch + container cleanup + DB writes fit inside it.
- **Slice 4 (A/B, PLAN step 3)**: two worker pools (`worker-a` stable, `worker-b` canary) on separate queues. The producer routes each job with `crc32(id) % 100 < canary_weight`; the weight lives in Redis, is set via `GET/PUT /rollout` (`make rollout W=25`), and falls back to stable if Redis can't be read. Every job metric carries a `deployment` label, plus `worker_info` and a `rollout_canary_weight` gauge; `/metrics` Redis sampling now has a timeout. New alert `QueueNotDraining` with a runbook entry. `make chaos` now targets the canary. Smoke runs the full lifecycle on each pool.
- **Slice 4 merge** (after the window): the branch forked before Slice 3, so it conflicted in 11 files. Resolved on
  Slice 3's side where they overlapped: metric label `task` (not `job`) plus slice 4's `deployment` label, which
  time-to-running and queue wait now carry too (a cutover compares pools on exactly those); per-pool due-job
  `queue_depth` replaces the per-queue gauge; `QueueNotDraining` kept, `QueueBacklogGrowing` stays removed;
  `Idempotency-Key` replay carries the pool; the dashboard gained a "Rollout (A/B)" row. 85 tests; smoke runs the
  lifecycle on both pools.
