import uuid

from fastapi.testclient import TestClient


def test_metrics_label_by_route_template(client: TestClient) -> None:
    client.get(f"/sandboxes/{uuid.uuid4()}")
    body = client.get("/metrics").text
    assert 'http_requests_total{method="GET",route="/sandboxes/{sandbox_id}",status="404"}' in body
    assert "queue_depth" in body


def test_unknown_paths_do_not_explode_cardinality(client: TestClient) -> None:
    client.get(f"/nope/{uuid.uuid4()}")
    body = client.get("/metrics").text
    assert 'route="unmatched"' in body


def test_request_id_echoed_or_generated(client: TestClient) -> None:
    echoed = client.get("/healthz", headers={"x-request-id": "abc123"})
    assert echoed.headers["x-request-id"] == "abc123"
    assert len(client.get("/healthz").headers["x-request-id"]) == 32
