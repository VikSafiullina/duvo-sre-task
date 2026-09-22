import asyncio
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.models import Deployment
from app.queue import QUEUES
from app.rollout import route
from tests.support import metric

IDS = [uuid.UUID(int=i * 7919 + 1) for i in range(2000)]


# --- routing -------------------------------------------------------------------------------


def test_route_extremes() -> None:
    assert {route(i, 0) for i in IDS} == {Deployment.STABLE}
    assert {route(i, 100) for i in IDS} == {Deployment.CANARY}


def test_route_is_deterministic_and_close_to_weight() -> None:
    first = [route(i, 25) for i in IDS]
    assert first == [route(i, 25) for i in IDS]  # a retried request lands on the same pool
    share = first.count(Deployment.CANARY) / len(IDS)
    assert 0.20 < share < 0.30


def test_raising_weight_never_moves_a_sandbox_back_to_stable() -> None:
    for low, high in ((10, 25), (25, 50), (50, 100)):
        canary_low = {i for i in IDS if route(i, low) is Deployment.CANARY}
        canary_high = {i for i in IDS if route(i, high) is Deployment.CANARY}
        assert canary_low <= canary_high


def test_each_pool_has_its_own_queue_and_stable_keeps_the_default() -> None:
    assert QUEUES == {Deployment.STABLE: "arq:queue", Deployment.CANARY: "arq:queue:canary"}


# --- PUT/GET /rollout ----------------------------------------------------------------------


def test_weight_defaults_to_zero_and_round_trips(client: TestClient) -> None:
    assert client.get("/rollout").json() == {"canary_weight": 0}
    r = client.put("/rollout", json={"canary_weight": 25})
    assert (r.status_code, r.json()) == (200, {"canary_weight": 25})
    assert client.get("/rollout").json() == {"canary_weight": 25}
    assert "rollout_canary_weight 25.0" in client.get("/metrics").text


@pytest.mark.parametrize(
    "body",
    [{}, {"canary_weight": -1}, {"canary_weight": 101}, {"canary_weight": "50"},
     {"canary_weight": 50.5}, {"canary_weight": True}, {"canary_weight": 5, "pool": "b"}],
    ids=["missing", "negative", "over-100", "string", "float", "bool", "unknown-field"],
)  # fmt: skip
def test_put_rejects_invalid_weight(client: TestClient, body: dict[str, object]) -> None:
    assert client.put("/rollout", json=body).status_code == 422
    assert client.get("/rollout").json() == {"canary_weight": 0}


def test_rollout_503_when_redis_down(client: TestClient, app: FastAPI) -> None:
    async def boom(*args: object, **kwargs: object) -> None:
        raise ConnectionError("redis down")

    app.state.queue.get = boom
    app.state.queue.set = boom
    assert client.get("/rollout").status_code == 503
    assert client.put("/rollout", json={"canary_weight": 10}).status_code == 503


# --- producer routing ----------------------------------------------------------------------


@pytest.mark.parametrize(("weight", "pool"), [(0, "stable"), (100, "canary")])
def test_create_routes_job_to_the_weighted_pool(client: TestClient, weight: int, pool: str) -> None:
    client.put("/rollout", json={"canary_weight": weight})
    accepted = client.post("/sandboxes", json={"type": "http"}).json()
    assert accepted["deployment"] == pool
    assert client.get(f"/sandboxes/{accepted['sandbox_id']}").json()["deployment"] == pool
    other = "canary" if pool == "stable" else "stable"
    assert metric(client, "queue_depth", deployment=pool) == 1
    assert metric(client, "queue_depth", deployment=other) == 0
    assert metric(
        client, "jobs_enqueued_total", deployment=pool, task="start_sandbox", outcome="enqueued"
    )


def test_stop_follows_the_current_weight_so_rollback_drains_to_stable(
    client: TestClient,
) -> None:
    client.put("/rollout", json={"canary_weight": 100})
    sandbox_id = client.post("/sandboxes", json={"type": "http"}).json()["sandbox_id"]
    client.put("/rollout", json={"canary_weight": 0})  # rollback
    assert client.delete(f"/sandboxes/{sandbox_id}").status_code == 202
    assert metric(
        client, "jobs_enqueued_total", deployment="stable", task="stop_sandbox", outcome="enqueued"
    )


def test_unreadable_weight_fails_safe_to_stable(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    client.put("/rollout", json={"canary_weight": 100})

    async def hang(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(5)

    app.state.settings = settings.model_copy(update={"redis_timeout_s": 0.05})
    app.state.queue.get = hang
    r = client.post("/sandboxes", json={"type": "http"})
    assert (r.status_code, r.json()["deployment"]) == (202, "stable")
