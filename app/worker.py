"""arq worker — consumes sandbox jobs from Redis.

Run: `python -m app.worker` (the container wraps it in `opentelemetry-instrument`).
We call `run_worker` ourselves instead of the `arq` CLI so the CLI's logging dictConfig
doesn't replace the handlers OpenTelemetry installed.
"""

import logging
import random
import time
import uuid
from typing import Any

from arq import Retry
from arq.worker import run_worker
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind
from prometheus_client import start_http_server

from app.config import get_settings
from app.db import make_engine, make_sessionmaker
from app.metrics import JOB_LATENCY, JOBS
from app.models import Sandbox, SandboxStatus
from app.observability import setup_logging
from app.queue import START_SANDBOX, redis_settings

log = logging.getLogger("app.worker")
tracer = trace.get_tracer("app.worker")

# Statuses in which (re)running the start job is meaningful. A redelivery for a sandbox
# that already reached running/stopped/failed must not start it a second time.
_STARTABLE = {SandboxStatus.QUEUED, SandboxStatus.STARTING}


class ChaosError(RuntimeError):
    """Injected failure (CHAOS_FAILURE_RATE) to exercise retries, metrics and alerts."""


async def _launch(sandbox: Sandbox, failure_rate: float) -> str | None:
    """Bring the sandbox up and return the URL it serves on. Step 1 of the plan only picks
    the job up and logs it (no URL); the container launch lands here next."""
    if random.random() < failure_rate:
        raise ChaosError("injected failure")
    return None


async def start_sandbox(ctx: dict[str, Any], sandbox_id: str, trace_ctx: dict[str, str]) -> str:
    settings = ctx["settings"]
    job_try: int = ctx["job_try"]
    parent = propagate.extract(trace_ctx)
    span_ctx = tracer.start_as_current_span(START_SANDBOX, context=parent, kind=SpanKind.CONSUMER)
    with span_ctx as span:
        span.set_attribute("sandbox.id", sandbox_id)
        span.set_attribute("job.try", job_try)
        start = time.perf_counter()
        try:
            async with ctx["sessionmaker"]() as session:
                sandbox = await session.get(Sandbox, uuid.UUID(sandbox_id))
                if sandbox is None:
                    JOBS.labels(START_SANDBOX, "failed").inc()
                    log.warning("sandbox missing", extra={"sandbox_id": sandbox_id})
                    return "missing"
                if sandbox.status not in _STARTABLE:
                    log.info(
                        "sandbox already settled, skipping",
                        extra={"sandbox_id": sandbox_id, "status": sandbox.status},
                    )
                    return sandbox.status.value
                sandbox.status, sandbox.attempts = SandboxStatus.STARTING, job_try
                await session.commit()
                log.info(
                    "sandbox job picked up",
                    extra={"sandbox_id": sandbox_id, "type": sandbox.type, "job_try": job_try},
                )
                try:
                    url = await _launch(sandbox, settings.chaos_failure_rate)
                except Exception as exc:
                    # Any failure (not only injected chaos) retries, then fails *visibly*:
                    # a sandbox must never be left in "starting" or missing from jobs_total.
                    span.record_exception(exc)
                    await session.rollback()  # the work may have left the transaction unusable
                    reason = f"{type(exc).__name__}: {exc}"[:500]
                    error = {"sandbox_id": sandbox_id, "job_try": job_try, "error": reason}
                    if job_try < settings.job_max_tries:
                        sandbox.status, sandbox.error = SandboxStatus.QUEUED, reason
                        await session.commit()
                        JOBS.labels(START_SANDBOX, "retry").inc()
                        log.warning("sandbox start failed, retrying", extra=error)
                        # exponential backoff + jitter so retries don't synchronise
                        raise Retry(defer=2**job_try + random.uniform(0, 1)) from exc
                    sandbox.status, sandbox.error = SandboxStatus.FAILED, reason
                    await session.commit()
                    JOBS.labels(START_SANDBOX, "failed").inc()
                    log.error("sandbox start failed permanently", extra=error)
                    return "failed"
                sandbox.status, sandbox.url, sandbox.error = SandboxStatus.RUNNING, url, None
                await session.commit()
                JOBS.labels(START_SANDBOX, "success").inc()
                log.info(
                    "sandbox running",
                    extra={"sandbox_id": sandbox_id, "url": url, "job_try": job_try},
                )
                return "running"
        finally:
            JOB_LATENCY.labels(START_SANDBOX).observe(time.perf_counter() - start)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    ctx["settings"] = settings
    ctx["engine"] = make_engine(settings)
    ctx["sessionmaker"] = make_sessionmaker(ctx["engine"])
    start_http_server(settings.worker_metrics_port)
    log.info("worker started", extra={"metrics_port": settings.worker_metrics_port})


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["engine"].dispose()
    log.info("worker stopped")


_settings = get_settings()


class WorkerSettings:
    functions = [start_sandbox]  # noqa: RUF012 — arq reads class attributes
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings(_settings)
    max_tries = _settings.job_max_tries
    job_timeout = _settings.job_timeout_s
    keep_result = 0  # no result retention => job-id dedupe window = queued/running only
    max_jobs = 10
    health_check_interval = 10


if __name__ == "__main__":
    run_worker(WorkerSettings)
