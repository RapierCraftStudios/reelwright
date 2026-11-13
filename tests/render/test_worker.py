# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import subprocess
import sys
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from reelwright.db.models import RenderJob
from reelwright.db.session import get_engine
from reelwright.providers.mocks import LocalCpuWorker
from reelwright.render import queue
from reelwright.render.queue import JobKind
from reelwright.render.worker import (
    BELOW_NORMAL_PRIORITY_CLASS,
    CPU_EXECUTORS,
    GPU_EXECUTORS,
    QA_JOB,
    RENDER_JOB,
    Agent,
    ResourcePolicy,
    check_server_url,
    popen_kwargs,
    run_limited,
)

PAYLOAD = {"input": "in.mov", "output": "out.mp4"}


def cpu_probe(**_: Any) -> dict[str, Any]:
    return {"host": "ci", "capabilities": ["cpu", "ffmpeg"], "yielding": False}


def _spec(argv: list[str]) -> dict[str, Any]:
    path = argv[argv.index("--spec") + 1]
    try:
        return dict(json.loads(open(path, encoding="utf-8").read()))
    finally:
        os.unlink(path)


def test_cpu_and_gpu_executors_cover_the_same_job_kinds() -> None:
    assert set(CPU_EXECUTORS) == set(GPU_EXECUTORS) == set(JobKind)
    for kind in JobKind:
        cpu_argv = CPU_EXECUTORS[kind](PAYLOAD)
        gpu_argv = GPU_EXECUTORS[kind](PAYLOAD)
        if kind == JobKind.render:  # the payload goes in a spec file, not on the command line
            assert cpu_argv[:2] == [sys.executable, str(RENDER_JOB)]
            assert cpu_argv[cpu_argv.index("--encoder") + 1] == "libx264"
            assert gpu_argv[gpu_argv.index("--encoder") + 1] == "h264_nvenc"
            assert gpu_argv[gpu_argv.index("--hwaccel") + 1] == "cuda"
            assert _spec(cpu_argv) == _spec(gpu_argv) == PAYLOAD
        else:
            assert "in.mov" in cpu_argv
        assert not {"cuda", "h264_nvenc"} & set(cpu_argv)
        if kind == JobKind.qa:  # ffmpeg analysis filters only: CPU work on any worker
            assert cpu_argv == gpu_argv
        else:
            assert {"cuda", "h264_nvenc"} & set(gpu_argv)


def test_qa_executor_runs_the_qa_job_script() -> None:
    argv = CPU_EXECUTORS[JobKind.qa](PAYLOAD)
    assert argv[:2] == [sys.executable, str(QA_JOB)] and QA_JOB.name == "qa.py"
    assert QA_JOB.is_file()
    assert argv[argv.index("--input") + 1] == "in.mov"
    assert argv[argv.index("--output") + 1] == PAYLOAD["output"]


def test_render_spec_file_holds_the_whole_payload() -> None:
    payload = {"render_id": 3, "mode": "segment", "otio": "x" * 40_000, "output": "D:/o.seg0.mov"}
    argv = GPU_EXECUTORS[JobKind.render](payload)
    assert sum(len(a) for a in argv) < 32767  # fits a Windows command line
    spec = argv[argv.index("--spec") + 1]
    assert os.path.basename(spec).startswith("rw-render-")
    assert _spec(argv) == payload


def test_mock_cpu_worker_declares_every_job_kind() -> None:
    mock = LocalCpuWorker()
    assert mock.kinds == tuple(JobKind) and "cpu" in mock.capabilities


def test_cpu_agent_runs_a_gpu_kind_job_end_to_end(client: TestClient) -> None:
    with Session(get_engine()) as session:
        job = queue.enqueue(session, JobKind.transcode, PAYLOAD)  # requires nvenc on a GPU
        job_id = job.id
    ran: list[list[str]] = []

    def fake_runner(argv: list[str], policy: ResourcePolicy) -> int:
        ran.append(argv)
        assert policy.nice
        return 0

    agent = Agent(
        "http://127.0.0.1:8000",
        "ci-cpu",
        cpu=True,
        client=client,
        runner=fake_runner,
        probe=cpu_probe,
    )
    assert agent.tick() is True
    assert ran and ran[0][0] == "ffmpeg" and "libx264" in ran[0]
    with Session(get_engine()) as session:
        done = session.get(RenderJob, job_id)
        assert done is not None
        assert (done.status, done.worker) == ("done", "ci-cpu")
        assert queue.job_to_read(done).outputs["backend"] == "cpu"
    assert agent.tick() is False


def test_bad_payload_fails_the_job_instead_of_crashing() -> None:
    agent = Agent("https://x.example", "w", cpu=True, runner=lambda a, p: 0, probe=cpu_probe)
    ok, outputs = agent.execute({"id": 1, "kind": "render", "payload": {}})
    assert ok is False and "'output'" in outputs["error"]
    ok, outputs = agent.execute({"id": 1, "kind": "proxy", "payload": {}})
    assert ok is False and "input" in outputs["error"]


def test_popen_kwargs_windows_low_priority() -> None:
    kwargs = popen_kwargs(ResourcePolicy(nice=True, mem_cap_mb=4096), platform="win32")
    assert kwargs == {"creationflags": BELOW_NORMAL_PRIORITY_CLASS}
    assert popen_kwargs(ResourcePolicy(nice=False), platform="win32") == {}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX limits")
def test_posix_job_runs_niced_with_ram_cap() -> None:
    policy = ResourcePolicy(nice=True, mem_cap_mb=2048)
    probe_code = "import os, resource; print(os.nice(0), resource.getrlimit(resource.RLIMIT_AS)[0])"
    out = subprocess.run(
        [sys.executable, "-c", probe_code],
        capture_output=True,
        text=True,
        check=True,
        **popen_kwargs(policy),
    ).stdout.split()
    assert int(out[0]) == min(os.nice(0) + 10, 19)
    assert int(out[1]) == 2048 * 1024 * 1024
    assert run_limited([sys.executable, "-c", "raise SystemExit(3)"], policy) == 3


def test_server_url_must_be_https_unless_loopback() -> None:
    assert check_server_url("https://devbox.example.ts.net")
    assert check_server_url("http://127.0.0.1:8000")
    assert check_server_url("http://localhost:8000")
    for bad in ("http://100.64.0.7:8000", "http://devbox.example.ts.net", "ftp://x", "https://"):
        with pytest.raises(ValueError):
            check_server_url(bad)


def test_proxy_job_uploads_output_then_removes_scratch(
    client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REELWRIGHT_PROXY_DIR", str(tmp_path / "proxies"))
    monkeypatch.setenv("REELWRIGHT_FREE_DISK_FLOOR_MB", "0")
    entry = {
        "path": "D:\\DJI\\DJI_0003.MP4",
        "host": "mrdubey",
        "checksum": "c" * 64,
        "size_bytes": 10,
        "kind": "camera",
        "duration_s": 2.0,
    }
    (asset,) = client.post("/api/assets/manifest", json={"entries": [entry]}).json()
    monkeypatch.chdir(tmp_path)

    def fake_encode(argv: list[str], policy: ResourcePolicy) -> int:
        (tmp_path / argv[-1]).write_bytes(b"720p-proxy")
        return 0

    agent = Agent(
        "http://127.0.0.1:8000",
        "ci-cpu",
        cpu=True,
        client=client,
        runner=fake_encode,
        probe=cpu_probe,
    )
    assert agent.tick() is True
    with Session(get_engine()) as session:
        job = session.get(RenderJob, asset["proxy_job_id"])
        assert job is not None and job.status == "done"
        assert queue.job_to_read(job).outputs["uploaded"] is True
    (listed,) = client.get("/api/assets").json()
    assert listed["proxy_path"] and open(listed["proxy_path"], "rb").read() == b"720p-proxy"
    assert not (tmp_path / f"rw-proxy-{asset['id']}.mp4").exists()


def test_proxy_upload_rejected_below_floor_fails_the_job(
    client: TestClient, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REELWRIGHT_PROXY_DIR", str(tmp_path / "proxies"))
    monkeypatch.setenv("REELWRIGHT_FREE_DISK_FLOOR_MB", str(10**12))  # never enough free disk
    out = tmp_path / "rw-proxy-1.mp4"
    out.write_bytes(b"720p-proxy")
    entry = {"path": "/m/a.mp4", "checksum": "d" * 64, "size_bytes": 1, "kind": "camera"}
    (asset,) = client.post("/api/assets/manifest", json={"entries": [entry]}).json()
    agent = Agent(
        "http://127.0.0.1:8000",
        "w",
        cpu=True,
        client=client,
        runner=lambda a, p: 0,
        probe=cpu_probe,
    )
    payload = {
        "input": "/m/a.mp4",
        "output": str(out),
        "upload": f"/api/assets/{asset['id']}/proxy",
    }
    ok, outputs = agent.execute({"id": 1, "kind": "proxy", "payload": payload})
    assert ok is False and outputs["uploaded"] is False and "507" in outputs["error"]
    assert out.exists()  # kept for a retry
    assert client.get("/api/assets").json()[0]["proxy_path"] is None
    ok, outputs = agent.execute(
        {"id": 1, "kind": "proxy", "payload": {**payload, "upload": "https://evil.example/x"}}
    )
    assert ok is False and "/api/" in outputs["error"]
