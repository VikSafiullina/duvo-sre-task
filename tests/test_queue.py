from fastapi import FastAPI
from fastapi.testclient import TestClient


def _new_item(client: TestClient) -> str:
    return client.post("/items", json={"name": "x"}).json()["id"]


def test_process_enqueues_and_marks_queued(client: TestClient) -> None:
    item_id = _new_item(client)
    r = client.post(f"/items/{item_id}/process")
    assert r.status_code == 202
    assert r.json() == {
        "job_id": f"process_item:{item_id}",
        "item_id": item_id,
        "status": "queued",
    }
    assert client.get(f"/items/{item_id}").json()["status"] == "queued"


def test_process_is_idempotent_while_queued(client: TestClient) -> None:
    item_id = _new_item(client)
    assert client.post(f"/items/{item_id}/process").status_code == 202
    assert client.post(f"/items/{item_id}/process").status_code == 202
    assert "queue_depth 1.0" in client.get("/metrics").text


def test_process_missing_item_404(client: TestClient) -> None:
    assert client.post("/items/00000000-0000-0000-0000-000000000000/process").status_code == 404


def test_process_503_and_status_reverted_when_queue_down(client: TestClient, app: FastAPI) -> None:
    item_id = _new_item(client)

    async def boom(*args: object, **kwargs: object) -> None:
        raise ConnectionError("redis down")

    app.state.queue.enqueue_job = boom
    assert client.post(f"/items/{item_id}/process").status_code == 503
    assert client.get(f"/items/{item_id}").json()["status"] == "pending"
