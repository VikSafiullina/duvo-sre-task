"""Prometheus metrics. Labels are route templates / enums only — never IDs — so cardinality
stays bounded no matter what callers send."""

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
# `deployment` (stable | canary) is the worker pool: the label a rollout compares on.
# Producer side. outcome: enqueued | error (Redis down/slow => the sandbox is marked failed)
JOBS_ENQUEUED = Counter(
    "jobs_enqueued_total", "Jobs handed to the queue", ["job", "outcome", "deployment"]
)
# Consumer side. outcome: success | retry | failed | expired (sat in the queue past its TTL)
JOBS = Counter("jobs_total", "Background jobs by outcome", ["job", "outcome", "deployment"])
JOB_LATENCY = Histogram(
    "job_duration_seconds",
    "Background job run time",
    ["job", "deployment"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
# reason: expired (TTL) | orphan (container, no live row) | vanished (running row, no container)
SANDBOXES_REAPED = Counter("sandboxes_reaped_total", "Sandboxes the reaper cleaned up", ["reason"])
# Kept out of jobs_total so a healthy sweep every 30s can't dilute the start-failure ratio.
RECONCILE_RUNS = Counter("sandbox_reconcile_runs_total", "Reaper sweeps by outcome", ["outcome"])
# Sampled by the API at scrape time. Due jobs only: deferred retries and the next reaper tick
# also sit in the queue, and would make an idle pool look like it has a backlog.
QUEUE_DEPTH = Gauge("queue_depth", "Due jobs waiting for a worker, per pool", ["deployment"])
ROLLOUT_CANARY_WEIGHT = Gauge(
    "rollout_canary_weight", "Percent of new jobs routed to the canary pool (0-100)"
)
# Always 1: tells dashboards and the rollout loop which version each pool runs.
WORKER_INFO = Gauge("worker_info", "Worker pool and version", ["deployment", "version"])
