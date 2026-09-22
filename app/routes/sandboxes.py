"""Sandbox lifecycle API: the producer side of the queue."""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.deps import QueueDep, SessionDep, SettingsDep
from app.metrics import JOBS_ENQUEUED
from app.models import Sandbox, SandboxStatus
from app.queue import START_SANDBOX, enqueue_start_sandbox, start_sandbox_job_id
from app.schemas import SandboxAccepted, SandboxCreate, SandboxOut

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])
log = logging.getLogger("app.sandboxes")


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=SandboxAccepted)
async def create_sandbox(
    body: SandboxCreate, session: SessionDep, queue: QueueDep, settings: SettingsDep
) -> SandboxAccepted:
    """Record the sandbox *before* enqueueing so a fast worker always finds its row. If the
    queue is unreachable the row is marked failed (never left looking "queued" forever) and
    the caller gets a 503 to retry."""
    sandbox = Sandbox(type=body.type, status=SandboxStatus.QUEUED)
    session.add(sandbox)
    await session.commit()
    job_id = start_sandbox_job_id(sandbox.id)
    try:
        await enqueue_start_sandbox(queue, sandbox.id, settings.redis_timeout_s)
    except Exception as exc:
        JOBS_ENQUEUED.labels(START_SANDBOX, "error").inc()
        log.exception("enqueue failed", extra={"sandbox_id": str(sandbox.id), "job_id": job_id})
        sandbox.status, sandbox.error = SandboxStatus.FAILED, f"enqueue: {type(exc).__name__}"
        await session.commit()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "queue unavailable, retry later"
        ) from None
    JOBS_ENQUEUED.labels(START_SANDBOX, "enqueued").inc()
    log.info(
        "sandbox requested",
        extra={"sandbox_id": str(sandbox.id), "job_id": job_id, "type": sandbox.type},
    )
    return SandboxAccepted(
        job_id=job_id, sandbox_id=sandbox.id, type=sandbox.type, status=sandbox.status
    )


@router.get("", response_model=list[SandboxOut])
async def list_sandboxes(
    session: SessionDep, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> list[Sandbox]:
    rows = await session.scalars(select(Sandbox).order_by(Sandbox.created_at.desc()).limit(limit))
    return list(rows)


@router.get("/{sandbox_id}", response_model=SandboxOut)
async def get_sandbox(sandbox_id: uuid.UUID, session: SessionDep) -> Sandbox:
    sandbox = await session.get(Sandbox, sandbox_id)
    if sandbox is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "sandbox not found")
    return sandbox
