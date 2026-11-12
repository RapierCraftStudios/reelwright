# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from reelwright.db.models import AssetKind
from reelwright.db.session import get_engine
from reelwright.ingest.manifest import classify_kind
from reelwright.render import queue
from reelwright.render.registry import utcnow

DJI = "D:\\DJI\\DCIM\\100MEDIA\\DJI_0001.MP4"
SUM = hashlib.sha256(b"dji-0001").hexdigest()
ENTRY: dict[str, Any] = {
    "path": DJI,
    "host": "mrdubey",
    "checksum": SUM,
    "size_bytes": 734003200,
    "kind": "camera",
    "duration_s": 312.48,
}
GPU = {
    "name": "mrdubey-4070ti",
    "host": "mrdubey",
    "gpu_model": "NVIDIA GeForce RTX 4070 Ti",
    "vram_total_mb": 12282,
    "vram_free_mb": 10850,
    "capabilities": ["nvenc", "cuda", "ffmpeg"],
}


def test_classify_kind_by_watched_folder_and_type() -> None:
    assert classify_kind(DJI, ["D:\\DJI"], ["D:\\Recordly"]) is AssetKind.camera
    assert classify_kind("d:\\recordly\\2026\\take.mp4", ["D:\\DJI"], ["D:\\Recordly"]) is (
        AssetKind.screen
    )
    assert classify_kind("/media/x/voice.WAV") is AssetKind.audio
    assert classify_kind("/media/x/city.mov") is AssetKind.broll
    assert classify_kind("D:\\DJIx\\a.mp4", ["D:\\DJI"]) is AssetKind.broll


def test_manifest_creates_asset_with_checksum_duration_and_kind(client: TestClient) -> None:
    r = client.post("/api/assets/manifest", json={"entries": [ENTRY]})
    assert r.status_code == 201, r.text
    (asset,) = r.json()
    assert (asset["checksum"], asset["duration_s"], asset["kind"]) == (SUM, 312.48, "camera")
    assert asset["proxy_job_id"] is not None and asset["proxy_path"] is None

    listed = client.get("/api/assets").json()
    assert [(a["path"], a["checksum"], a["kind"]) for a in listed] == [(DJI, SUM, "camera")]

    again = client.post("/api/assets/manifest", json={"entries": [ENTRY]})
    assert again.status_code == 201
    assert again.json()[0]["id"] == asset["id"]
    assert again.json()[0]["proxy_job_id"] == asset["proxy_job_id"]
    assert len(client.get("/api/assets").json()) == 1


def test_manifest_rejects_bad_checksum(client: TestClient) -> None:
    r = client.post("/api/assets/manifest", json={"entries": [{**ENTRY, "checksum": "nope"}]})
    assert r.status_code == 422


def test_audio_gets_no_proxy_job(client: TestClient) -> None:
    audio = {**ENTRY, "path": "D:\\Audio\\vo.wav", "kind": "audio", "checksum": "a" * 64}
    (asset,) = client.post("/api/assets/manifest", json={"entries": [audio]}).json()
    assert asset["proxy_job_id"] is None


def test_manifest_needs_worker_token_when_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REELWRIGHT_WORKER_TOKEN", "t0ken-for-test")
    assert client.post("/api/assets/manifest", json={"entries": [ENTRY]}).status_code == 401
    good = {"Authorization": "Bearer t0ken-for-test"}
    r = client.post("/api/assets/manifest", json={"entries": [ENTRY]}, headers=good)
    assert r.status_code == 201
    assert client.put("/api/assets/1/proxy", content=b"x").status_code == 401


def test_proxy_job_goes_to_a_gpu_worker_via_the_picker(client: TestClient) -> None:
    client.post("/api/workers/heartbeat", json={**GPU, "name": "legion", "yielding": True})
    client.post("/api/workers/heartbeat", json=GPU)
    (asset,) = client.post("/api/assets/manifest", json={"entries": [ENTRY]}).json()

    assert client.get("/api/workers/next", params={"worker": "legion"}).status_code == 204
    r = client.get("/api/workers/next", params={"worker": GPU["name"]})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["id"] == asset["proxy_job_id"] and job["kind"] == "proxy"
    assert job["required_caps"] == ["nvenc"]
    assert job["payload"]["input"] == DJI
    assert job["payload"]["upload"] == f"/api/assets/{asset['id']}/proxy"
    with Session(get_engine()) as session:
        assert queue.next_job_for(session, "legion", utcnow()) is None
