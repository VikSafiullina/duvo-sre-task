"""Queue plumbing shared by the API (producer) and the worker (consumer)."""

import asyncio
import uuid

from arq.connections import ArqRedis, RedisSettings
from arq.jobs import Job
from opentelemetry import propagate

from app.config import Settings

START_SANDBOX = "start_sandbox"


def redis_settings(settings: Settings) -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url)


def start_sandbox_job_id(sandbox_id: uuid.UUID) -> str:
    return f"{START_SANDBOX}:{sandbox_id}"


async def enqueue_start_sandbox(
    queue: ArqRedis, sandbox_id: uuid.UUID, timeout_s: float
) -> Job | None:
    """Deterministic job id => a duplicate enqueue while the job is queued or running is a
    no-op (arq returns None). Trace context rides in the job args so the worker span joins
    the API request's trace. arq only sets a *connect* timeout, so we bound the call here."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    async with asyncio.timeout(timeout_s):
        return await queue.enqueue_job(
            START_SANDBOX, str(sandbox_id), carrier, _job_id=start_sandbox_job_id(sandbox_id)
        )
