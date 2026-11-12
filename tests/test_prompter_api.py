# SPDX-License-Identifier: AGPL-3.0-or-later
"""Teleprompter retake marks: /api/prompter/{id}/marks (issue #6)."""

from fastapi.testclient import TestClient

SCRIPT = (
    "---\nformat: long\nworking_title: T\n---\n"
    "## beat: hook | intent: grab\nHi there. [broll: desk]\n"
    "## beat: cta | intent: subscribe\nSee you next time.\n"
)


def _project(client: TestClient, *, lock: bool = True) -> int:
    pid = int(client.post("/api/projects", json={"title": "Prompter test"}).json()["id"])
    assert client.put(f"/api/projects/{pid}/script", json={"source": SCRIPT}).status_code == 201
    if lock:
        assert client.post(f"/api/projects/{pid}/script/lock").status_code == 200
    return pid


def _mark(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "kind": "retake",
        "script_version": 1,
        "beat_slug": "cta",
        "line_no": 8,
        "marked_at": "2026-11-12T10:15:30.250Z",
        "offset_ms": 4200,
    }
    body.update(overrides)
    return body


def test_retake_mark_is_stored_with_timestamps_and_listed(client: TestClient) -> None:
    pid = _project(client)
    assert client.get(f"/api/prompter/{pid}/marks").json() == []

    first = client.post(f"/api/prompter/{pid}/marks", json=_mark())
    assert first.status_code == 201
    mark = first.json()
    assert mark["kind"] == "retake"
    assert (mark["script_version"], mark["beat_slug"], mark["line_no"]) == (1, "cta", 8)
    assert mark["marked_at"].startswith("2026-11-12T10:15:30.250")
    assert mark["offset_ms"] == 4200
    assert mark["created_at"]

    earlier = _mark(beat_slug="hook", line_no=6, marked_at="2026-11-12T10:15:01Z", offset_ms=10)
    assert client.post(f"/api/prompter/{pid}/marks", json=earlier).status_code == 201
    listed = client.get(f"/api/prompter/{pid}/marks").json()
    assert [(m["beat_slug"], m["line_no"]) for m in listed] == [("hook", 6), ("cta", 8)]


def test_marks_are_scoped_to_their_project(client: TestClient) -> None:
    a, b = _project(client), _project(client)
    assert client.post(f"/api/prompter/{a}/marks", json=_mark()).status_code == 201
    assert client.get(f"/api/prompter/{b}/marks").json() == []


def test_unknown_project_is_404(client: TestClient) -> None:
    assert client.post("/api/prompter/999/marks", json=_mark()).status_code == 404
    assert client.get("/api/prompter/999/marks").status_code == 404


def test_mark_needs_an_existing_locked_version(client: TestClient) -> None:
    unlocked = _project(client, lock=False)
    response = client.post(f"/api/prompter/{unlocked}/marks", json=_mark())
    assert response.status_code == 422
    assert "not locked" in response.json()["detail"]["message"]

    locked = _project(client)
    missing = client.post(f"/api/prompter/{locked}/marks", json=_mark(script_version=7))
    assert missing.status_code == 422
    assert "not found" in missing.json()["detail"]["message"]


def test_mark_body_is_validated(client: TestClient) -> None:
    pid = _project(client)
    url = f"/api/prompter/{pid}/marks"
    assert client.post(url, json=_mark(kind="keeper")).status_code == 422
    assert client.post(url, json=_mark(offset_ms=-1)).status_code == 422
    assert client.post(url, json=_mark(line_no=0)).status_code == 422
    assert client.post(url, json=_mark(marked_at="2026-11-12T10:15:30")).status_code == 422
    assert client.get(url).json() == []
