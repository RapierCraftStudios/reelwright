# SPDX-License-Identifier: AGPL-3.0-or-later
"""Camera take markers: /api/projects/{id}/takes and match_take (issue #50)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from reelwright.api.takes import match_take
from reelwright.db.models import CameraTakeRead

SCRIPT = (
    "---\nformat: long\nworking_title: T\n---\n"
    "## beat: hook | intent: grab\nHi there. [broll: desk]\n"
    "## beat: cta | intent: subscribe\nSee you next time.\n"
)


def _project(client: TestClient, *, lock: bool = True) -> int:
    pid = int(client.post("/api/projects", json={"title": "Takes test"}).json()["id"])
    assert client.put(f"/api/projects/{pid}/script", json={"source": SCRIPT}).status_code == 201
    if lock:
        assert client.post(f"/api/projects/{pid}/script/lock").status_code == 200
    return pid


def _take(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "client_id": "6f1d1b8e-0000-4000-8000-000000000001",
        "script_version": 1,
        "beat_slug": "cta",
        "line_from": 8,
        "line_to": 9,
        "retake": False,
        "continuous": False,
        "camera": "Osmo Action 5 Pro",
        "started_at": "2026-11-12T10:15:30.250Z",
        "stopped_at": "2026-11-12T10:15:42Z",
    }
    body.update(overrides)
    return body


def test_take_is_stored_and_listed_by_start_time(client: TestClient) -> None:
    pid = _project(client)
    url = f"/api/projects/{pid}/takes"
    assert client.get(url).json() == []

    created = client.post(url, json=_take())
    assert created.status_code == 201
    take = created.json()
    assert (take["beat_slug"], take["line_from"], take["line_to"]) == ("cta", 8, 9)
    assert take["script_version"] == 1
    assert take["project_id"] == pid
    assert take["started_at"].startswith("2026-11-12T10:15:30.250")
    assert take["camera"] == "Osmo Action 5 Pro"
    assert take["created_at"]

    earlier = _take(
        client_id="6f1d1b8e-0000-4000-8000-000000000002",
        beat_slug="hook",
        line_from=6,
        line_to=6,
        retake=True,
        started_at="2026-11-12T10:15:01Z",
        stopped_at=None,
    )
    assert client.post(url, json=earlier).status_code == 201
    listed = client.get(url).json()
    assert [(t["beat_slug"], t["retake"], t["stopped_at"]) for t in listed] == [
        ("hook", True, None),
        ("cta", False, listed[1]["stopped_at"]),
    ]


def test_replayed_client_id_returns_the_stored_take(client: TestClient) -> None:
    pid = _project(client)
    url = f"/api/projects/{pid}/takes"
    first = client.post(url, json=_take())
    again = client.post(url, json=_take(line_to=12))
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["line_to"] == 9
    assert len(client.get(url).json()) == 1

    other = _project(client)
    assert client.post(f"/api/projects/{other}/takes", json=_take()).status_code == 409


def test_takes_are_scoped_to_their_project(client: TestClient) -> None:
    a, b = _project(client), _project(client)
    assert client.post(f"/api/projects/{a}/takes", json=_take()).status_code == 201
    assert client.get(f"/api/projects/{b}/takes").json() == []


def test_unknown_project_is_404(client: TestClient) -> None:
    assert client.post("/api/projects/999/takes", json=_take()).status_code == 404
    assert client.get("/api/projects/999/takes").status_code == 404


def test_take_needs_an_existing_locked_version(client: TestClient) -> None:
    unlocked = _project(client, lock=False)
    response = client.post(f"/api/projects/{unlocked}/takes", json=_take())
    assert response.status_code == 422
    assert "not locked" in response.json()["detail"]["message"]

    locked = _project(client)
    missing = client.post(f"/api/projects/{locked}/takes", json=_take(script_version=7))
    assert missing.status_code == 422
    assert "not found" in missing.json()["detail"]["message"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"started_at": "2026-11-12T10:15:30"},
        {"stopped_at": "2026-11-12T10:15:42"},
        {"line_from": 9, "line_to": 8},
        {"line_from": 0},
        {"script_version": 0},
        {"beat_slug": ""},
        {"client_id": ""},
        {"stopped_at": "2026-11-12T10:15:00Z"},
    ],
)
def test_take_body_is_validated(client: TestClient, overrides: dict[str, object]) -> None:
    pid = _project(client)
    url = f"/api/projects/{pid}/takes"
    assert client.post(url, json=_take(**overrides)).status_code == 422
    assert client.get(url).json() == []


def _read(take_id: int, started: datetime) -> CameraTakeRead:
    return CameraTakeRead(
        id=take_id,
        project_id=1,
        script_version_id=1,
        script_version=1,
        client_id=f"c{take_id}",
        beat_slug="hook",
        line_from=1,
        line_to=2,
        retake=False,
        continuous=False,
        camera="",
        started_at=started,
        stopped_at=None,
        created_at=started,
    )


def test_match_take_joins_a_camera_file_by_nearest_start_within_tolerance() -> None:
    t0 = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    takes = [
        _read(1, t0),
        _read(2, t0 + timedelta(seconds=30)),
        _read(3, t0 + timedelta(minutes=2)),
    ]

    first = match_take(takes, t0 + timedelta(seconds=1.2))
    assert first is not None and first.id == 1
    second = match_take(takes, t0 + timedelta(seconds=28))
    assert second is not None and second.id == 2
    assert match_take(takes, t0 + timedelta(seconds=60)) is None
    loose = match_take(takes, t0 + timedelta(seconds=60), tolerance_s=40)
    assert loose is not None and loose.id == 2
    assert match_take([], t0) is None
    with pytest.raises(ValueError):
        match_take(takes, datetime(2026, 11, 12, 10, 0))


def test_listed_takes_join_to_camera_files(client: TestClient) -> None:
    pid = _project(client)
    url = f"/api/projects/{pid}/takes"
    client.post(url, json=_take(client_id="a", started_at="2026-11-12T10:00:00Z", stopped_at=None))
    client.post(url, json=_take(client_id="b", beat_slug="hook", started_at="2026-11-12T10:01:00Z"))
    stored = [CameraTakeRead.model_validate(t) for t in client.get(url).json()]
    hit = match_take(stored, datetime(2026, 11, 12, 10, 1, 2, tzinfo=UTC))
    assert hit is not None and (hit.client_id, hit.beat_slug) == ("b", "hook")
