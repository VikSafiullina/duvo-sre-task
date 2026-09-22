"""Placeholder domain API — rename or replace for the real task."""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import QueueDep, SessionDep
from app.models import Item, ItemStatus
from app.queue import enqueue_process_item, process_item_job_id
from app.schemas import ItemCreate, ItemOut, JobAccepted

router = APIRouter(prefix="/items", tags=["items"])
log = logging.getLogger("app.items")


async def _get_or_404(session: AsyncSession, item_id: uuid.UUID) -> Item:
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "item not found")
    return item


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ItemOut)
async def create_item(body: ItemCreate, session: SessionDep) -> Item:
    item = Item(name=body.name)
    session.add(item)
    await session.commit()
    return item


@router.get("", response_model=list[ItemOut])
async def list_items(
    session: SessionDep, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> list[Item]:
    rows = await session.scalars(select(Item).order_by(Item.created_at.desc()).limit(limit))
    return list(rows)


@router.get("/{item_id}", response_model=ItemOut)
async def get_item(item_id: uuid.UUID, session: SessionDep) -> Item:
    return await _get_or_404(session, item_id)


@router.post("/{item_id}/process", status_code=status.HTTP_202_ACCEPTED, response_model=JobAccepted)
async def process_item(item_id: uuid.UUID, session: SessionDep, queue: QueueDep) -> JobAccepted:
    """Queue background processing. Status is written *before* enqueueing so a fast worker
    can never be overwritten by a late API write; if enqueueing fails we roll it back."""
    item = await _get_or_404(session, item_id)
    previous = item.status
    item.status = ItemStatus.QUEUED
    await session.commit()
    try:
        await enqueue_process_item(queue, item.id)
    except Exception:
        log.exception("enqueue failed", extra={"item_id": str(item.id)})
        item.status = previous
        await session.commit()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "queue unavailable, retry later"
        ) from None
    return JobAccepted(job_id=process_item_job_id(item.id), item_id=item.id, status=item.status)
