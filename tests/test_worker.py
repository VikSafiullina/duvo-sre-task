import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from arq import Retry

from app.config import Settings
from app.db import make_engine, make_sessionmaker
from app.models import Item, ItemStatus
from app.worker import process_item
from tests.support import reset_state


@pytest.fixture
async def ctx(settings: Settings) -> AsyncIterator[dict[str, Any]]:
    await reset_state(settings)
    engine = make_engine(settings)
    yield {"settings": settings, "sessionmaker": make_sessionmaker(engine), "job_try": 1}
    await engine.dispose()


async def _add_item(ctx: dict[str, Any]) -> uuid.UUID:
    async with ctx["sessionmaker"]() as s:
        item = Item(name="widget")
        s.add(item)
        await s.commit()
        return item.id


async def _status(ctx: dict[str, Any], item_id: uuid.UUID) -> ItemStatus:
    async with ctx["sessionmaker"]() as s:
        item = await s.get(Item, item_id)
        assert item is not None
        return item.status


async def test_job_marks_item_done(ctx: dict[str, Any]) -> None:
    item_id = await _add_item(ctx)
    assert await process_item(ctx, str(item_id), {}) == "done"
    assert await _status(ctx, item_id) == ItemStatus.DONE


async def test_job_retries_then_fails_permanently(ctx: dict[str, Any]) -> None:
    ctx["settings"] = ctx["settings"].model_copy(update={"chaos_failure_rate": 1.0})
    item_id = await _add_item(ctx)
    with pytest.raises(Retry):
        await process_item(ctx, str(item_id), {})
    assert await _status(ctx, item_id) == ItemStatus.QUEUED
    ctx["job_try"] = ctx["settings"].job_max_tries
    assert await process_item(ctx, str(item_id), {}) == "failed"
    assert await _status(ctx, item_id) == ItemStatus.FAILED


async def test_unexpected_error_retries_then_fails_visibly(
    ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real bug in the work (not the injected chaos) must never leave items stuck."""

    async def broken(item: Item, failure_rate: float) -> str:
        raise RuntimeError("downstream exploded")

    monkeypatch.setattr("app.worker._do_work", broken)
    item_id = await _add_item(ctx)
    with pytest.raises(Retry):
        await process_item(ctx, str(item_id), {})
    assert await _status(ctx, item_id) == ItemStatus.QUEUED
    ctx["job_try"] = ctx["settings"].job_max_tries
    assert await process_item(ctx, str(item_id), {}) == "failed"
    assert await _status(ctx, item_id) == ItemStatus.FAILED


async def test_job_for_missing_item_is_noop(ctx: dict[str, Any]) -> None:
    assert await process_item(ctx, str(uuid.uuid4()), {}) == "missing"
