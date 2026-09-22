"""Prometheus scrape endpoint. State that lives in Redis (queue depth) and Postgres (sandbox
lifecycle) is sampled here, at scrape time, so the numbers are exactly as fresh as the scrape
and no background poller can silently die."""

import asyncio
import logging
from datetime import UTC, datetime

from arq.constants import default_queue_name
from fastapi import APIRouter, Request, Response
from opentelemetry.instrumentation.utils import suppress_instrumentation
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select

from app.metrics import QUEUE_DEPTH, SANDBOX_CAPACITY, SANDBOX_OLDEST, SANDBOXES_ACTIVE
from app.models import ACTIVE_STATUSES, Sandbox

router = APIRouter(tags=["ops"])
log = logging.getLogger("app.metrics")

QUEUES = (default_queue_name,)


async def _sample_queues(request: Request) -> None:
    state = request.app.state
    async with asyncio.timeout(state.settings.redis_timeout_s):
        for queue in QUEUES:
            QUEUE_DEPTH.labels(queue).set(await state.queue.zcard(queue))


async def _sample_sandboxes(request: Request) -> None:
    state = request.app.state
    SANDBOX_CAPACITY.set(state.settings.sandbox_max_active)
    async with asyncio.timeout(state.settings.db_timeout_s), state.sessionmaker() as session:
        rows = await session.execute(
            select(Sandbox.status, func.count(), func.min(Sandbox.updated_at))
            .where(Sandbox.status.in_(ACTIVE_STATUSES))
            .group_by(Sandbox.status)
        )
        found = {status: (count, oldest) for status, count, oldest in rows.tuples()}
    now = datetime.now(UTC)
    for status in ACTIVE_STATUSES:  # explicit zeros: absent series would make alerts go blind
        count, oldest = found.get(status, (0, None))
        SANDBOXES_ACTIVE.labels(status.value).set(count)
        SANDBOX_OLDEST.labels(status.value).set((now - oldest).total_seconds() if oldest else 0)


_SAMPLERS = (
    (_sample_queues, (QUEUE_DEPTH,)),
    (_sample_sandboxes, (SANDBOXES_ACTIVE, SANDBOX_OLDEST)),
)


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    # A trace per scrape would drown real traces. Each sampler fails on its own and the scrape
    # still succeeds (dependency health is reported by /readyz and its own alerts). A failed
    # sampler clears its gauges: a gap reads "unknown", a frozen last value would read "fine".
    with suppress_instrumentation():
        for sampler, gauges in _SAMPLERS:
            try:
                await sampler(request)
            except Exception as exc:
                for gauge in gauges:
                    gauge.clear()
                log.warning(
                    "metric sampling failed",
                    extra={"sampler": sampler.__name__, "error": repr(exc)},
                )
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
