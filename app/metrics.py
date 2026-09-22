"""Prometheus metrics. Labels are route templates / enums only — never IDs or raw caller
input (unknown HTTP methods become OTHER) — so cardinality stays bounded no matter what
callers send."""

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
# Producer side. outcome: enqueued | error (Redis down/slow => the sandbox is marked failed)
# | deduplicated (Idempotency-Key replay: nothing enqueued; a spike = clients retrying)
JOBS_ENQUEUED = Counter("jobs_enqueued_total", "Jobs handed to the queue", ["job", "outcome"])
# Consumer side. outcome: success | retry | failed | expired (sat in the queue past its TTL)
JOBS = Counter("jobs_total", "Background jobs by outcome", ["job", "outcome"])
JOB_LATENCY = Histogram(
    "job_duration_seconds",
    "Background job run time",
    ["job"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
# reason: expired (TTL) | orphan (container, no live row) | vanished (running row, no container)
SANDBOXES_REAPED = Counter("sandboxes_reaped_total", "Sandboxes the reaper cleaned up", ["reason"])
# Kept out of jobs_total so a healthy sweep every 30s can't dilute the start-failure ratio.
RECONCILE_RUNS = Counter("sandbox_reconcile_runs_total", "Reaper sweeps by outcome", ["outcome"])
QUEUE_DEPTH = Gauge("queue_depth", "Jobs waiting in the queue (sampled at scrape time)")
