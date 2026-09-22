import asyncio
import uuid

from arq import create_pool
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.queue import START_SANDBOX, enqueue_sandbox_job, redis_settings
from tests.support import metric


def test_create_enqueues_one_job_and_counts_it(client: TestClient) -> None:
    assert client.post("/sandboxes", json={"type": "http"}).status_code == 202
    assert metric(client, "queue_depth", queue="arq:queue") == 1
    assert metric(client, "jobs_enqueued_total", task="start_sandbox", outcome="enqueued")


async def test_enqueue_is_idempotent_per_sandbox(settings: Settings) -> None:
    queue = await create_pool(redis_settings(settings))
    try:
        await queue.flushdb()
        sandbox_id = uuid.uuid4()
        assert await enqueue_sandbox_job(queue, START_SANDBOX, sandbox_id, 1) is not None
        assert await enqueue_sandbox_job(queue, START_SANDBOX, sandbox_id, 1) is None
    finally:
        await queue.flushdb()
        await queue.aclose()


def test_queue_down_returns_503_and_marks_sandbox_failed(client: TestClient, app: FastAPI) -> None:
    async def boom(*args: object, **kwargs: object) -> None:
        raise ConnectionError("redis down")

    app.state.queue.enqueue_job = boom
    assert client.post("/sandboxes", json={"type": "http"}).status_code == 503
    [sandbox] = client.get("/sandboxes").json()
    assert sandbox["status"] == "failed"
    assert sandbox["error"] == "enqueue: ConnectionError"
    assert metric(client, "jobs_enqueued_total", task="start_sandbox", outcome="error")


def test_slow_queue_times_out_instead_of_hanging(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    async def hang(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(5)

    app.state.settings = settings.model_copy(update={"redis_timeout_s": 0.05})
    app.state.queue.enqueue_job = hang
    assert client.post("/sandboxes", json={"type": "http"}).status_code == 503
    [sandbox] = client.get("/sandboxes").json()
    assert sandbox["error"] == "enqueue: TimeoutError"
