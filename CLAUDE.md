# CLAUDE.md

Timed (~1h) take-home for Duvo's SRE role, done on screen recording. Optimise for: a working end-to-end
product, visible engineering judgment, and a clear README write-up. Speed matters; so does quality.

## Stack
- `app/main.py` FastAPI app factory · `app/routes/*` endpoints · `app/schemas.py` pydantic models
- `app/models.py` + `app/db.py` Postgres via SQLAlchemy async (tables via `create_all`)
- `app/queue.py` (producer) + `app/worker.py` (arq consumer on Redis)
- `app/metrics.py` Prometheus metrics · `app/observability.py` JSON logs + RED middleware
- OTel traces + logs via zero-code `opentelemetry-instrument` → grafana/otel-lgtm
- `infra/terraform` Cloud Run (API service + worker pool) · `.github/workflows/ci.yml`

## Commands
`make up` · `make smoke` · `make test` · `make lint` · `make fmt` · `make load` · `make chaos` · `make logs`
· `make restart` (rebuild api+worker) · `make check` (all CI checks) · `make down`

## Conventions — apply to every change
- Replace/rename the placeholder `items` domain; don't build next to it. When you do, also update
  `scripts/smoke.sh`, `loadtest/k6.js` and `tests/support.py` (`TRUNCATE items`).
- Every endpoint: pydantic request/response models, input limits, 4xx for caller errors, at least one test.
- Every external call (DB, Redis, HTTP): explicit timeout. Retry only idempotent operations, with backoff.
- Background work: `app/queue.py` helper with a deterministic job id → job in `app/worker.py`.
- Metrics live in `app/metrics.py`; labels are route templates or enum values, never ids.
- Logging: `logging.getLogger("app.<module>")`, short event message, details in `extra={...}`.
- New failure mode → alert in `observability/prometheus/rules.yml` + entry in `docs/RUNBOOK.md`.
- Keep README "Decisions & trade-offs", "What I'd do next", "Time log" updated as you go.
- Small commits with clear messages; `make lint test` before each commit.

## Definition of done
`make lint test` green · `make restart && make smoke` green · README sections filled · no TODO left in code paths.

## Don't
- Add infrastructure components the task doesn't need.
- `git push` unless asked. Never commit secrets.
