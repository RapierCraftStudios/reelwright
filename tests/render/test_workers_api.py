# SPDX-License-Identifier: AGPL-3.0-or-later
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session
from typer.testing import CliRunner

from reelwright.cli.main import app
from reelwright.db.models import WorkerHeartbeat
from reelwright.db.session import get_engine
from reelwright.render.registry import record_heartbeat, utcnow

runner = CliRunner()

HB = {
    "name": "mrdubey-4070ti",
    "host": "mrdubey",
    "gpu_model": "NVIDIA GeForce RTX 4070 Ti",
    "vram_total_mb": 12282,
    "vram_free_mb": 10850,
    "utilization_pct": 7,
    "temperature_c": 41,
    "capabilities": ["nvenc", "cuda", "ffmpeg"],
    "yielding": False,
}


def test_heartbeat_then_list_workers_api_and_cli(client: TestClient) -> None:
    r = client.post("/api/workers/heartbeat", json=HB)
    assert r.status_code == 200, r.text
    assert r.json()["capabilities"] == ["cuda", "ffmpeg", "nvenc"]
    client.post("/api/workers/heartbeat", json={**HB, "name": "legion", "yielding": True})

    listed = client.get("/api/workers").json()
    assert [w["name"] for w in listed] == ["legion", "mrdubey-4070ti"]
    assert listed[1]["vram_free_mb"] == 10850 and listed[1]["temperature_c"] == 41

    result = runner.invoke(app, ["workers"])
    assert result.exit_code == 0, result.output
    assert (
        "mrdubey-4070ti\tmrdubey\tNVIDIA GeForce RTX 4070 Ti\t10850/12282 MB\t7%\t41C\t"
        "cuda,ffmpeg,nvenc\tready"
    ) in result.output
    assert "legion\t" in result.output and result.output.rstrip().split("\n")[0].endswith("yield")


def test_rw_workers_hides_stale_workers(client: TestClient) -> None:
    with Session(get_engine()) as session:
        stale = WorkerHeartbeat(name="gone", capabilities=["cuda"])
        record_heartbeat(session, stale, utcnow() - timedelta(seconds=120))
    assert client.get("/api/workers").json() == []
    result = runner.invoke(app, ["workers"])
    assert result.exit_code == 0, result.output
    assert "No live workers." in result.output


def test_next_is_204_when_queue_empty(client: TestClient) -> None:
    client.post("/api/workers/heartbeat", json=HB)
    r = client.get("/api/workers/next", params={"worker": HB["name"]})
    assert r.status_code == 204


def test_worker_token_enforced_when_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REELWRIGHT_WORKER_TOKEN", "t0ken-for-test")
    assert client.post("/api/workers/heartbeat", json=HB).status_code == 401
    bad = {"Authorization": "Bearer wrong"}
    assert client.post("/api/workers/heartbeat", json=HB, headers=bad).status_code == 401
    assert client.get("/api/workers/next", params={"worker": "x"}).status_code == 401
    good = {"Authorization": "Bearer t0ken-for-test"}
    assert client.post("/api/workers/heartbeat", json=HB, headers=good).status_code == 200
    assert client.get("/api/workers").status_code == 200


def test_complete_unknown_job_is_404(client: TestClient) -> None:
    r = client.post("/api/workers/jobs/42/complete", json={"worker": "w", "ok": True})
    assert r.status_code == 404
