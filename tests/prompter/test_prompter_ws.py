# SPDX-License-Identifier: AGPL-3.0-or-later
"""WS /api/prompter/{id}/align on the real app with the mock speech engine (issue #7)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from reelwright.prompter.asr import BYTES_PER_SECOND

MS_PER_WORD = 100
WORD = bytes(BYTES_PER_SECOND * MS_PER_WORD // 1000)  # audio that makes the mock "hear" one word

SCRIPT = (
    "---\nformat: long\nworking_title: T\n---\n"
    "## beat: hook | intent: grab\nHi there. [broll: desk]\n"
    "## beat: cta | intent: subscribe\nSee you next time.\n"
)
# Spoken lines: 6 "Hi there." and 8 "See you next time."


@pytest.fixture(autouse=True)
def mock_asr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REELWRIGHT_PROMPTER_ASR", "mock")
    monkeypatch.setenv("REELWRIGHT_PROMPTER_MOCK_MS_PER_WORD", str(MS_PER_WORD))


def _project(client: TestClient, source: str = SCRIPT, *, lock: bool = True) -> int:
    pid = int(client.post("/api/projects", json={"title": "Voice"}).json()["id"])
    assert client.put(f"/api/projects/{pid}/script", json={"source": source}).status_code == 201
    if lock:
        assert client.post(f"/api/projects/{pid}/script/lock").status_code == 200
    return pid


def _start(ws: Any, line_no: int | None = 6, version: int = 1) -> None:
    message: dict[str, Any] = {"type": "start", "script_version": version}
    if line_no is not None:
        message["line_no"] = line_no
    ws.send_json(message)


def _rejected(client: TestClient, pid: int, start: dict[str, Any]) -> tuple[str, int]:
    with client.websocket_connect(f"/api/prompter/{pid}/align") as ws:
        ws.send_json(start)
        error = ws.receive_json()
        assert error["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    return str(error["code"]), closed.value.code


def test_position_follows_the_audio_and_seek_reseeds(client: TestClient) -> None:
    pid = _project(client)
    with client.websocket_connect(f"/api/prompter/{pid}/align") as ws:
        _start(ws, None)  # no line_no: the first spoken line
        ws.send_bytes(WORD)
        first = ws.receive_json()
        assert first["type"] == "position"
        assert (first["line_no"], first["word"], first["confidence"]) == (6, 0, 1.0)
        assert first["server_ms"] >= 0
        ws.send_bytes(WORD * 2)  # "there", "see": onto the next line
        assert (ws.receive_json()["line_no"], 0) == (8, 0)
        ws.send_bytes(WORD)
        assert ws.receive_json()["word"] == 1  # "you"
        ws.send_json({"type": "seek", "line_no": 6})
        ws.send_bytes(WORD)
        assert ws.receive_json() | {"server_ms": 0} == {
            "type": "position",
            "line_no": 6,
            "word": 0,
            "confidence": 1.0,
            "server_ms": 0,
        }


def test_start_line_is_where_listening_begins(client: TestClient) -> None:
    pid = _project(client)
    with client.websocket_connect(f"/api/prompter/{pid}/align") as ws:
        _start(ws, 8)
        ws.send_bytes(WORD)
        assert ws.receive_json()["line_no"] == 8


def test_ping_is_echoed(client: TestClient) -> None:
    pid = _project(client)
    with client.websocket_connect(f"/api/prompter/{pid}/align") as ws:
        _start(ws)
        ws.send_json({"type": "ping", "t": 123.5})
        assert ws.receive_json() == {"type": "pong", "t": 123.5}


def test_rejects_missing_project_unknown_version_and_unlocked_script(client: TestClient) -> None:
    pid = _project(client)
    draft = _project(client, lock=False)
    start = {"type": "start", "script_version": 1, "line_no": 6}
    assert _rejected(client, 999, start) == ("project_not_found", 4404)
    assert _rejected(client, pid, start | {"script_version": 9}) == ("version_not_found", 4404)
    assert _rejected(client, draft, start) == ("not_locked", 4409)
    assert _rejected(client, pid, start | {"line_no": 7}) == ("bad_line_no", 4400)
    assert _rejected(client, pid, {"type": "seek", "line_no": 6}) == ("expected_start", 4400)


def test_asr_unavailable_closes_1011(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _project(client)
    monkeypatch.setenv("REELWRIGHT_PROMPTER_ASR", "no-such-engine")
    start = {"type": "start", "script_version": 1, "line_no": 6}
    assert _rejected(client, pid, start) == ("asr_unavailable", 1011)


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, -(-95 * len(ordered) // 100) - 1)]


def test_round_trips_are_under_300ms_p95_and_no_audio_hits_disk(
    client: TestClient, tmp_path: Path
) -> None:
    words = " ".join(f"word{i}" for i in range(60))
    source = f"---\nformat: long\nworking_title: T\n---\n## beat: hook | intent: grab\n{words}.\n"
    pid = _project(client, source)
    before = {p.name for p in tmp_path.rglob("*")}
    pings: list[float] = []
    frames: list[float] = []
    with client.websocket_connect(f"/api/prompter/{pid}/align") as ws:
        _start(ws)
        for i in range(50):
            sent = time.perf_counter()
            ws.send_json({"type": "ping", "t": i})
            assert ws.receive_json() == {"type": "pong", "t": i}
            pings.append((time.perf_counter() - sent) * 1000)
        for i in range(50):
            sent = time.perf_counter()
            ws.send_bytes(WORD)
            position = ws.receive_json()
            frames.append((time.perf_counter() - sent) * 1000)
            assert (position["type"], position["word"]) == ("position", i)
    assert _p95(pings) < 300, pings
    assert _p95(frames) < 300, frames
    after = {p.name for p in tmp_path.rglob("*")}
    assert all(name.startswith("rw.sqlite") for name in after - before), after - before
    assert all(name.startswith("rw.sqlite") for name in after), after
