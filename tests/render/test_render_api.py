# SPDX-License-Identifier: AGPL-3.0-or-later
"""/api/projects/{id}/renders: request, segment and concat completion, preview upload, errors."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from spectracut.edit import Clip, Edit, FrameRate, Track

from reelwright.edit.build import edit_to_otio_json

EDIT = Edit(
    id="e1",
    rate=FrameRate(numerator=30),
    tracks=[
        Track(
            id="v1",
            items=[
                Clip(id="c1", media_ref="D:/f/a.mp4", source_in=0, source_out=2700),
                Clip(id="c2", media_ref="D:/f/b.mp4", source_in=0, source_out=2700),
            ],
        )
    ],
)
OTIO = edit_to_otio_json(EDIT)
OUT = "D:\\out\\final.mp4"


def gpu(client: TestClient, name: str) -> None:
    body = {"name": name, "vram_total_mb": 12000, "vram_free_mb": 8000, "capabilities": ["nvenc"]}
    assert client.post("/api/workers/heartbeat", json=body).status_code == 200


def project(client: TestClient) -> int:
    return int(client.post("/api/projects", json={"title": "Render API"}).json()["id"])


def claim(client: TestClient, worker: str) -> dict[str, Any]:
    job = client.get("/api/workers/next", params={"worker": worker})
    assert job.status_code == 200, job.text
    return dict(job.json())


def finish(client: TestClient, job: dict[str, Any], worker: str, ok: bool = True) -> None:
    done = client.post(
        f"/api/workers/jobs/{job['id']}/complete",
        json={"worker": worker, "ok": ok, "outputs": {"returncode": 0 if ok else 1}},
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert set(body) == {
        "id", "kind", "status", "min_vram_mb", "required_caps", "payload", "worker", "attempts",
        "outputs",
    }  # fmt: skip
    assert body["status"] == ("done" if ok else "failed")  # response shape unchanged


def claim_and_finish(client: TestClient, worker: str, ok: bool = True) -> dict[str, Any]:
    job = claim(client, worker)
    finish(client, job, worker, ok)
    return job


@pytest.fixture
def proxies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("REELWRIGHT_PROXY_DIR", str(tmp_path / "proxies"))
    monkeypatch.setenv("REELWRIGHT_FREE_DISK_FLOOR_MB", "0")
    return tmp_path / "proxies"


def test_full_flow_reports_output_and_preview(client: TestClient, proxies: Path) -> None:
    pid = project(client)
    gpu(client, "gpu-a")
    gpu(client, "gpu-b")
    resp = client.post(f"/api/projects/{pid}/renders", json={"otio": OTIO, "output_path": OUT})
    assert resp.status_code == 201, resp.text
    render = resp.json()
    rid = render["id"]
    assert (render["status"], render["segments"], render["frames"]) == ("queued", 2, 5400)
    assert render["fps"] == 30 and render["output_path"] is None

    seg_a = claim(client, "gpu-a")
    seg_b = claim(client, "gpu-b")  # both cards render at the same time
    assert seg_a["id"] != seg_b["id"]
    finish(client, seg_a, "gpu-a")
    assert client.get(f"/api/projects/{pid}/renders/{rid}").json()["status"] == "rendering"
    finish(client, seg_b, "gpu-b")
    assert {seg_a["payload"]["mode"], seg_b["payload"]["mode"]} == {"segment"}
    assert client.get(f"/api/projects/{pid}/renders/{rid}").json()["status"] == "concatenating"

    concat = claim_and_finish(client, "gpu-a")
    assert concat["payload"]["mode"] == "concat" and concat["payload"]["output"] == OUT
    mid = client.get(f"/api/projects/{pid}/renders/{rid}").json()
    assert (mid["status"], mid["output_path"]) == ("previewing", OUT)

    preview_job = claim(client, "gpu-a")
    assert preview_job["kind"] == "proxy"
    upload = preview_job["payload"]["upload"]
    assert upload == f"/api/projects/{pid}/renders/{rid}/preview"
    put = client.put(upload, content=b"720p-preview", headers={"Content-Type": "video/mp4"})
    assert put.status_code == 200, put.text
    client.post(
        f"/api/workers/jobs/{preview_job['id']}/complete",
        json={"worker": "gpu-a", "ok": True, "outputs": {"uploaded": True}},
    )

    final = client.get(f"/api/projects/{pid}/renders/{rid}").json()
    assert final["status"] == "done"
    assert final["output_path"] == OUT
    assert Path(final["preview_path"]).read_bytes() == b"720p-preview"
    assert Path(final["preview_path"]).parent == proxies.resolve()
    listed = client.get(f"/api/projects/{pid}/renders").json()
    assert [r["id"] for r in listed] == [rid]


def test_failed_segment_is_reported(client: TestClient) -> None:
    pid = project(client)
    gpu(client, "gpu-a")
    rid = client.post(
        f"/api/projects/{pid}/renders", json={"otio": OTIO, "output_path": OUT}
    ).json()["id"]
    claim_and_finish(client, "gpu-a", ok=False)
    got = client.get(f"/api/projects/{pid}/renders/{rid}").json()
    assert got["status"] == "failed" and "returncode 1" in got["error"]


def test_unknown_project_or_render_is_404(client: TestClient) -> None:
    assert (
        client.post(
            "/api/projects/999/renders", json={"otio": OTIO, "output_path": OUT}
        ).status_code
        == 404
    )
    assert client.get("/api/projects/999/renders").status_code == 404
    pid = project(client)
    assert client.get(f"/api/projects/{pid}/renders/5").status_code == 404
    assert client.put(f"/api/projects/{pid}/renders/5/preview", content=b"x").status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"otio": "not an otio document", "output_path": OUT},
        {"otio": '{"OTIO_SCHEMA": "Clip.2"}', "output_path": OUT},
        {"otio": OTIO, "output_path": "final.mp4"},  # not absolute
        {"otio": OTIO, "output_path": "D:/out/final.mov"},  # not .mp4
        {"otio": OTIO},
    ],
)
def test_bad_request_is_422(client: TestClient, body: dict[str, str]) -> None:
    pid = project(client)
    resp = client.post(f"/api/projects/{pid}/renders", json=body)
    assert resp.status_code == 422, resp.text
    assert client.get(f"/api/projects/{pid}/renders").json() == []


def test_unsupported_edit_is_422(client: TestClient) -> None:
    fast = EDIT.model_copy(deep=True)
    fast.tracks[0].items[0] = Clip(id="c1", media_ref="D:/f/a.mp4", source_out=900, speed=2.0)
    pid = project(client)
    resp = client.post(
        f"/api/projects/{pid}/renders", json={"otio": edit_to_otio_json(fast), "output_path": OUT}
    )
    assert resp.status_code == 422 and "speed" in resp.text


def test_preview_below_the_free_disk_floor_is_507(
    client: TestClient, proxies: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = project(client)
    rid = client.post(
        f"/api/projects/{pid}/renders", json={"otio": OTIO, "output_path": OUT}
    ).json()["id"]
    monkeypatch.setenv("REELWRIGHT_FREE_DISK_FLOOR_MB", str(10**12))  # never enough free disk
    resp = client.put(f"/api/projects/{pid}/renders/{rid}/preview", content=b"720p-preview")
    assert resp.status_code == 507
    got = client.get(f"/api/projects/{pid}/renders/{rid}").json()
    assert got["preview_path"] is None and got["status"] != "done"
    assert not any(proxies.glob("render-*")) if proxies.exists() else True
