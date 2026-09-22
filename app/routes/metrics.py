import logging

from arq.constants import default_queue_name
from fastapi import APIRouter, Request, Response
from opentelemetry.instrumentation.utils import suppress_instrumentation
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.metrics import QUEUE_DEPTH

router = APIRouter(tags=["ops"])
log = logging.getLogger("app.metrics")


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    try:
        with suppress_instrumentation():  # a trace per scrape would drown real traces
            QUEUE_DEPTH.set(await request.app.state.queue.zcard(default_queue_name))
    except Exception:
        # Scrape must still succeed; Redis health is reported by /readyz and its own alert.
        log.warning("queue depth unavailable")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
