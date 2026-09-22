from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_healthz_ok(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_ok_when_dependencies_up(client: TestClient) -> None:
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["checks"] == {"database": "ok", "redis": "ok"}


def test_readyz_503_when_redis_down(client: TestClient, app: FastAPI) -> None:
    async def boom() -> None:
        raise ConnectionError("redis down")

    app.state.queue.ping = boom
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["redis"] == "error: ConnectionError"
