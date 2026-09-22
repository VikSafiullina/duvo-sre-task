"""Prometheus metrics. Labels are route templates / enums only — never IDs — so cardinality
stays bounded no matter what callers send. Never name a label `job` or `instance`: Prometheus
owns those and renames ours to `exported_job`, silently breaking every query on it."""

from prometheus_client import Counter, Gauge, Histogram

# --- API (RED) ---------------------------------------------------------------------------
HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

# --- Producer + queue ---------------------------------------------------------------------
# outcome: enqueued | error (Redis down/slow => the sandbox is marked failed)
JOBS_ENQUEUED = Counter("jobs_enqueued_total", "Jobs handed to the queue", ["task", "outcome"])
# Sampled by the API at scrape time. Depth alone can't tell "busy" from "stuck", hence the
# wait histogram and the oldest-in-status gauge below.
QUEUE_DEPTH = Gauge("queue_depth", "Jobs waiting in the queue", ["queue"])
JOB_QUEUE_WAIT = Histogram(
    "job_queue_wait_seconds",
    "Enqueue -> first pickup by a worker (first try only; retries are deliberately deferred)",
    ["task"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

# --- Consumer -----------------------------------------------------------------------------
# outcome: success | retry | failed | expired (sat in the queue past its TTL)
JOBS = Counter("jobs_total", "Background jobs by outcome", ["task", "outcome"])
JOB_LATENCY = Histogram(
    "job_duration_seconds",
    "Background job run time",
    ["task"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

# --- Sandbox lifecycle --------------------------------------------------------------------
# The freshness SLI: what an agent actually waits for, queue + container start + readiness.
TIME_TO_RUNNING = Histogram(
    "sandbox_time_to_running_seconds",
    "Sandbox request accepted -> sandbox serving HTTP",
    buckets=(0.5, 1, 2, 3, 5, 8, 13, 20, 30, 60, 120),
)
# Sampled from Postgres by the API at scrape time (the DB is the source of truth).
SANDBOXES_ACTIVE = Gauge("sandboxes_active", "Sandboxes per non-terminal status", ["status"])
# Works even when throughput is zero: a histogram can't show a job that never finishes.
SANDBOX_OLDEST = Gauge(
    "sandbox_oldest_in_status_seconds",
    "Time the longest-waiting sandbox has spent in its current status",
    ["status"],
)
SANDBOX_CAPACITY = Gauge("sandbox_capacity", "Admission cap on active sandboxes")
# reason: expired (TTL) | orphan (container, no live row) | vanished (running row, no container)
SANDBOXES_REAPED = Counter("sandboxes_reaped_total", "Sandboxes the reaper cleaned up", ["reason"])
# Kept out of jobs_total so a healthy sweep every 30s can't dilute the start-failure ratio.
RECONCILE_RUNS = Counter("sandbox_reconcile_runs_total", "Reaper sweeps by outcome", ["outcome"])
