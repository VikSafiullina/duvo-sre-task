import asyncio
import uuid

from arq import create_pool
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.queue import enqueue_start_sandbox, redis_settings


def test_create_enqueues_one_job_and_counts_it(client: TestClient) -> None:
    assert client.post("/sandboxes", json={"type": "http"}).status_code == 202
    body = client.get("/metrics").text
    assert "queue_depth 1.0" in body
    assert 'jobs_enqueued_total{job="start_sandbox",outcome="enqueued"}' in body


async def test_enqueue_is_idempotent_per_sandbox(settings: Settings) -> None:
    queue = await create_pool(redis_settings(settings))
    try:
        await queue.flushdb()
        sandbox_id = uuid.uuid4()
        assert await enqueue_start_sandbox(queue, sandbox_id, timeout_s=1) is not None
        assert await enqueue_start_sandbox(queue, sandbox_id, timeout_s=1) is None
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
    assert 'jobs_enqueued_total{job="start_sandbox",outcome="error"}' in client.get("/metrics").text


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
