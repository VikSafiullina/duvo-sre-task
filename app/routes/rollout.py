"""Traffic split between the stable and canary worker pools (routing: app/rollout.py)."""

import logging

from fastapi import APIRouter, HTTPException, status

from app.deps import QueueDep, SettingsDep
from app.rollout import get_weight, set_weight
from app.schemas import RolloutOut, RolloutUpdate

router = APIRouter(prefix="/rollout", tags=["rollout"])
log = logging.getLogger("app.rollout")

_503 = HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "rollout state unavailable, retry later")


@router.get("", response_model=RolloutOut)
async def get_rollout(queue: QueueDep, settings: SettingsDep) -> RolloutOut:
    try:
        weight = await get_weight(queue, settings.redis_timeout_s)
    except Exception:
        log.exception("canary weight read failed")
        raise _503 from None
    return RolloutOut(canary_weight=weight)


@router.put("", response_model=RolloutOut)
async def put_rollout(body: RolloutUpdate, queue: QueueDep, settings: SettingsDep) -> RolloutOut:
    """Applies to jobs enqueued from now on; jobs already queued stay on their pool."""
    try:
        previous = await set_weight(queue, body.canary_weight, settings.redis_timeout_s)
    except Exception:
        log.exception("canary weight write failed", extra={"canary_weight": body.canary_weight})
        raise _503 from None
    log.info(
        "canary weight set",
        extra={"canary_weight": body.canary_weight, "previous_weight": previous},
    )
    return RolloutOut(canary_weight=body.canary_weight)
