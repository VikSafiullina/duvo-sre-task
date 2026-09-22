import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from arq import Retry

from app.config import Settings
from app.db import make_engine, make_sessionmaker
from app.models import Sandbox, SandboxStatus, SandboxType
from app.worker import start_sandbox
from tests.support import reset_state


@pytest.fixture
async def ctx(settings: Settings) -> AsyncIterator[dict[str, Any]]:
    await reset_state(settings)
    engine = make_engine(settings)
    yield {"settings": settings, "sessionmaker": make_sessionmaker(engine), "job_try": 1}
    await engine.dispose()


async def _add_sandbox(ctx: dict[str, Any], status: SandboxStatus = SandboxStatus.QUEUED) -> str:
    async with ctx["sessionmaker"]() as s:
        sandbox = Sandbox(type=SandboxType.HTTP, status=status)
        s.add(sandbox)
        await s.commit()
        return str(sandbox.id)


async def _get(ctx: dict[str, Any], sandbox_id: str) -> Sandbox:
    async with ctx["sessionmaker"]() as s:
        sandbox = await s.get(Sandbox, uuid.UUID(sandbox_id))
        assert sandbox is not None
        return sandbox


def _always_fail(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    async def broken(sandbox: Sandbox, failure_rate: float) -> str:
        raise exc

    monkeypatch.setattr("app.worker._launch", broken)


async def test_job_marks_sandbox_running(ctx: dict[str, Any]) -> None:
    sandbox_id = await _add_sandbox(ctx)
    assert await start_sandbox(ctx, sandbox_id, {}) == "running"
    sandbox = await _get(ctx, sandbox_id)
    assert (sandbox.status, sandbox.attempts, sandbox.error) == (SandboxStatus.RUNNING, 1, None)


async def test_job_retries_then_fails_permanently(ctx: dict[str, Any]) -> None:
    ctx["settings"] = ctx["settings"].model_copy(update={"chaos_failure_rate": 1.0})
    sandbox_id = await _add_sandbox(ctx)
    with pytest.raises(Retry):
        await start_sandbox(ctx, sandbox_id, {})
    sandbox = await _get(ctx, sandbox_id)
    assert sandbox.status == SandboxStatus.QUEUED
    assert sandbox.error == "ChaosError: injected failure"  # why it is retrying is visible
    ctx["job_try"] = ctx["settings"].job_max_tries
    assert await start_sandbox(ctx, sandbox_id, {}) == "failed"
    assert (await _get(ctx, sandbox_id)).status == SandboxStatus.FAILED


async def test_unexpected_error_retries_then_fails_visibly(
    ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real bug in the launch (not the injected chaos) must never leave a sandbox stuck."""
    _always_fail(monkeypatch, RuntimeError("docker exploded"))
    sandbox_id = await _add_sandbox(ctx)
    with pytest.raises(Retry):
        await start_sandbox(ctx, sandbox_id, {})
    assert (await _get(ctx, sandbox_id)).status == SandboxStatus.QUEUED
    ctx["job_try"] = ctx["settings"].job_max_tries
    assert await start_sandbox(ctx, sandbox_id, {}) == "failed"
    sandbox = await _get(ctx, sandbox_id)
    assert sandbox.status == SandboxStatus.FAILED
    assert sandbox.error == "RuntimeError: docker exploded"


async def test_success_after_retry_clears_error(ctx: dict[str, Any]) -> None:
    ctx["settings"] = ctx["settings"].model_copy(update={"chaos_failure_rate": 1.0})
    sandbox_id = await _add_sandbox(ctx)
    with pytest.raises(Retry):
        await start_sandbox(ctx, sandbox_id, {})
    ctx["settings"] = ctx["settings"].model_copy(update={"chaos_failure_rate": 0.0})
    ctx["job_try"] = 2
    assert await start_sandbox(ctx, sandbox_id, {}) == "running"
    sandbox = await _get(ctx, sandbox_id)
    assert (sandbox.status, sandbox.attempts, sandbox.error) == (SandboxStatus.RUNNING, 2, None)


@pytest.mark.parametrize("status", [SandboxStatus.RUNNING, SandboxStatus.FAILED])
async def test_redelivery_of_settled_sandbox_is_noop(
    ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch, status: SandboxStatus
) -> None:
    """At-least-once delivery: a repeat job must not launch the sandbox a second time."""
    _always_fail(monkeypatch, AssertionError("launch must not be called"))
    sandbox_id = await _add_sandbox(ctx, status)
    assert await start_sandbox(ctx, sandbox_id, {}) == status.value
    assert (await _get(ctx, sandbox_id)).attempts == 0


async def test_job_for_missing_sandbox_is_noop(ctx: dict[str, Any]) -> None:
    assert await start_sandbox(ctx, str(uuid.uuid4()), {}) == "missing"
