"""Sandbox lifecycle API: the producer side of the queue. The API never talks to Docker —
it records intent in Postgres and hands the work to the worker."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import QueueDep, SessionDep, SettingsDep
from app.metrics import JOBS_ENQUEUED
from app.models import ACTIVE_STATUSES, Sandbox, SandboxStatus
from app.queue import START_SANDBOX, STOP_SANDBOX, enqueue_sandbox_job, sandbox_job_id
from app.rollout import pick_deployment
from app.schemas import SandboxAccepted, SandboxCreate, SandboxOut

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])
log = logging.getLogger("app.sandboxes")

_503 = HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "queue unavailable, retry later")


async def _get_or_404(session: AsyncSession, sandbox_id: uuid.UUID) -> Sandbox:
    sandbox = await session.get(Sandbox, sandbox_id)
    if sandbox is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "sandbox not found")
    return sandbox


IdempotencyKey = Annotated[
    str | None, Header(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
]


def _accepted(sandbox: Sandbox) -> SandboxAccepted:
    return SandboxAccepted(
        job_id=sandbox_job_id(START_SANDBOX, sandbox.id),
        sandbox_id=sandbox.id,
        type=sandbox.type,
        status=sandbox.status,
        deployment=sandbox.deployment,
        expires_at=sandbox.expires_at,
    )


async def _replay(session: AsyncSession, key: str, response: Response) -> SandboxAccepted | None:
    """The sandbox an earlier request with this Idempotency-Key created, if any."""
    original = await session.scalar(select(Sandbox).where(Sandbox.idempotency_key == key))
    if original is None:
        return None
    JOBS_ENQUEUED.labels(START_SANDBOX, "deduplicated", original.deployment).inc()
    log.info("idempotent replay", extra={"sandbox_id": str(original.id)})
    response.headers["Idempotent-Replayed"] = "true"
    return _accepted(original)


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
    original sandbox instead of starting another container, even at capacity (the original
    already holds its slot). The unique constraint settles concurrent duplicates. A replayed
    `failed` sandbox stays failed: use a new key to try again."""
    if idempotency_key and (replay := await _replay(session, idempotency_key, response)):
        return replay
    active = await session.scalar(
        select(func.count()).select_from(Sandbox).where(Sandbox.status.in_(ACTIVE_STATUSES))
    )
    if (active or 0) >= settings.sandbox_max_active:
        log.warning("sandbox cap reached", extra={"active": active})
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"sandbox capacity reached ({settings.sandbox_max_active}), retry later",
            headers={"Retry-After": "30"},
        )
    sandbox_id = uuid.uuid4()  # routing is keyed on the id, so it's chosen up front
    deployment = await pick_deployment(queue, sandbox_id, settings.redis_timeout_s)
    expires_at = datetime.now(UTC) + timedelta(seconds=body.ttl_s)
    sandbox = Sandbox(
        id=sandbox_id,
        type=body.type,
        status=SandboxStatus.QUEUED,
        deployment=deployment,
        expires_at=expires_at,
        idempotency_key=idempotency_key,
    )
    session.add(sandbox)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()  # a concurrent request with the same key won the insert
        if idempotency_key and (replay := await _replay(session, idempotency_key, response)):
            return replay
        raise
    job_id = sandbox_job_id(START_SANDBOX, sandbox.id)
    ids = {"sandbox_id": str(sandbox.id), "job_id": job_id, "deployment": deployment}
    try:
        await enqueue_sandbox_job(
            queue, START_SANDBOX, sandbox.id, settings.redis_timeout_s, deployment
        )
    except Exception as exc:
        JOBS_ENQUEUED.labels(START_SANDBOX, "error", deployment).inc()
        log.exception("enqueue failed", extra=ids)
        sandbox.status, sandbox.error = SandboxStatus.FAILED, f"enqueue: {type(exc).__name__}"
        await session.commit()
        raise _503 from None
    JOBS_ENQUEUED.labels(START_SANDBOX, "enqueued", deployment).inc()
    log.info("sandbox requested", extra={**ids, "type": sandbox.type})
    return _accepted(sandbox)


@router.get("", response_model=list[SandboxOut])
async def list_sandboxes(
    session: SessionDep, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> list[Sandbox]:
    rows = await session.scalars(select(Sandbox).order_by(Sandbox.created_at.desc()).limit(limit))
    return list(rows)


@router.get("/{sandbox_id}", response_model=SandboxOut)
async def get_sandbox(sandbox_id: uuid.UUID, session: SessionDep) -> Sandbox:
    return await _get_or_404(session, sandbox_id)


@router.delete("/{sandbox_id}", status_code=status.HTTP_202_ACCEPTED, response_model=SandboxOut)
async def stop_sandbox(
    sandbox_id: uuid.UUID, session: SessionDep, queue: QueueDep, settings: SettingsDep
) -> Sandbox:
    """Idempotent. Stopped/failed sandboxes come back unchanged. Otherwise the row moves to
    `stopping` (compare-and-set, so it can't clobber a concurrent worker transition) and the
    worker removes the container. If enqueueing fails the row stays `stopping`: the reaper
    finishes the stop within one sweep, and a retried DELETE re-enqueues the fast path."""
    sandbox = await _get_or_404(session, sandbox_id)
    if sandbox.status not in ACTIVE_STATUSES:
        return sandbox
    await session.execute(
        update(Sandbox)
        .where(Sandbox.id == sandbox_id, Sandbox.status.in_(ACTIVE_STATUSES))
        .values(status=SandboxStatus.STOPPING)
    )
    await session.commit()
    await session.refresh(sandbox)
    if sandbox.status != SandboxStatus.STOPPING:
        return sandbox  # settled some other way in between
    # Routed by the *current* weight, not the pool that started it: after a rollback, stops
    # go to stable at once. Any pool can remove any sandbox (same Docker host, same labels).
    deployment = await pick_deployment(queue, sandbox.id, settings.redis_timeout_s)
    job_id = sandbox_job_id(STOP_SANDBOX, sandbox.id)
    ids = {"sandbox_id": str(sandbox.id), "job_id": job_id, "deployment": deployment}
    try:
        await enqueue_sandbox_job(
            queue, STOP_SANDBOX, sandbox.id, settings.redis_timeout_s, deployment
        )
    except Exception:
        JOBS_ENQUEUED.labels(STOP_SANDBOX, "error", deployment).inc()
        log.exception("enqueue failed", extra=ids)
        raise _503 from None
    JOBS_ENQUEUED.labels(STOP_SANDBOX, "enqueued", deployment).inc()
    log.info("sandbox stop requested", extra=ids)
    return sandbox
