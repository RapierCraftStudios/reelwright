# SPDX-License-Identifier: AGPL-3.0-or-later
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
    Agent,
    ResourcePolicy,
    check_server_url,
    popen_kwargs,
    run_limited,
)

PAYLOAD = {"input": "in.mov", "output": "out.mp4"}


def cpu_probe(**_: Any) -> dict[str, Any]:
    return {"host": "ci", "capabilities": ["cpu", "ffmpeg"], "yielding": False}


def test_cpu_and_gpu_executors_cover_the_same_job_kinds() -> None:
    assert set(CPU_EXECUTORS) == set(GPU_EXECUTORS) == set(JobKind)
    for kind in JobKind:
        cpu_argv = CPU_EXECUTORS[kind](PAYLOAD)
        assert "in.mov" in cpu_argv
        assert not {"cuda", "h264_nvenc"} & set(cpu_argv)
        assert {"cuda", "h264_nvenc"} & set(GPU_EXECUTORS[kind](PAYLOAD))


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
