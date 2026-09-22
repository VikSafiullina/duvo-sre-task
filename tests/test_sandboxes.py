import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from tests.support import metric


def _create(client: TestClient, **body: object) -> str:
    r = client.post("/sandboxes", json={"type": "http", **body})
    assert r.status_code == 202, r.text
    return r.json()["sandbox_id"]


def test_create_accepts_and_get_returns_queued_sandbox(client: TestClient) -> None:
    r = client.post("/sandboxes", json={"type": "http"})
    assert r.status_code == 202
    accepted = r.json()
    sandbox_id = accepted["sandbox_id"]
    assert accepted["job_id"] == f"start_sandbox:{sandbox_id}"
    assert (accepted["type"], accepted["status"], accepted["deployment"]) == (
        "http",
        "queued",
        "stable",
    )
    sandbox = client.get(f"/sandboxes/{sandbox_id}").json()
    assert (sandbox["status"], sandbox["url"], sandbox["attempts"]) == ("queued", None, 0)


def test_ttl_defaults_to_ten_minutes_and_is_honoured(client: TestClient) -> None:
    default = client.post("/sandboxes", json={"type": "http"}).json()
    custom = client.post("/sandboxes", json={"type": "http", "ttl_s": 120}).json()
    now = datetime.now(UTC)
    for body, ttl in ((default, 600), (custom, 120)):
        expires = datetime.fromisoformat(body["expires_at"])
        assert abs(expires - (now + timedelta(seconds=ttl))) < timedelta(seconds=5)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"type": "ftp"},
        {"type": None},
        {"type": "http", "image": "evil:latest"},
        {"type": "http", "ttl_s": 59},
        {"type": "http", "ttl_s": 3601},
        {"type": "http", "ttl_s": "forever"},
    ],
    ids=[
        "missing-type",
        "unknown-type",
        "null-type",
        "unknown-field",
        "ttl-low",
        "ttl-high",
        "ttl-str",
    ],
)
def test_create_rejects_invalid_body(client: TestClient, body: dict[str, object]) -> None:
    assert client.post("/sandboxes", json=body).status_code == 422
    assert client.get("/sandboxes").json() == []  # nothing persisted, nothing enqueued


def test_idempotency_key_replays_original_sandbox(client: TestClient) -> None:
    """A retried create (client timeout, 503) must not start a second container."""
    headers = {"Idempotency-Key": "agent-7:req-42"}
    first = client.post("/sandboxes", json={"type": "http"}, headers=headers)
    replay = client.post("/sandboxes", json={"type": "http"}, headers=headers)
    assert first.status_code == replay.status_code == 202
    assert replay.json() == first.json()
    assert replay.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in first.headers
    assert len(client.get("/sandboxes").json()) == 1
    assert metric(client, "queue_depth", deployment="stable") == 1  # one job, not two
    assert metric(
        client,
        "jobs_enqueued_total",
        deployment="stable",
        task="start_sandbox",
        outcome="deduplicated",
    )


def test_different_idempotency_keys_create_different_sandboxes(client: TestClient) -> None:
    for key in ("a", "b"):
        client.post("/sandboxes", json={"type": "http"}, headers={"Idempotency-Key": key})
    client.post("/sandboxes", json={"type": "http"})  # no key: no dedupe
    assert len(client.get("/sandboxes").json()) == 3


@pytest.mark.parametrize("key", ["", "x" * 65, "has space", "a/b"])
def test_idempotency_key_is_bounded(client: TestClient, key: str) -> None:
    r = client.post("/sandboxes", json={"type": "http"}, headers={"Idempotency-Key": key})
    assert r.status_code == 422
    assert client.get("/sandboxes").json() == []


def test_create_answers_429_at_capacity(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    app.state.settings = settings.model_copy(update={"sandbox_max_active": 2})
    _create(client)
    _create(client)
    r = client.post("/sandboxes", json={"type": "http"})
    assert r.status_code == 429
    assert r.headers["retry-after"] == "30"
    assert len(client.get("/sandboxes").json()) == 2


def test_idempotent_replay_is_not_refused_at_capacity(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    """The original already holds its slot: a retry must get it back, not a 429."""
    app.state.settings = settings.model_copy(update={"sandbox_max_active": 1})
    headers = {"Idempotency-Key": "agent-7:req-43"}
    first = client.post("/sandboxes", json={"type": "http"}, headers=headers)
    replay = client.post("/sandboxes", json={"type": "http"}, headers=headers)
    assert replay.status_code == 202
    assert replay.json()["sandbox_id"] == first.json()["sandbox_id"]
    assert client.post("/sandboxes", json={"type": "http"}).status_code == 429


def test_get_missing_sandbox_404(client: TestClient) -> None:
    assert client.get(f"/sandboxes/{uuid.uuid4()}").status_code == 404


def test_get_malformed_id_422(client: TestClient) -> None:
    assert client.get("/sandboxes/not-a-uuid").status_code == 422


def test_list_newest_first_and_bounded(client: TestClient) -> None:
    ids = [_create(client) for _ in range(3)]
    r = client.get("/sandboxes", params={"limit": 2})
    assert [s["id"] for s in r.json()] == [ids[2], ids[1]]
    assert client.get("/sandboxes", params={"limit": 1000}).status_code == 422
    assert client.get("/sandboxes", params={"limit": 0}).status_code == 422


def test_delete_marks_stopping_and_enqueues_stop_once(client: TestClient) -> None:
    sandbox_id = _create(client)
    for _ in range(2):  # idempotent: second DELETE re-uses the same stop job id
        r = client.delete(f"/sandboxes/{sandbox_id}")
        assert r.status_code == 202
        assert r.json()["status"] == "stopping"
    assert metric(client, "queue_depth", deployment="stable") == 2  # one start + one stop
    assert metric(
        client, "jobs_enqueued_total", deployment="stable", task="stop_sandbox", outcome="enqueued"
    )


def test_delete_of_settled_sandbox_is_noop(client: TestClient, app: FastAPI) -> None:
    async def boom(*args: object, **kwargs: object) -> None:
        raise ConnectionError("redis down")

    real_enqueue = app.state.queue.enqueue_job
    app.state.queue.enqueue_job = boom
    client.post("/sandboxes", json={"type": "http"})  # -> failed (queue down)
    app.state.queue.enqueue_job = real_enqueue
    [sandbox] = client.get("/sandboxes").json()
    r = client.delete(f"/sandboxes/{sandbox['id']}")
    assert (r.status_code, r.json()["status"]) == (202, "failed")
    assert metric(client, "queue_depth", deployment="stable") == 0


def test_delete_missing_sandbox_404(client: TestClient) -> None:
    assert client.delete(f"/sandboxes/{uuid.uuid4()}").status_code == 404


def test_delete_with_queue_down_503_and_stays_stopping(client: TestClient, app: FastAPI) -> None:
    """The row keeps the stop intent; the reaper finishes it even if nobody retries."""
    sandbox_id = _create(client)

    async def boom(*args: object, **kwargs: object) -> None:
        raise ConnectionError("redis down")

    app.state.queue.enqueue_job = boom
    assert client.delete(f"/sandboxes/{sandbox_id}").status_code == 503
    assert client.get(f"/sandboxes/{sandbox_id}").json()["status"] == "stopping"
