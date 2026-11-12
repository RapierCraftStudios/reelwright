# SPDX-License-Identifier: AGPL-3.0-or-later
"""The worker agent: heartbeat, pull the next job over HTTPS, run it under a resource policy.

The GPU and CPU executor tables cover the same job kinds; the CPU one is slower but lets dev and
CI run every job without a GPU. Jobs run at low CPU priority and, when a cap is set, with a RAM
cap, because MrDubey is the owner's work PC. The bearer token comes from REELWRIGHT_WORKER_TOKEN.
"""

from __future__ import annotations

import ipaddress
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from reelwright.render import probe as probe_mod
from reelwright.render.queue import JobKind

TOKEN_ENV = "REELWRIGHT_WORKER_TOKEN"
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
UPLOAD_CHUNK = 1024 * 1024

ArgvBuilder = Callable[[Mapping[str, Any]], list[str]]


def _io(payload: Mapping[str, Any]) -> tuple[str, str]:
    try:
        return str(payload["input"]), str(payload["output"])
    except KeyError as e:
        raise ValueError(f"job payload needs {e.args[0]!r}") from None


def _ffmpeg(hw: bool, vcodec: str, *filters: str) -> ArgvBuilder:
    def build(payload: Mapping[str, Any]) -> list[str]:
        src, dst = _io(payload)
        argv = ["ffmpeg", "-hide_banner", "-y"]
        if hw:
            argv += ["-hwaccel", "cuda"]
        argv += ["-i", src, *filters, "-c:v", vcodec, "-c:a", "aac", dst]
        return argv

    return build


TRANSCRIBE_JOB = Path(__file__).resolve().parents[3] / "workers" / "gpu" / "jobs" / "transcribe.py"


def _transcribe(device: str, compute_type: str) -> ArgvBuilder:
    """WhisperX (faster-whisper fallback) via the job script; it falls back to CPU itself."""

    def build(payload: Mapping[str, Any]) -> list[str]:
        src, dst = _io(payload)
        return [
            sys.executable,
            str(TRANSCRIBE_JOB),
            "--input",
            src,
            "--output",
            dst,
            "--device",
            device,
            "--compute-type",
            compute_type,
        ]

    return build


GPU_EXECUTORS: dict[JobKind, ArgvBuilder] = {
    JobKind.transcode: _ffmpeg(True, "h264_nvenc", "-preset", "p5"),
    JobKind.proxy: _ffmpeg(True, "h264_nvenc", "-vf", "scale=-2:720", "-preset", "p1"),
    JobKind.render: _ffmpeg(True, "h264_nvenc", "-preset", "p7", "-rc", "vbr"),
    JobKind.transcribe: _transcribe("cuda", "float16"),
}

CPU_EXECUTORS: dict[JobKind, ArgvBuilder] = {
    JobKind.transcode: _ffmpeg(False, "libx264", "-preset", "medium"),
    JobKind.proxy: _ffmpeg(False, "libx264", "-vf", "scale=-2:720", "-preset", "veryfast"),
    JobKind.render: _ffmpeg(False, "libx264", "-preset", "slow"),
    JobKind.transcribe: _transcribe("cpu", "int8"),
}


@dataclass(frozen=True)
class ResourcePolicy:
    """Low CPU priority, and an optional per-process RAM cap in MB."""

    nice: bool = True
    mem_cap_mb: int | None = None


def _posix_limits(policy: ResourcePolicy) -> Callable[[], None]:
    def apply() -> None:
        import resource

        if policy.nice:
            os.nice(10)
        if policy.mem_cap_mb:
            cap = policy.mem_cap_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (cap, cap))

    return apply


def popen_kwargs(policy: ResourcePolicy, platform: str = sys.platform) -> dict[str, Any]:
    """Popen kwargs for the policy. On Windows the RAM cap is a Job Object set after spawn."""
    if platform == "win32":
        return {"creationflags": BELOW_NORMAL_PRIORITY_CLASS} if policy.nice else {}
    return {"preexec_fn": _posix_limits(policy)}


def _assign_job_object(proc: subprocess.Popen[bytes], mem_cap_mb: int) -> object:
    """Cap the process's committed memory with a Windows Job Object; returns the job handle."""
    if sys.platform != "win32":
        raise OSError("job objects are Windows-only")
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in ("Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")
        ]

    class EXTENDED(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC),
            ("IoInfo", IO),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    job = kernel32.CreateJobObjectW(None, None)
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_PROCESS_MEMORY
    info.ProcessMemoryLimit = mem_cap_mb * 1024 * 1024
    extended_limit_information = 9
    kernel32.SetInformationJobObject(
        job, extended_limit_information, ctypes.byref(info), ctypes.sizeof(info)
    )
    kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(getattr(proc, "_handle"))))  # noqa: B009
    return job


def run_limited(argv: list[str], policy: ResourcePolicy) -> int:
    """Run argv under the policy and return its exit code."""
    proc = subprocess.Popen(argv, **popen_kwargs(policy))
    job = None
    if sys.platform == "win32" and policy.mem_cap_mb:
        job = _assign_job_object(proc, policy.mem_cap_mb)
    try:
        return proc.wait()
    finally:
        del job


LOOPBACK_NAMES = {"localhost"}


def check_server_url(url: str) -> str:
    """Jobs are pulled over HTTPS (Tailscale serve); plain http only to loopback."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme == "https" and host:
        return url
    if parts.scheme == "http":
        if host in LOOPBACK_NAMES:
            return url
        try:
            if ipaddress.ip_address(host).is_loopback:
                return url
        except ValueError:
            pass
    raise ValueError(f"worker server URL must be https:// (or http:// to loopback), got {url!r}")


Runner = Callable[[list[str], ResourcePolicy], int]


class Agent:
    """One worker: heartbeat, pull, execute, report."""

    def __init__(
        self,
        server: str,
        name: str,
        *,
        cpu: bool = False,
        token: str | None = None,
        client: httpx.Client | None = None,
        runner: Runner = run_limited,
        probe: Callable[..., dict[str, Any]] = probe_mod.probe,
        policy: ResourcePolicy | None = None,
        heartbeat_interval_s: float = 15.0,
    ) -> None:
        check_server_url(server)
        self.name = name
        self.cpu = cpu
        self.runner = runner
        self.probe = probe
        self.policy = policy or ResourcePolicy()
        self.heartbeat_interval_s = heartbeat_interval_s
        self.executors = CPU_EXECUTORS if cpu else GPU_EXECUTORS
        token = token if token is not None else os.environ.get(TOKEN_ENV)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = client or httpx.Client(base_url=server, timeout=30.0)
        self.client.headers.update(headers)

    def heartbeat(self, busy: bool = False) -> None:
        body = {**self.probe(gpu=not self.cpu), "name": self.name, "busy": busy}
        self.client.post("/api/workers/heartbeat", json=body).raise_for_status()

    def execute(self, job: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
        payload = job.get("payload") or {}
        try:
            argv = self.executors[JobKind(job["kind"])](payload)
        except (ValueError, KeyError) as e:
            return False, {"error": str(e)}
        stop = threading.Event()

        def keep_alive() -> None:
            while not stop.wait(self.heartbeat_interval_s):
                try:
                    self.heartbeat(busy=True)
                except httpx.HTTPError:
                    pass

        beat = threading.Thread(target=keep_alive, daemon=True)
        beat.start()
        try:
            rc = self.runner(argv, self.policy)
        except OSError as e:
            return False, {"error": str(e)}
        finally:
            stop.set()
            beat.join()
        outputs: dict[str, Any] = {"returncode": rc, "backend": "cpu" if self.cpu else "gpu"}
        if "output" in payload:
            outputs["output"] = payload["output"]
        if rc == 0 and payload.get("upload"):
            return self.upload(payload, outputs)
        return rc == 0, outputs

    def upload(
        self, payload: Mapping[str, Any], outputs: dict[str, Any]
    ) -> tuple[bool, dict[str, Any]]:
        """PUT the local output to the server path in payload['upload'], then delete it."""
        src = Path(str(payload.get("output", "")))
        target = str(payload["upload"])
        if not target.startswith("/api/"):
            return False, {**outputs, "error": f"upload must be an /api/ path, got {target!r}"}

        def chunks() -> Iterator[bytes]:
            with src.open("rb") as fh:
                while chunk := fh.read(UPLOAD_CHUNK):
                    yield chunk

        try:
            content_type = str(payload.get("content_type") or "video/mp4")
            resp = self.client.put(target, content=chunks(), headers={"Content-Type": content_type})
            resp.raise_for_status()
        except (OSError, httpx.HTTPError) as e:
            return False, {**outputs, "uploaded": False, "error": f"upload failed: {e}"}
        src.unlink(missing_ok=True)
        return True, {**outputs, "uploaded": True}

    def tick(self) -> bool:
        """Heartbeat, pull one job and run it. False when there was no job."""
        self.heartbeat()
        resp = self.client.get("/api/workers/next", params={"worker": self.name})
        resp.raise_for_status()
        if resp.status_code == 204:
            return False
        job = resp.json()
        ok, outputs = self.execute(job)
        body = {"worker": self.name, "ok": ok, "outputs": outputs}
        self.client.post(f"/api/workers/jobs/{job['id']}/complete", json=body).raise_for_status()
        return True

    def run_forever(self, interval_s: float = 5.0) -> None:
        while True:
            try:
                worked = self.tick()
            except httpx.HTTPError as e:
                print(f"worker {self.name}: {e}", file=sys.stderr)
                worked = False
            if not worked:
                time.sleep(interval_s)
