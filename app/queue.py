"""Queue plumbing shared by the API (producer) and the worker (consumer)."""

import asyncio
import uuid

from arq.connections import ArqRedis, RedisSettings
from arq.constants import default_queue_name
from arq.jobs import Job
from opentelemetry import propagate

from app.config import Settings
from app.models import Deployment

START_SANDBOX = "start_sandbox"
STOP_SANDBOX = "stop_sandbox"

# One queue per worker pool. Stable keeps arq's default name, so jobs enqueued by the
# release before A/B existed are still consumed after the deploy instead of being stranded.
QUEUES = {
    Deployment.STABLE: default_queue_name,
    Deployment.CANARY: f"{default_queue_name}:canary",
}


def redis_settings(settings: Settings) -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url)


def sandbox_job_id(job: str, sandbox_id: uuid.UUID) -> str:
    return f"{job}:{sandbox_id}"


async def enqueue_sandbox_job(
    queue: ArqRedis,
    job: str,
    sandbox_id: uuid.UUID,
    timeout_s: float,
    deployment: Deployment = Deployment.STABLE,
) -> Job | None:
    """Deterministic job id => a duplicate enqueue while the job is queued or running is a
    no-op (arq returns None; the id is global, so this holds across both pools' queues).
    Trace context rides in the job args so the worker span joins the API request's trace.
    arq only sets a *connect* timeout, so we bound the call here."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    async with asyncio.timeout(timeout_s):
        return await queue.enqueue_job(
            job,
            str(sandbox_id),
            carrier,
            _job_id=sandbox_job_id(job, sandbox_id),
            _queue_name=QUEUES[deployment],
        )
