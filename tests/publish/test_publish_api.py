# SPDX-License-Identifier: AGPL-3.0-or-later
"""/api/publish: draft → approve uploads to the mock and stores the remote id; errors and retry."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from reelwright.api.publish import get_youtube
from reelwright.edit.build import edit_to_otio_json
from reelwright.providers.mocks import MockYouTube
from reelwright.providers.registry import CREDENTIALS, Service

from .fixtures import beat_edit

OUT = "D:\\out\\final.mp4"


class FlakyYouTube(MockYouTube):
    fail: str | None = None

    def upload(self, path: str, title: str, **meta: Any) -> str:
        if self.fail:
            raise RuntimeError(self.fail)
        return super().upload(path, title, **meta)


@pytest.fixture
def youtube(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[FlakyYouTube]:
    for name in CREDENTIALS[Service.youtube]:
        monkeypatch.delenv(name, raising=False)  # dev and CI never need real credentials
    mock = FlakyYouTube()
    app: Any = client.app
    app.dependency_overrides[get_youtube] = lambda: mock
    yield mock
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def proxies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("REELWRIGHT_PROXY_DIR", str(tmp_path / "proxies"))
    monkeypatch.setenv("REELWRIGHT_FREE_DISK_FLOOR_MB", "0")
    return tmp_path / "proxies"


def job(client: TestClient, worker: str = "gpu-a") -> dict[str, Any]:
    got = client.get("/api/workers/next", params={"worker": worker})
    assert got.status_code == 200, got.text
    return dict(got.json())


def complete(client: TestClient, j: dict[str, Any], outputs: dict[str, Any]) -> None:
    body = {"worker": "gpu-a", "ok": True, "outputs": outputs}
    assert client.post(f"/api/workers/jobs/{j['id']}/complete", json=body).status_code == 200


def render(client: TestClient, finished: bool = True, preview: bool = True) -> tuple[int, int]:
    """A project and a render driven through the worker endpoints to output (and preview)."""
    pid = int(client.post("/api/projects", json={"title": "Publish API"}).json()["id"])
    hb = {"name": "gpu-a", "vram_total_mb": 12000, "vram_free_mb": 8000, "capabilities": ["nvenc"]}
    assert client.post("/api/workers/heartbeat", json=hb).status_code == 200
    body = {"otio": edit_to_otio_json(beat_edit()), "output_path": OUT}
    rid = int(client.post(f"/api/projects/{pid}/renders", json=body).json()["id"])
    if finished:
        complete(client, job(client), {"returncode": 0})  # the one segment
        complete(client, job(client), {"returncode": 0})  # the concat
    if finished and preview:
        p = job(client)
        put = client.put(
            p["payload"]["upload"], content=b"720p", headers={"Content-Type": "video/mp4"}
        )
        assert put.status_code == 200, put.text
        complete(client, p, {"uploaded": True})
    return pid, rid


def draft(client: TestClient, pid: int, rid: int, **fields: Any) -> dict[str, Any]:
    resp = client.post(
        f"/api/projects/{pid}/publish", json={"render_id": rid, "title": "Hi", **fields}
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def approve(client: TestClient, pub: dict[str, Any], hash_: str | None = None) -> Any:
    body = {"approved_by": "yash", "payload_hash": hash_ or pub["payload_hash"]}
    return client.post(f"/api/publish/{pub['id']}/approve", json=body)


def test_approving_a_draft_uploads_to_the_mock_and_stores_the_remote_id(
    client: TestClient, youtube: FlakyYouTube
) -> None:
    pid, rid = render(client)
    pub = draft(client, pid, rid, tags=["ai"])
    assert pub["status"] == "draft" and pub["remote_id"] is None
    assert pub["payload"]["media_path"] == OUT
    assert {d["field"] for d in pub["diff"]} >= {"title", "tags"}
    assert youtube.uploads == []

    resp = approve(client, pub)
    assert resp.status_code == 200, resp.text
    assert (resp.json()["status"], resp.json()["remote_id"]) == ("live", "mock-1")
    assert [u["id"] for u in youtube.uploads] == ["mock-1"]

    got = client.get(f"/api/publish/{pub['id']}").json()
    assert (got["status"], got["remote_id"]) == ("live", "mock-1")
    [approval] = got["approvals"]
    assert approval["approved_by"] == "yash" and approval["payload_hash"] == pub["payload_hash"]
    assert [p["id"] for p in client.get("/api/publish", params={"status": "live"}).json()] == [
        pub["id"]
    ]
    assert client.get("/api/publish", params={"status": "draft"}).json() == []


def test_future_publish_at_is_scheduled(client: TestClient, youtube: FlakyYouTube) -> None:
    pid, rid = render(client)
    pub = draft(client, pid, rid)
    at = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    put = client.put(f"/api/publish/{pub['id']}", json={"publish_at": at})
    assert put.status_code == 200, put.text
    updated = put.json()
    assert updated["payload_hash"] != pub["payload_hash"]
    assert updated["payload"]["privacy"] == "private" and updated["payload"]["title"] == "Hi"
    resp = approve(client, updated)
    assert resp.status_code == 200 and resp.json()["status"] == "scheduled"
    assert youtube.uploads[0]["privacy"] == "private"
    assert client.put(f"/api/publish/{pub['id']}", json={"title": "x"}).status_code == 409


def test_stale_hash_is_409_and_uploads_nothing(client: TestClient, youtube: FlakyYouTube) -> None:
    pid, rid = render(client)
    pub = draft(client, pid, rid)
    client.put(f"/api/publish/{pub['id']}", json={"title": "Edited after review"})
    resp = approve(client, pub)  # the old hash the reviewer saw
    assert resp.status_code == 409
    assert youtube.uploads == []
    assert client.get(f"/api/publish/{pub['id']}").json()["approvals"] == []


def test_render_without_output_is_409(client: TestClient, youtube: FlakyYouTube) -> None:
    pid, rid = render(client, finished=False)
    pub = draft(client, pid, rid)
    assert approve(client, pub).status_code == 409
    assert youtube.uploads == []


def test_provider_error_is_502_stored_and_retry_goes_live(
    client: TestClient, youtube: FlakyYouTube
) -> None:
    pid, rid = render(client)
    pub = draft(client, pid, rid)
    youtube.fail = "quotaExceeded: daily upload limit reached"
    resp = approve(client, pub)
    assert resp.status_code == 502 and "quotaExceeded" in resp.json()["detail"]
    got = client.get(f"/api/publish/{pub['id']}").json()
    assert got["status"] == "failed" and got["error"].startswith("quotaExceeded")
    youtube.fail = None
    retried = client.post(f"/api/publish/{pub['id']}/retry")
    assert retried.status_code == 200, retried.text
    assert (retried.json()["status"], retried.json()["remote_id"]) == ("live", "mock-1")
    assert len(retried.json()["approvals"]) == 1
    assert client.post(f"/api/publish/{pub['id']}/retry").status_code == 409


def test_preview_and_unknown_ids(client: TestClient, youtube: FlakyYouTube) -> None:
    pid, rid = render(client)
    pub = draft(client, pid, rid)
    assert pub["preview_url"] == f"/api/publish/{pub['id']}/preview"
    got = client.get(pub["preview_url"])
    assert got.status_code == 200 and got.content == b"720p"
    pid2, rid2 = render(client, preview=False)
    bare = draft(client, pid2, rid2)
    assert bare["preview_url"] is None
    assert client.get(f"/api/publish/{bare['id']}/preview").status_code == 404
    assert client.get("/api/publish/999").status_code == 404
    body = {"render_id": rid, "title": "x"}
    assert client.post("/api/projects/999/publish", json=body).status_code == 404
    assert client.post(f"/api/projects/{pid2}/publish", json=body).status_code == 404


def test_schema_rejects_bad_input(client: TestClient, youtube: FlakyYouTube) -> None:
    pid, rid = render(client)
    url = f"/api/projects/{pid}/publish"
    assert client.post(url, json={"render_id": rid, "title": ""}).status_code == 422
    naive = {"render_id": rid, "title": "x", "publish_at": "2030-01-01T10:00:00"}
    assert client.post(url, json=naive).status_code == 422
    http = {
        "render_id": rid,
        "title": "x",
        "affiliate_links": [{"label": "a", "url": "http://x.io"}],
    }
    assert client.post(url, json=http).status_code == 422
    pub = draft(client, pid, rid)
    assert approve(client, pub, "NOTHEX" * 10 + "abcd").status_code == 422
