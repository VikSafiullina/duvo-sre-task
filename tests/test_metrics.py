import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families


def _samples(client: TestClient) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    text = client.get("/metrics").text
    return {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in text_string_to_metric_families(text)
        for s in family.samples
    }


def test_metrics_label_by_route_template(client: TestClient) -> None:
    client.get(f"/sandboxes/{uuid.uuid4()}")
    body = client.get("/metrics").text
    assert 'http_requests_total{method="GET",route="/sandboxes/{sandbox_id}",status="404"}' in body


def test_unknown_paths_do_not_explode_cardinality(client: TestClient) -> None:
    client.get(f"/nope/{uuid.uuid4()}")
    body = client.get("/metrics").text
    assert 'route="unmatched"' in body


def test_unknown_http_methods_do_not_explode_cardinality(client: TestClient) -> None:
    for method in ("SPAM1", "SPAM2"):
        client.request(method, "/sandboxes")
    body = client.get("/metrics").text
    assert "SPAM" not in body
    assert 'http_requests_total{method="OTHER",route=' in body


def test_request_id_echoed_or_generated(client: TestClient) -> None:
    echoed = client.get("/healthz", headers={"x-request-id": "abc123"})
    assert echoed.headers["x-request-id"] == "abc123"
    assert len(client.get("/healthz").headers["x-request-id"]) == 32


def test_lifecycle_gauges_sampled_from_db(client: TestClient) -> None:
    for _ in range(2):
        client.post("/sandboxes", json={"type": "http"})
    m = _samples(client)
    assert m[("sandboxes_active", (("status", "queued"),))] == 2
    assert m[("sandboxes_active", (("status", "running"),))] == 0  # explicit zero, not absent
    assert m[("sandbox_oldest_in_status_seconds", (("status", "queued"),))] > 0
    assert m[("sandbox_oldest_in_status_seconds", (("status", "running"),))] == 0
    assert m[("sandbox_capacity", ())] == 50
    assert m[("queue_depth", (("queue", "arq:queue"),))] == 2


def test_scrape_survives_db_outage_and_drops_stale_gauges(client: TestClient, app: FastAPI) -> None:
    client.post("/sandboxes", json={"type": "http"})
    assert ("sandboxes_active", (("status", "queued"),)) in _samples(client)

    def db_down() -> None:
        raise ConnectionError("postgres down")

    app.state.sessionmaker = db_down
    r = client.get("/metrics")
    assert r.status_code == 200
    m = _samples(client)
    assert not any(name == "sandboxes_active" for name, _ in m)  # unknown, not frozen
    assert ("queue_depth", (("queue", "arq:queue"),)) in m  # other samplers unaffected


def test_scrape_survives_redis_outage(client: TestClient, app: FastAPI) -> None:
    async def redis_down(*args: object) -> int:
        raise ConnectionError("redis down")

    app.state.queue.zcard = redis_down
    assert client.get("/metrics").status_code == 200
    assert ("sandbox_capacity", ()) in _samples(client)
