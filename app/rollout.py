"""Canary routing: the producer splits jobs between the stable and canary worker pools.

A job goes to canary when crc32(sandbox_id) % 100 < canary_weight. Routing is a pure
function of (id, weight): a retried request lands on the same pool, and raising the weight
only ever moves sandboxes *into* the canary, never back and forth. The weight lives in Redis
so every API replica routes the same way and it survives restarts; no key means 0.
"""

import asyncio
import logging
import uuid
import zlib

from arq.connections import ArqRedis

from app.models import Deployment

log = logging.getLogger("app.rollout")

WEIGHT_KEY = "rollout:canary_weight"


def bucket(sandbox_id: uuid.UUID) -> int:
    return zlib.crc32(sandbox_id.bytes) % 100


def route(sandbox_id: uuid.UUID, canary_weight: int) -> Deployment:
    return Deployment.CANARY if bucket(sandbox_id) < canary_weight else Deployment.STABLE


async def get_weight(redis: ArqRedis, timeout_s: float) -> int:
    async with asyncio.timeout(timeout_s):
        raw = await redis.get(WEIGHT_KEY)
    return min(max(int(raw), 0), 100) if raw is not None else 0


async def set_weight(redis: ArqRedis, weight: int, timeout_s: float) -> int:
    """Returns the previous weight. SET is idempotent, but callers get a fast failure rather
    than a retry: the operator (or the rollout loop) re-issues it."""
    async with asyncio.timeout(timeout_s):
        previous = await redis.set(WEIGHT_KEY, weight, get=True)
    return int(previous) if previous is not None else 0


async def pick_deployment(redis: ArqRedis, sandbox_id: uuid.UUID, timeout_s: float) -> Deployment:
    """Fail safe toward stable: if the weight can't be read, the canary gets nothing."""
    try:
        weight = await get_weight(redis, timeout_s)
    except Exception as exc:
        log.warning(
            "canary weight unavailable, routing to stable",
            extra={"sandbox_id": str(sandbox_id), "error": repr(exc)},
        )
        return Deployment.STABLE
    return route(sandbox_id, weight)
