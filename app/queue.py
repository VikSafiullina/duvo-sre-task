"""Queue plumbing shared by the API (producer) and the worker (consumer)."""

import uuid

from arq.connections import ArqRedis, RedisSettings
from arq.jobs import Job
from opentelemetry import propagate

from app.config import Settings

PROCESS_ITEM = "process_item"


def redis_settings(settings: Settings) -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url)


def process_item_job_id(item_id: uuid.UUID) -> str:
    return f"{PROCESS_ITEM}:{item_id}"


async def enqueue_process_item(queue: ArqRedis, item_id: uuid.UUID) -> Job | None:
    """Deterministic job id => a duplicate request while the job is queued or running is a
    no-op (arq returns None). Trace context rides in the job args so the worker span joins
    the API request's trace."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return await queue.enqueue_job(
        PROCESS_ITEM, str(item_id), carrier, _job_id=process_item_job_id(item_id)
    )
