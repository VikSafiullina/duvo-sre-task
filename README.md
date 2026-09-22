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
make down
```

| What | Where |
|---|---|
| API docs (OpenAPI) | http://localhost:8000/docs |
| Grafana — "Duvo service" dashboard, traces, logs | http://localhost:3000 |
| Prometheus alerts | http://localhost:9090/alerts |

## Architecture
```
client ──POST /sandboxes {"type":"http"}──▶ api (producer) ──▶ Postgres (sandbox row = source of truth)
                    │  enqueue start_sandbox:{id} (deterministic id, trace context in args)
                    ▼
                 Redis (arq queue) ──▶ worker (consumer) ──▶ Postgres  queued → starting → running | failed
api + worker ──OTLP (traces, logs)──▶ OTel Collector ─▶ Tempo / Loki ─▶ Grafana
Prometheus ──scrape /metrics──▶ api :8000, worker :9100   (SLO burn-rate alerts)
```

## Decisions & trade-offs
| Decision | Why | Trade-off / revisit when |
|---|---|---|
| Sandbox row written *before* enqueue; enqueue failure marks it `failed` + 503 | Worker always finds its row; no sandbox is left looking `queued` forever | Retry is safe with an `Idempotency-Key`; without one, a retry creates a new sandbox |
| Optional `Idempotency-Key` on `POST /sandboxes`: unique column, insert first, replay on conflict | A client retry (timeout, 503, agent loop) must not start a second container. The DB constraint settles concurrent duplicates, so there's no check-then-insert race | Keys are global (no tenants yet) and never expire. `create_all` won't add the column to an existing DB: run `ALTER TABLE sandboxes ADD COLUMN idempotency_key VARCHAR(64) UNIQUE` or `make down && make up` |
| Launch bounded by `LAUNCH_TIMEOUT_S` (45s) inside arq's `job_timeout` (60s), checked at startup | When arq's timeout fires it *cancels* the job: no retry, the row stays `starting`, and nothing reaches `jobs_total` or the alerts. Our timeout goes through retry → `failed` | Two timeouts to keep in order; a settings validator enforces it |
| Worker drains for 8s on SIGTERM (`job_completion_wait`) | Deploys, including the A/B cutover, stop cancelling jobs in the middle of a launch | Jobs still running after 8s are cancelled and re-run (`starting` is re-startable) |
| `asyncio.timeout` around enqueue | arq sets only a *connect* timeout; a hung Redis would otherwise hang the request | No retry on enqueue: fail fast, caller retries |
| Worker skips sandboxes not in `queued`/`starting` | Queue delivery is at-least-once; a redelivered job must not start a sandbox twice | Relies on the DB row, not on arq's result store (`keep_result=0`) |
| `jobs_enqueued_total{outcome}` on the producer | Producer health visible separately from HTTP 5xx | 503s also burn the API SLO; no separate alert yet |
| `url`/`error` columns added in slice 1 | `create_all` never alters existing tables | Real migrations (Alembic) before production |
| Liveness (`/healthz`) ≠ readiness (`/readyz`) | A DB blip must not trigger a restart storm | — |
| Deterministic job ids | Duplicate requests don't double-process while a job is queued/running | Window ends when the job finishes; true exactly-once needs DB-level guards |
| Write status *before* enqueue, roll back on failure | A fast worker can't be overwritten by a late API write | Brief "queued" state if the process dies between the two steps |
| Prometheus pull for metrics, OTel for traces + logs | Matches a Prometheus/Grafana shop; deterministic metric names | On Cloud Run use Managed Service for Prometheus or OTLP metrics |
| Route templates as metric labels, unknown HTTP methods → `OTHER` | Bounded cardinality whatever callers send (the method is caller input: `curl -X <random>` would otherwise add a series per request) | — |
| No tracing of the worker's Redis poll loop / `/metrics` scrapes | Keeps traces about real work, not polling | Redis latency comes from metrics instead |
| `create_all` at startup | Speed for a 1-hour task | Alembic migrations as a release step before >1 replica |
| One uvicorn process per container | Simple metrics + predictable memory; scale with replicas | — |

## Reliability & observability
- **SLOs:** [docs/SLO.md](docs/SLO.md) — alerts in `observability/prometheus/rules.yml`, runbook in [docs/RUNBOOK.md](docs/RUNBOOK.md).
- **Signals:** RED metrics per route, job outcomes/latency, queue depth; JSON logs with `trace_id`; one trace spans API → worker.
- **Failure modes considered:** <!-- TASK: table — failure | detection | mitigation -->

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
| 1 | Tenant isolation | No authn and no owner on a sandbox: any caller can list and read every sandbox, including its URL once containers run. `error` returns raw exception text | Cross-tenant data exposure. Deliberately not patched: a tenant header without authn is theatre | Authn at the edge (IAP / JWT) → `tenant_id` claim → owner column, every query scoped, 404 for other tenants' ids, idempotency unique per `(tenant_id, key)` |
| 2 | Idempotency / stuck state | Nothing sweeps `queued`/`starting` rows that have no live job. That happens when the API dies between commit and enqueue, or when a job is cancelled on its last try (arq then drops it without calling our handler) | The sandbox never resolves. With an active-sandbox cap, each one also holds a slot forever | Reconciler cron: re-enqueue stale `queued` rows (safe thanks to the deterministic job id and the status guard) and fail `starting` rows older than `job_timeout × max_tries`. Add a `sandboxes{status}` gauge and a stuck alert |
| 3 | Timeouts | arq's Redis pool only has a *connect* timeout. `/metrics` calls `zcard` with no bound, and the worker's poll can block on a half-open connection | A hung Redis hangs the API scrape (TargetDown pages the API, the wrong component) and stalls the worker | `asyncio.timeout(redis_timeout_s)` around `zcard`, plus a pool with `socket_timeout` |
| 4 | Unbounded inputs | No admission control or per-caller quota: every `POST` becomes a container | One caller can exhaust the host | Cap on active sandboxes plus a per-tenant quota → 429 with `Retry-After`. Count and insert atomically (advisory lock or a counter row), or concurrent requests overshoot the cap |
| 5 | Unsafe retries | The worker retries *every* exception, including ones that can't succeed (bad input, bugs) | Wasted attempts, and the `failed` signal arrives late | Classify errors and fail non-retryable ones on the first try |
| 6 | Idempotency | The worker's status check-then-set isn't atomic; it's safe only because arq's in-progress key serializes a job id. An `Idempotency-Key` isn't bound to the request body (only one `type` exists today) | A future DELETE or reaper could race a start and resurrect a stopped sandbox | `UPDATE … WHERE status IN (…) RETURNING`. Store a body hash with the key and return 422 on mismatch |
| 7 | Unbounded queries | `GET /sandboxes` orders by `created_at` with no index, has no cursor, and rows are never deleted | Every list call is a top-N sort over the whole table | Index on `created_at`, keyset pagination, a retention job |
| 8 | Unbounded inputs | `x-request-id` is echoed and logged without length or charset checks. No request body size cap | Log bloat and log injection | Accept `^[A-Za-z0-9-]{1,64}$`, otherwise generate one. Cap body size at the LB |
| 9 | Graceful shutdown / health | The worker healthcheck only probes the metrics thread, so a wedged arq loop looks healthy. The Cloud Run worker pool has no probe. `/readyz` stays 200 while the API drains | A dead consumer goes undetected until the backlog alert fires | Heartbeat from arq's health key → gauge + alert. Flip readiness on SIGTERM |
| 10 | Secrets | `database_url` is a plain `str` (use `SecretStr` so repr and tracebacks can't leak it). Defaults embed `app:app` credentials, so prod with a missing env var silently targets localhost. `REDIS_URL` is a plain Terraform env var, so an AUTH password would end up in TF state. `/metrics` is served on the public API port | Credential leakage, exposure of internals | No DSN defaults outside local. `REDIS_URL` in Secret Manager like `DATABASE_URL`. Metrics on an internal port. (Compose publishes Postgres and Redis without auth and gives Grafana anonymous Admin: local-only shortcut) |

## What I'd do next
1. Authn and tenant scoping (limitation #1): the only open item that exposes data.
2. Stuck-sandbox reconciler with a gauge and an alert (#2). Fold it into the container reaper so there's one sweep.
3. Redis read timeouts (#3) and an atomic admission cap (#4).

## Time log
- **Plan** — `PLAN.md`: thinnest end-to-end slice first, then containers → observability → A/B → metric cutover.
- **Slice 1 (step 1)** — replaced placeholder `items` with `sandboxes`: `POST/GET /sandboxes`, `start_sandbox` job, worker logs the work and marks `running`; producer metric; tests, smoke, k6 updated.
- **SRE review** (parallel worktree, branch `sre-review-fixes`): fixed launches stuck in `starting` (own launch timeout plus SIGTERM drain), `POST` idempotency (`Idempotency-Key`), and HTTP method label cardinality. The other findings are listed under Known limitations.
