"""arq worker — consumes sandbox jobs from Redis and drives the container runtime.

Run: `python -m app.worker` (the container wraps it in `opentelemetry-instrument`).
We call `run_worker` ourselves instead of the `arq` CLI so the CLI's logging dictConfig
doesn't replace the handlers OpenTelemetry installed.

Status changes are compare-and-set: the API (DELETE) and the reaper touch the same rows
concurrently, and a plain write could resurrect a sandbox someone just stopped. Every fast
path has the reaper (`reconcile_sandboxes`) as a safety net.
"""

import asyncio
import logging
import random
import time
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from arq import Retry, cron
from arq.worker import run_worker
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind
from prometheus_client import start_http_server
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import make_engine, make_sessionmaker
from app.metrics import JOB_LATENCY, JOBS, RECONCILE_RUNS, SANDBOXES_REAPED
from app.models import ACTIVE_STATUSES, Sandbox, SandboxStatus
from app.observability import setup_logging
from app.queue import START_SANDBOX, STOP_SANDBOX, redis_settings
from app.runtime import DockerRuntime, SandboxRuntime

log = logging.getLogger("app.worker")
tracer = trace.get_tracer("app.worker")

# Statuses in which (re)running the start job is meaningful. A redelivery for a sandbox
# that already reached running/stopped/failed must not start it a second time.
_STARTABLE = {SandboxStatus.QUEUED, SandboxStatus.STARTING}
# A container belonging to a sandbox in one of these statuses is a leak.
_NO_CONTAINER_WANTED = {SandboxStatus.STOPPING, SandboxStatus.STOPPED, SandboxStatus.FAILED}


class ChaosError(RuntimeError):
    """Injected failure (CHAOS_FAILURE_RATE) to exercise retries, metrics and alerts."""


async def _transition(
    session: AsyncSession, sandbox_id: uuid.UUID, from_: Iterable[SandboxStatus], **values: Any
) -> bool:
    """Compare-and-set on status; True if this call made the change."""
    result = await session.execute(
        update(Sandbox)
        .where(Sandbox.id == sandbox_id, Sandbox.status.in_(list(from_)))
        .values(**values)
    )
    await session.commit()
    return result.rowcount == 1  # type: ignore[attr-defined]


async def _discard(runtime: SandboxRuntime, sandbox_id: uuid.UUID) -> None:
    """Best-effort container removal; the reaper catches whatever this misses."""
    try:
        await runtime.remove(sandbox_id)
    except Exception as exc:
        log.warning(
            "container cleanup failed, reaper will retry",
            extra={"sandbox_id": str(sandbox_id), "error": repr(exc)},
        )


async def _launch(runtime: SandboxRuntime, sandbox: Sandbox, failure_rate: float) -> str:
    if random.random() < failure_rate:
        raise ChaosError("injected failure")
    return await runtime.launch(sandbox.id, sandbox.expires_at)


async def start_sandbox(ctx: dict[str, Any], sandbox_id: str, trace_ctx: dict[str, str]) -> str:
    settings, runtime = ctx["settings"], ctx["runtime"]
    job_try: int = ctx["job_try"]
    sid = uuid.UUID(sandbox_id)
    parent = propagate.extract(trace_ctx)
    span_ctx = tracer.start_as_current_span(START_SANDBOX, context=parent, kind=SpanKind.CONSUMER)
    with span_ctx as span:
        span.set_attribute("sandbox.id", sandbox_id)
        span.set_attribute("job.try", job_try)
        start = time.perf_counter()
        try:
            async with ctx["sessionmaker"]() as session:
                sandbox = await session.get(Sandbox, sid)
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
                if sandbox.expires_at <= datetime.now(UTC):
                    # Sat in the queue past its TTL: starting it now only wastes a slot.
                    await _transition(
                        session, sid, _STARTABLE, status=SandboxStatus.STOPPED, error="expired"
                    )
                    JOBS.labels(START_SANDBOX, "expired").inc()
                    log.warning("sandbox expired before start", extra={"sandbox_id": sandbox_id})
                    return "expired"
                if not await _transition(
                    session, sid, _STARTABLE, status=SandboxStatus.STARTING, attempts=job_try
                ):
                    return "cancelled"  # stopped between our read and our write
                log.info(
                    "sandbox job picked up",
                    extra={"sandbox_id": sandbox_id, "type": sandbox.type, "job_try": job_try},
                )
                try:
                    async with asyncio.timeout(settings.launch_timeout_s):
                        url = await _launch(runtime, sandbox, settings.chaos_failure_rate)
                except Exception as exc:
                    # Any failure (not only injected chaos) retries, then fails *visibly*:
                    # a sandbox must never be left in "starting" or missing from jobs_total.
                    span.record_exception(exc)
                    await _discard(runtime, sid)  # never carry a broken container into a retry
                    reason = f"{type(exc).__name__}: {exc}"[:500]
                    error = {"sandbox_id": sandbox_id, "job_try": job_try, "error": reason}
                    starting = {SandboxStatus.STARTING}
                    if job_try < settings.job_max_tries:
                        if not await _transition(
                            session, sid, starting, status=SandboxStatus.QUEUED, error=reason
                        ):
                            return "cancelled"
                        JOBS.labels(START_SANDBOX, "retry").inc()
                        log.warning("sandbox start failed, retrying", extra=error)
                        # exponential backoff + jitter so retries don't synchronise
                        raise Retry(defer=2**job_try + random.uniform(0, 1)) from exc
                    await _transition(
                        session, sid, starting, status=SandboxStatus.FAILED, error=reason
                    )
                    JOBS.labels(START_SANDBOX, "failed").inc()
                    log.error("sandbox start failed permanently", extra=error)
                    return "failed"
                if not await _transition(
                    session,
                    sid,
                    {SandboxStatus.STARTING},
                    status=SandboxStatus.RUNNING,
                    url=url,
                    error=None,
                ):
                    await _discard(runtime, sid)
                    log.info(
                        "sandbox stopped during start, container discarded",
                        extra={"sandbox_id": sandbox_id},
                    )
                    return "cancelled"
                JOBS.labels(START_SANDBOX, "success").inc()
                log.info(
                    "sandbox running",
                    extra={"sandbox_id": sandbox_id, "url": url, "job_try": job_try},
                )
                return "running"
        finally:
            JOB_LATENCY.labels(START_SANDBOX).observe(time.perf_counter() - start)


async def stop_sandbox(ctx: dict[str, Any], sandbox_id: str, trace_ctx: dict[str, str]) -> str:
    settings, runtime = ctx["settings"], ctx["runtime"]
    job_try: int = ctx["job_try"]
    sid = uuid.UUID(sandbox_id)
    parent = propagate.extract(trace_ctx)
    span_ctx = tracer.start_as_current_span(STOP_SANDBOX, context=parent, kind=SpanKind.CONSUMER)
    with span_ctx as span:
        span.set_attribute("sandbox.id", sandbox_id)
        start = time.perf_counter()
        try:
            try:
                removed = await runtime.remove(sid)  # idempotent => safe to retry
            except Exception as exc:
                span.record_exception(exc)
                error = {"sandbox_id": sandbox_id, "job_try": job_try, "error": repr(exc)}
                if job_try < settings.job_max_tries:
                    JOBS.labels(STOP_SANDBOX, "retry").inc()
                    log.warning("sandbox stop failed, retrying", extra=error)
                    raise Retry(defer=2**job_try + random.uniform(0, 1)) from exc
                JOBS.labels(STOP_SANDBOX, "failed").inc()
                log.error("sandbox stop failed permanently, reaper will retry", extra=error)
                return "failed"
            async with ctx["sessionmaker"]() as session:
                await _transition(
                    session, sid, {SandboxStatus.STOPPING}, status=SandboxStatus.STOPPED
                )
            JOBS.labels(STOP_SANDBOX, "success").inc()
            log.info("sandbox stopped", extra={"sandbox_id": sandbox_id, "removed": removed})
            return "stopped"
        finally:
            JOB_LATENCY.labels(STOP_SANDBOX).observe(time.perf_counter() - start)


async def reconcile_sandboxes(ctx: dict[str, Any]) -> dict[str, int]:
    """Converge what Docker runs with what Postgres says should run: enforce TTLs, remove
    containers leaked by crashes, finish stops whose job was lost, and fail sandboxes whose
    container died underneath them."""
    runtime: SandboxRuntime = ctx["runtime"]
    reaped = {"expired": 0, "orphan": 0, "vanished": 0}

    def count(reason: str, sandbox_id: uuid.UUID) -> None:
        reaped[reason] += 1
        SANDBOXES_REAPED.labels(reason).inc()
        log.info("sandbox reaped", extra={"sandbox_id": str(sandbox_id), "reason": reason})

    try:
        async with ctx["sessionmaker"]() as session:
            # Snapshot running rows *before* listing containers: a row running now had its
            # container created earlier, so "running but no container" is a real loss.
            running = set(
                await session.scalars(
                    select(Sandbox.id).where(Sandbox.status == SandboxStatus.RUNNING)
                )
            )
            await session.commit()
            containers = await runtime.list_sandboxes()
            live = {c.sandbox_id for c in containers}
            rows = await session.execute(
                select(Sandbox.id, Sandbox.status).where(Sandbox.id.in_(list(live)))
            )
            statuses = dict(rows.tuples().all())
            await session.commit()
            now = datetime.now(UTC)
            for c in containers:
                status = statuses.get(c.sandbox_id)
                if c.expires_at <= now:
                    reason = "expired"
                elif status is None or status in _NO_CONTAINER_WANTED:
                    reason = "orphan"
                else:
                    continue
                await runtime.remove(c.sandbox_id)
                await _transition(
                    session, c.sandbox_id, ACTIVE_STATUSES, status=SandboxStatus.STOPPED
                )
                count(reason, c.sandbox_id)
            for sid in running - live:
                if await _transition(
                    session,
                    sid,
                    {SandboxStatus.RUNNING},
                    status=SandboxStatus.FAILED,
                    error="container disappeared",
                ):
                    count("vanished", sid)
            # Stops whose job was lost (enqueue failed, Redis flushed): nothing left to remove.
            await session.execute(
                update(Sandbox)
                .where(Sandbox.status == SandboxStatus.STOPPING, Sandbox.id.not_in(list(live)))
                .values(status=SandboxStatus.STOPPED)
            )
            await session.commit()
    except Exception:
        RECONCILE_RUNS.labels("failed").inc()
        log.exception("reconcile failed")
        return reaped
    RECONCILE_RUNS.labels("success").inc()
    return reaped


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    ctx["settings"] = settings
    ctx["engine"] = make_engine(settings)
    ctx["sessionmaker"] = make_sessionmaker(ctx["engine"])
    ctx["runtime"] = await asyncio.to_thread(DockerRuntime, settings)  # blocking daemon ping
    await ctx["runtime"].prepare()
    start_http_server(settings.worker_metrics_port)
    log.info("worker started", extra={"metrics_port": settings.worker_metrics_port})


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["engine"].dispose()
    log.info("worker stopped")


_settings = get_settings()


class WorkerSettings:
    functions = [start_sandbox, stop_sandbox]  # noqa: RUF012 — arq reads class attributes
    cron_jobs = [  # noqa: RUF012
        cron(
            reconcile_sandboxes,
            second=set(range(0, 60, _settings.reconcile_interval_s)),
            run_at_startup=True,
            max_tries=1,  # the next sweep is the retry
        )
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings(_settings)
    max_tries = _settings.job_max_tries
    job_timeout = _settings.job_timeout_s  # backstop only: launch_timeout_s fires first
    # SIGTERM (deploy, scale-in): stop picking jobs and let in-flight ones finish instead of
    # cancelling them mid-launch. Jobs still running after the wait are re-run later.
    job_completion_wait = _settings.worker_drain_s
    keep_result = 0  # no result retention => job-id dedupe window = queued/running only
    max_jobs = 10
    health_check_interval = 10


if __name__ == "__main__":
    run_worker(WorkerSettings)
