"""arq worker — consumes background jobs from Redis.

Run: `python -m app.worker` (the container wraps it in `opentelemetry-instrument`).
We call `run_worker` ourselves instead of the `arq` CLI so the CLI's logging dictConfig
doesn't replace the handlers OpenTelemetry installed.
"""

import asyncio
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
from app.models import Item, ItemStatus
from app.observability import setup_logging
from app.queue import PROCESS_ITEM, redis_settings

log = logging.getLogger("app.worker")
tracer = trace.get_tracer("app.worker")


class ChaosError(RuntimeError):
    """Injected failure (CHAOS_FAILURE_RATE) to exercise retries, metrics and alerts."""


async def _do_work(item: Item, failure_rate: float) -> str:
    """The actual unit of work — replace this for the real task."""
    await asyncio.sleep(random.uniform(0.05, 0.3))
    if random.random() < failure_rate:
        raise ChaosError("injected failure")
    return f"processed {item.name}"


async def process_item(ctx: dict[str, Any], item_id: str, trace_ctx: dict[str, str]) -> str:
    settings = ctx["settings"]
    job_try: int = ctx["job_try"]
    parent = propagate.extract(trace_ctx)
    with tracer.start_as_current_span(PROCESS_ITEM, context=parent, kind=SpanKind.CONSUMER) as span:
        span.set_attribute("item.id", item_id)
        span.set_attribute("job.try", job_try)
        start = time.perf_counter()
        try:
            async with ctx["sessionmaker"]() as session:
                item = await session.get(Item, uuid.UUID(item_id))
                if item is None:
                    JOBS.labels(PROCESS_ITEM, "failed").inc()
                    log.warning("item missing", extra={"item_id": item_id})
                    return "missing"
                item.status, item.attempts = ItemStatus.PROCESSING, job_try
                await session.commit()
                try:
                    item.result = await _do_work(item, settings.chaos_failure_rate)
                except Exception as exc:
                    # Any failure (not only injected chaos) retries, then fails *visibly*:
                    # an item must never be left in "processing" or missing from jobs_total.
                    span.record_exception(exc)
                    await session.rollback()  # the work may have left the transaction unusable
                    error = {"item_id": item_id, "job_try": job_try, "error": type(exc).__name__}
                    if job_try < settings.job_max_tries:
                        item.status = ItemStatus.QUEUED
                        await session.commit()
                        JOBS.labels(PROCESS_ITEM, "retry").inc()
                        log.warning("job failed, retrying", extra=error)
                        # exponential backoff + jitter so retries don't synchronise
                        raise Retry(defer=2**job_try + random.uniform(0, 1)) from exc
                    item.status, item.result = ItemStatus.FAILED, str(exc)
                    await session.commit()
                    JOBS.labels(PROCESS_ITEM, "failed").inc()
                    log.error("job failed permanently", extra=error)
                    return "failed"
                item.status = ItemStatus.DONE
                await session.commit()
                JOBS.labels(PROCESS_ITEM, "success").inc()
                log.info("job done", extra={"item_id": item_id, "job_try": job_try})
                return "done"
        finally:
            JOB_LATENCY.labels(PROCESS_ITEM).observe(time.perf_counter() - start)


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
    functions = [process_item]  # noqa: RUF012 — arq reads class attributes
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
