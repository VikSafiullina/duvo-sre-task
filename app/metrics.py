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
# Producer side. outcome: enqueued | error (Redis down/slow => the sandbox is marked failed)
JOBS_ENQUEUED = Counter("jobs_enqueued_total", "Jobs handed to the queue", ["job", "outcome"])
# Consumer side. outcome: success | retry | failed
JOBS = Counter("jobs_total", "Background jobs by outcome", ["job", "outcome"])
JOB_LATENCY = Histogram(
    "job_duration_seconds",
    "Background job run time",
    ["job"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
QUEUE_DEPTH = Gauge("queue_depth", "Jobs waiting in the queue (sampled at scrape time)")
