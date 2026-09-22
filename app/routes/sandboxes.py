"""Sandbox lifecycle API: the producer side of the queue."""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.deps import QueueDep, SessionDep, SettingsDep
from app.metrics import JOBS_ENQUEUED
from app.models import Sandbox, SandboxStatus
from app.queue import START_SANDBOX, enqueue_start_sandbox, start_sandbox_job_id
from app.schemas import SandboxAccepted, SandboxCreate, SandboxOut

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])
log = logging.getLogger("app.sandboxes")


IdempotencyKey = Annotated[
    str | None, Header(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
]


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=SandboxAccepted)
async def create_sandbox(
    body: SandboxCreate,
    session: SessionDep,
    queue: QueueDep,
    settings: SettingsDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> SandboxAccepted:
    """Record the sandbox *before* enqueueing so a fast worker always finds its row. If the
    queue is unreachable the row is marked failed (never left looking "queued" forever) and
    the caller gets a 503 to retry.

    With an `Idempotency-Key`, a retry (client timeout, 503, concurrent duplicate) returns the
    original sandbox instead of starting another container. The unique constraint picks the
    winner, so there is no check-then-insert race. A replayed `failed` sandbox stays failed:
    use a new key to try again."""
    sandbox = Sandbox(type=body.type, status=SandboxStatus.QUEUED, idempotency_key=idempotency_key)
    session.add(sandbox)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        if idempotency_key is None:
            raise
        original = await session.scalar(
            select(Sandbox).where(Sandbox.idempotency_key == idempotency_key)
        )
        if original is None:
            raise
        JOBS_ENQUEUED.labels(START_SANDBOX, "deduplicated").inc()
        log.info("idempotent replay", extra={"sandbox_id": str(original.id)})
        response.headers["Idempotent-Replayed"] = "true"
        return SandboxAccepted(
            job_id=start_sandbox_job_id(original.id),
            sandbox_id=original.id,
            type=original.type,
            status=original.status,
        )
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
