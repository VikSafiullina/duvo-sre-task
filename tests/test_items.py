import uuid

from fastapi.testclient import TestClient


def test_create_and_get_item(client: TestClient) -> None:
    r = client.post("/items", json={"name": "invoice-42"})
    assert r.status_code == 201
    item = r.json()
    assert item["status"] == "pending"
    assert item["attempts"] == 0
    r = client.get(f"/items/{item['id']}")
    assert r.status_code == 200
    assert r.json()["name"] == "invoice-42"


def test_get_missing_item_404(client: TestClient) -> None:
    assert client.get(f"/items/{uuid.uuid4()}").status_code == 404


def test_create_rejects_empty_name(client: TestClient) -> None:
    assert client.post("/items", json={"name": ""}).status_code == 422


def test_list_newest_first_and_bounded(client: TestClient) -> None:
    for i in range(3):
        client.post("/items", json={"name": f"n{i}"})
    r = client.get("/items", params={"limit": 2})
    assert [i["name"] for i in r.json()] == ["n2", "n1"]
    assert client.get("/items", params={"limit": 1000}).status_code == 422
