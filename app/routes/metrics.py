import asyncio
import logging
import time

from fastapi import APIRouter, Request, Response
from opentelemetry.instrumentation.utils import suppress_instrumentation
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.metrics import QUEUE_DEPTH, ROLLOUT_CANARY_WEIGHT
from app.queue import QUEUES
from app.rollout import get_weight

router = APIRouter(tags=["ops"])
log = logging.getLogger("app.metrics")


async def _sample_queues(request: Request) -> None:
    """Rollout state lives in Redis, so it's sampled here, as fresh as the scrape."""
    queue, timeout_s = request.app.state.queue, request.app.state.settings.redis_timeout_s
    now_ms = int(time.time() * 1000)  # arq scores jobs by when they're due, in ms
    async with asyncio.timeout(timeout_s):
        for deployment, name in QUEUES.items():
            QUEUE_DEPTH.labels(deployment).set(await queue.zcount(name, "-inf", now_ms))
    ROLLOUT_CANARY_WEIGHT.set(await get_weight(queue, timeout_s))


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    try:
        with suppress_instrumentation():  # a trace per scrape would drown real traces
            await _sample_queues(request)
    except Exception as exc:
        # Scrape must still succeed; Redis health is reported by /readyz and its own alert.
        log.warning("queue sampling failed", extra={"error": repr(exc)})
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
