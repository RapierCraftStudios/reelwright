# SPDX-License-Identifier: AGPL-3.0-or-later
from fastapi.testclient import TestClient


def test_create_then_list(client: TestClient) -> None:
    assert client.get("/api/projects").json() == []
    created = client.post("/api/projects", json={"title": "My First Video", "format": "vlog"})
    assert created.status_code == 201
    body = created.json()
    assert body["slug"] == "my-first-video"
    assert body["format"] == "vlog"
    assert body["status"] == "draft"
    listed = client.get("/api/projects").json()
    assert [p["id"] for p in listed] == [body["id"]]
    assert client.get(f"/api/projects/{body['id']}").json()["title"] == "My First Video"


def test_slugs_are_unique(client: TestClient) -> None:
    slugs = [client.post("/api/projects", json={"title": "Same"}).json()["slug"] for _ in range(3)]
    assert slugs == ["same", "same-2", "same-3"]


def test_missing_project_is_404(client: TestClient) -> None:
    assert client.get("/api/projects/999").status_code == 404


def test_bad_format_is_422(client: TestClient) -> None:
    response = client.post("/api/projects", json={"title": "x", "format": "movie"})
    assert response.status_code == 422


def test_empty_title_is_422(client: TestClient) -> None:
    assert client.post("/api/projects", json={"title": ""}).status_code == 422


def test_unknown_api_path_is_404_not_spa(client: TestClient) -> None:
    assert client.get("/api/nope").status_code == 404
