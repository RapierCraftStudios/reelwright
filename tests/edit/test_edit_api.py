# SPDX-License-Identifier: AGPL-3.0-or-later
"""POST /api/projects/:id/edit builds a rough cut and returns its OTIO."""

from __future__ import annotations

import importlib
import json
from typing import Any

from fastapi.testclient import TestClient
from sqlmodel import Session

from reelwright.db.models import Asset, Transcript, TranscriptWord
from reelwright.db.session import get_engine

otio: Any = importlib.import_module("opentimelineio")

SCRIPT = (
    "---\nformat: long\nworking_title: T\n---\n"
    "## beat: hook | intent: grab\nToday we test the new camera.\n"
    "## beat: cta | intent: sub\nSubscribe for more gear tests.\n"
)
LINE = "Today we test the new camera."


def say(text: str, at: float) -> list[TranscriptWord]:
    out: list[TranscriptWord] = []
    for word in text.split():
        out.append(TranscriptWord(word=word, start=at, end=at + 0.3, confidence=0.9))
        at += 0.4
    return out


def add_asset(n: int, words: list[TranscriptWord] | None) -> int:
    with Session(get_engine()) as session:
        asset = Asset(
            host="mrdubey", path=f"D:/footage/{n}.mp4", checksum=f"{n:064x}", kind="camera"
        )
        session.add(asset)
        session.commit()
        session.refresh(asset)
        assert asset.id is not None
        if words is not None:
            row = Transcript(
                asset_id=asset.id,
                engine="whisperx",
                words=json.dumps([w.model_dump() for w in words]),
                word_count=len(words),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            asset.transcript_id = row.id
            session.add(asset)
            session.commit()
        return asset.id


def project(client: TestClient, script: str | None = SCRIPT) -> int:
    pid = int(client.post("/api/projects", json={"title": "Edit test"}).json()["id"])
    if script is not None:
        assert client.put(f"/api/projects/{pid}/script", json={"source": script}).status_code == 201
    return pid


def footage() -> list[TranscriptWord]:
    return (
        say("Today we test the the um new camera.", 0.0)
        + say(LINE, 5.0)
        + say(LINE, 10.0)
        + say("Subscribe for more gear tests.", 15.0)
    )


def test_post_edit_builds_a_rough_cut_and_returns_otio(client: TestClient) -> None:
    pid = project(client)
    asset_id = add_asset(1, footage())
    response = client.post(f"/api/projects/{pid}/edit", json={"asset_ids": [asset_id]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [(t["beat"], t["start"]) for t in body["takes"]] == [("hook", 10.0), ("cta", 15.0)]
    assert body["missing_lines"] == []
    timeline = otio.adapters.read_from_string(body["otio"], "otio_json")
    video = timeline.video_tracks()[0]
    assert [c.media_reference.target_url for c in video] == ["D:/footage/1.mp4"] * 2
    first = video[0].source_range
    assert first.start_time.value == round((10.0 - 0.08) * 30)
    assert body["duration_s"] == timeline.duration().to_seconds()


def test_retake_marks_change_the_chosen_take(client: TestClient) -> None:
    pid = project(client)
    asset_id = add_asset(1, footage())
    marks = [{"asset_id": asset_id, "at": 12.6}]
    body = client.post(
        f"/api/projects/{pid}/edit", json={"asset_ids": [asset_id], "retake_marks": marks}
    ).json()
    assert body["takes"][0]["start"] == 5.0


def test_edit_errors(client: TestClient) -> None:
    good = add_asset(1, footage())
    no_words = add_asset(2, None)
    assert client.post("/api/projects/999/edit", json={"asset_ids": [good]}).status_code == 404
    bare = project(client, script=None)
    assert client.post(f"/api/projects/{bare}/edit", json={"asset_ids": [good]}).status_code == 409
    pid = project(client)
    url = f"/api/projects/{pid}/edit"
    assert client.post(url, json={"asset_ids": [no_words]}).status_code == 422
    assert client.post(url, json={"asset_ids": [12345]}).status_code == 404
    assert client.post(url, json={"asset_ids": []}).status_code == 422
