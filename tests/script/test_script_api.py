# SPDX-License-Identifier: AGPL-3.0-or-later
from fastapi.testclient import TestClient

GOOD = (
    "---\nformat: long\nworking_title: T\n---\n"
    "## beat: hook | intent: grab\nHi there. [broll: desk]\n"
)
TODO = GOOD + "[TODO punchline]\n"


def _project(client: TestClient) -> int:
    response = client.post("/api/projects", json={"title": "Script test"})
    return int(response.json()["id"])


def test_put_stores_versions_and_get_returns_latest(client: TestClient) -> None:
    pid = _project(client)
    assert client.get(f"/api/projects/{pid}/script").status_code == 404
    first = client.put(f"/api/projects/{pid}/script", json={"source": GOOD})
    assert first.status_code == 201
    assert first.json()["version"]["version"] == 1
    second = client.put(f"/api/projects/{pid}/script", json={"source": TODO})
    assert second.json()["version"]["version"] == 2
    latest = client.get(f"/api/projects/{pid}/script").json()
    assert latest["version"]["version"] == 2
    assert latest["locked"] is False
    beat = latest["script"]["beats"][0]
    assert (beat["slug"], beat["lines"][0]["cues"][0]["type"]) == ("hook", "broll")
    versions = client.get(f"/api/projects/{pid}/script/versions").json()
    assert [v["version"] for v in versions] == [2, 1]


def test_put_rejects_parse_errors_with_line_numbers(client: TestClient) -> None:
    pid = _project(client)
    response = client.put(f"/api/projects/{pid}/script", json={"source": GOOD + "[zoom: x]\n"})
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert [(e["line"], e["col"]) for e in errors] == [(7, 1)]
    assert client.get(f"/api/projects/{pid}/script/versions").json() == []


def test_parse_does_not_store(client: TestClient) -> None:
    pid = _project(client)
    body = client.post(f"/api/projects/{pid}/script/parse", json={"source": "## nope\n"}).json()
    assert [e["line"] for e in body["errors"]] == [1, 1]
    assert client.get(f"/api/projects/{pid}/script/versions").json() == []


def test_lock_refused_with_todo_then_freezes(client: TestClient) -> None:
    pid = _project(client)
    assert client.post(f"/api/projects/{pid}/script/lock").status_code == 404
    client.put(f"/api/projects/{pid}/script", json={"source": TODO})
    refused = client.post(f"/api/projects/{pid}/script/lock")
    assert refused.status_code == 409
    assert refused.json()["detail"]["todos"] == [{"line": 7, "text": "[TODO punchline]"}]
    client.put(f"/api/projects/{pid}/script", json={"source": GOOD})
    locked = client.post(f"/api/projects/{pid}/script/lock")
    assert locked.status_code == 200
    assert locked.json()["locked"] is True
    assert client.put(f"/api/projects/{pid}/script", json={"source": GOOD}).status_code == 409
    assert client.post(f"/api/projects/{pid}/script/lock").status_code == 409
    assert client.get(f"/api/projects/{pid}/script").json()["version"]["version"] == 2


def test_unknown_project_is_404(client: TestClient) -> None:
    assert client.get("/api/projects/999/script").status_code == 404
    assert client.put("/api/projects/999/script", json={"source": GOOD}).status_code == 404
    assert client.post("/api/projects/999/script/parse", json={"source": GOOD}).status_code == 404
