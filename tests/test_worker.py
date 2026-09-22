import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from arq import Retry
from prometheus_client import REGISTRY

from app.config import Settings
from app.db import make_engine, make_sessionmaker
from app.models import Sandbox, SandboxStatus, SandboxType
from app.runtime import SandboxNotReady
from app.worker import reconcile_sandboxes, start_sandbox, stop_sandbox
from tests.support import FakeRuntime, reset_state

S = SandboxStatus


@pytest.fixture
async def ctx(settings: Settings) -> AsyncIterator[dict[str, Any]]:
    await reset_state(settings)
    engine = make_engine(settings)
    yield {
        "settings": settings,
        "sessionmaker": make_sessionmaker(engine),
        "runtime": FakeRuntime(),
        "job_try": 1,
    }
    await engine.dispose()


def _in(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


async def _add(ctx: dict[str, Any], status: S = S.QUEUED, ttl_s: int = 600) -> uuid.UUID:
    async with ctx["sessionmaker"]() as s:
        sandbox = Sandbox(type=SandboxType.HTTP, status=status, expires_at=_in(ttl_s))
        s.add(sandbox)
        await s.commit()
        return sandbox.id


async def _get(ctx: dict[str, Any], sandbox_id: uuid.UUID) -> Sandbox:
    async with ctx["sessionmaker"]() as s:
        sandbox = await s.get(Sandbox, sandbox_id)
        assert sandbox is not None
        return sandbox


async def _set_status(ctx: dict[str, Any], sandbox_id: uuid.UUID, status: S) -> None:
    async with ctx["sessionmaker"]() as s:
        sandbox = await s.get(Sandbox, sandbox_id)
        assert sandbox is not None
        sandbox.status = status
        await s.commit()


# --- start_sandbox ---------------------------------------------------------------------


async def test_start_launches_container_and_records_url(ctx: dict[str, Any]) -> None:
    sid = await _add(ctx)
    assert await start_sandbox(ctx, str(sid), {}) == "running"
    sandbox = await _get(ctx, sid)
    assert (sandbox.status, sandbox.url, sandbox.attempts, sandbox.error) == (
        S.RUNNING,
        "http://localhost:49153",
        1,
        None,
    )
    assert sid in ctx["runtime"].containers


async def test_start_retries_then_fails_permanently(ctx: dict[str, Any]) -> None:
    ctx["settings"] = ctx["settings"].model_copy(update={"chaos_failure_rate": 1.0})
    sid = await _add(ctx)
    with pytest.raises(Retry):
        await start_sandbox(ctx, str(sid), {})
    sandbox = await _get(ctx, sid)
    assert (sandbox.status, sandbox.error) == (S.QUEUED, "ChaosError: injected failure")
    ctx["job_try"] = ctx["settings"].job_max_tries
    assert await start_sandbox(ctx, str(sid), {}) == "failed"
    assert (await _get(ctx, sid)).status == S.FAILED


async def test_failed_launch_discards_container_before_retry(ctx: dict[str, Any]) -> None:
    """A half-started container must not survive into the retry (or leak on final failure)."""
    sid = await _add(ctx)
    ctx["runtime"].containers[sid] = _in(600)  # what the failed attempt left behind
    ctx["runtime"].launch_error = SandboxNotReady("no answer within 15s")
    with pytest.raises(Retry):
        await start_sandbox(ctx, str(sid), {})
    assert ctx["runtime"].containers == {}
    ctx["job_try"] = ctx["settings"].job_max_tries
    assert await start_sandbox(ctx, str(sid), {}) == "failed"
    assert (await _get(ctx, sid)).error == "SandboxNotReady: no answer within 15s"


async def test_success_after_retry_clears_error(ctx: dict[str, Any]) -> None:
    sid = await _add(ctx)
    ctx["runtime"].launch_error = SandboxNotReady("slow")
    with pytest.raises(Retry):
        await start_sandbox(ctx, str(sid), {})
    ctx["runtime"].launch_error, ctx["job_try"] = None, 2
    assert await start_sandbox(ctx, str(sid), {}) == "running"
    sandbox = await _get(ctx, sid)
    assert (sandbox.status, sandbox.attempts, sandbox.error) == (S.RUNNING, 2, None)


@pytest.mark.parametrize("status", [S.RUNNING, S.STOPPING, S.STOPPED, S.FAILED])
async def test_redelivery_of_settled_sandbox_is_noop(ctx: dict[str, Any], status: S) -> None:
    """At-least-once delivery: a repeat job must not launch the sandbox a second time."""
    sid = await _add(ctx, status)
    assert await start_sandbox(ctx, str(sid), {}) == status.value
    assert ctx["runtime"].containers == {}
    assert (await _get(ctx, sid)).attempts == 0


async def test_sandbox_expired_in_queue_is_not_started(ctx: dict[str, Any]) -> None:
    sid = await _add(ctx, ttl_s=-1)
    assert await start_sandbox(ctx, str(sid), {}) == "expired"
    assert (await _get(ctx, sid)).status == S.STOPPED
    assert ctx["runtime"].containers == {}


async def test_stop_during_launch_discards_the_new_container(ctx: dict[str, Any]) -> None:
    """DELETE lands while the container is coming up: the job must not flip it to running."""
    sid = await _add(ctx)

    async def user_deletes(sandbox_id: uuid.UUID) -> None:
        await _set_status(ctx, sandbox_id, S.STOPPING)

    ctx["runtime"].during_launch = user_deletes
    assert await start_sandbox(ctx, str(sid), {}) == "cancelled"
    assert (await _get(ctx, sid)).status == S.STOPPING
    assert ctx["runtime"].containers == {}


async def test_start_for_missing_sandbox_is_noop(ctx: dict[str, Any]) -> None:
    assert await start_sandbox(ctx, str(uuid.uuid4()), {}) == "missing"


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_start_records_time_to_running(ctx: dict[str, Any]) -> None:
    before = _sample("sandbox_time_to_running_seconds_count")
    await start_sandbox(ctx, str(await _add(ctx)), {})
    assert _sample("sandbox_time_to_running_seconds_count") == before + 1


async def test_queue_wait_measured_on_first_try_only(ctx: dict[str, Any]) -> None:
    """Retries wait on purpose (backoff): counting them would fake queue pressure."""
    count = ("job_queue_wait_seconds_count", {"task": "start_sandbox"})
    total = ("job_queue_wait_seconds_sum", {"task": "start_sandbox"})
    before_n, before_sum = _sample(count[0], **count[1]), _sample(total[0], **total[1])
    ctx["enqueue_time"] = datetime.now(UTC) - timedelta(seconds=3)
    await start_sandbox(ctx, str(await _add(ctx)), {})
    assert _sample(count[0], **count[1]) == before_n + 1
    assert _sample(total[0], **total[1]) - before_sum >= 3
    ctx["job_try"] = 2
    await start_sandbox(ctx, str(await _add(ctx)), {})
    assert _sample(count[0], **count[1]) == before_n + 1


# --- stop_sandbox ----------------------------------------------------------------------


async def test_stop_removes_container_and_marks_stopped(ctx: dict[str, Any]) -> None:
    sid = await _add(ctx, S.STOPPING)
    ctx["runtime"].containers[sid] = _in(600)
    assert await stop_sandbox(ctx, str(sid), {}) == "stopped"
    assert (await _get(ctx, sid)).status == S.STOPPED
    assert ctx["runtime"].containers == {}


async def test_stop_retries_when_docker_fails(ctx: dict[str, Any]) -> None:
    sid = await _add(ctx, S.STOPPING)
    ctx["runtime"].remove_error = TimeoutError()
    with pytest.raises(Retry):
        await stop_sandbox(ctx, str(sid), {})
    ctx["job_try"] = ctx["settings"].job_max_tries
    assert await stop_sandbox(ctx, str(sid), {}) == "failed"
    assert (await _get(ctx, sid)).status == S.STOPPING  # left for the reaper, not lost


# --- reconcile_sandboxes ---------------------------------------------------------------


async def test_reaper_converges_docker_and_db(ctx: dict[str, Any]) -> None:
    rt: FakeRuntime = ctx["runtime"]
    healthy = await _add(ctx, S.RUNNING)
    rt.containers[healthy] = _in(600)
    expired = await _add(ctx, S.RUNNING)
    rt.containers[expired] = _in(-1)
    orphan_failed = await _add(ctx, S.FAILED)
    rt.containers[orphan_failed] = _in(600)
    orphan_unknown = uuid.uuid4()  # container whose row doesn't exist at all
    rt.containers[orphan_unknown] = _in(600)
    vanished = await _add(ctx, S.RUNNING)  # row says running, container is gone
    lost_stop = await _add(ctx, S.STOPPING)  # stop job lost, container already gone

    assert await reconcile_sandboxes(ctx) == {"expired": 1, "orphan": 2, "vanished": 1}

    assert set(rt.containers) == {healthy}
    assert (await _get(ctx, healthy)).status == S.RUNNING
    assert (await _get(ctx, expired)).status == S.STOPPED
    assert (await _get(ctx, orphan_failed)).status == S.FAILED  # terminal rows are kept
    gone = await _get(ctx, vanished)
    assert (gone.status, gone.error) == (S.FAILED, "container disappeared")
    assert (await _get(ctx, lost_stop)).status == S.STOPPED


async def test_reaper_leaves_starting_sandboxes_alone(ctx: dict[str, Any]) -> None:
    """A container whose row is still `starting` is mid-launch, not an orphan."""
    sid = await _add(ctx, S.STARTING)
    ctx["runtime"].containers[sid] = _in(600)
    assert await reconcile_sandboxes(ctx) == {"expired": 0, "orphan": 0, "vanished": 0}
    assert sid in ctx["runtime"].containers


async def test_reaper_failure_is_contained(ctx: dict[str, Any]) -> None:
    ctx["runtime"].list_error = ConnectionError("docker down")
    assert await reconcile_sandboxes(ctx) == {"expired": 0, "orphan": 0, "vanished": 0}
