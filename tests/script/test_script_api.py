# SPDX-License-Identifier: AGPL-3.0-or-later
import json
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from spectracut.providers.base import ProviderError

from reelwright.api.script import get_text_provider

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


def _events(body: str) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for frame in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.splitlines())
        out.append((fields["event"], json.loads(fields["data"])))
    return out


def test_generate_streams_progress_and_stores_a_draft(client: TestClient) -> None:
    pid = _project(client)
    response = client.post(f"/api/projects/{pid}/script/generate", json={"topic": "Year one"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    assert [e for e, _ in events] == ["progress", "progress", "progress", "done"]
    assert [d["stage"] for _, d in events[:3]] == ["outline", "script", "validate"]
    done = events[-1][1]
    assert done["version"]["version"] == 1 and done["errors"] == []
    assert done["script"]["front_matter"]["status"] == "draft"
    assert len(done["script"]["front_matter"]["title_options"]) == 5
    assert [v["version"] for v in client.get(f"/api/projects/{pid}/script/versions").json()] == [1]
    latest = client.get(f"/api/projects/{pid}/script").json()
    assert latest["errors"] == [] and latest["locked"] is False
    hooks = [b for b in latest["script"]["beats"] if b["type"] == "hook"]
    assert len(hooks) == 3


def test_generate_refuses_locked_script(client: TestClient) -> None:
    pid = _project(client)
    client.put(f"/api/projects/{pid}/script", json={"source": GOOD})
    client.post(f"/api/projects/{pid}/script/lock")
    response = client.post(f"/api/projects/{pid}/script/generate", json={"topic": "x"})
    assert response.status_code == 409
    assert [v["version"] for v in client.get(f"/api/projects/{pid}/script/versions").json()] == [1]


def test_generate_unknown_project_is_404(client: TestClient) -> None:
    assert client.post("/api/projects/999/script/generate", json={"topic": "x"}).status_code == 404


def test_generate_rejects_empty_topic(client: TestClient) -> None:
    pid = _project(client)
    response = client.post(f"/api/projects/{pid}/script/generate", json={"topic": ""})
    assert response.status_code == 422


class _Failing:
    name = "failing"

    def complete(self, system: str, prompt: str, *, schema: dict[str, Any]) -> Any:
        raise ProviderError("claude", "rate limit reached")


def test_generate_provider_error_streams_error_and_stores_nothing(client: TestClient) -> None:
    pid = _project(client)
    app = cast(FastAPI, client.app)
    app.dependency_overrides[get_text_provider] = _Failing
    try:
        response = client.post(f"/api/projects/{pid}/script/generate", json={"topic": "x"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    events = _events(response.text)
    assert events[0] == ("progress", {"stage": "outline"})
    assert events[-1][0] == "error" and "rate limit" in events[-1][1]["message"]
    assert client.get(f"/api/projects/{pid}/script/versions").json() == []
