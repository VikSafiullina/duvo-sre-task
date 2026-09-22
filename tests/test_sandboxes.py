import uuid

import pytest
from fastapi.testclient import TestClient


def test_create_accepts_and_get_returns_queued_sandbox(client: TestClient) -> None:
    r = client.post("/sandboxes", json={"type": "http"})
    assert r.status_code == 202
    accepted = r.json()
    sandbox_id = accepted["sandbox_id"]
    assert accepted == {
        "job_id": f"start_sandbox:{sandbox_id}",
        "sandbox_id": sandbox_id,
        "type": "http",
        "status": "queued",
    }
    sandbox = client.get(f"/sandboxes/{sandbox_id}").json()
    assert sandbox["status"] == "queued"
    assert sandbox["url"] is None
    assert sandbox["attempts"] == 0


@pytest.mark.parametrize(
    "body",
    [{}, {"type": "ftp"}, {"type": "http", "image": "evil:latest"}, {"type": None}],
    ids=["missing-type", "unknown-type", "unknown-field", "null-type"],
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
    body = client.get("/metrics").text
    assert "queue_depth 1.0" in body  # one job, not two
    assert 'jobs_enqueued_total{job="start_sandbox",outcome="deduplicated"}' in body


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


def test_get_missing_sandbox_404(client: TestClient) -> None:
    assert client.get(f"/sandboxes/{uuid.uuid4()}").status_code == 404


def test_get_malformed_id_422(client: TestClient) -> None:
    assert client.get("/sandboxes/not-a-uuid").status_code == 422


def test_list_newest_first_and_bounded(client: TestClient) -> None:
    ids = [client.post("/sandboxes", json={"type": "http"}).json()["sandbox_id"] for _ in range(3)]
    r = client.get("/sandboxes", params={"limit": 2})
    assert [s["id"] for s in r.json()] == [ids[2], ids[1]]
    assert client.get("/sandboxes", params={"limit": 1000}).status_code == 422
    assert client.get("/sandboxes", params={"limit": 0}).status_code == 422
