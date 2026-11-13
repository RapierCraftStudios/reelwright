# SPDX-License-Identifier: AGPL-3.0-or-later
"""Worker-side GPU probe: model, VRAM, utilisation, temperature, capabilities and a yield flag.

The yield flag tells the picker to leave this card alone: a full-screen app is in front (the
owner is working or playing on MrDubey) or another process holds a lot of GPU memory.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from typing import Any

NVIDIA_SMI_QUERY = [
    "nvidia-smi",
    "--query-gpu=name,memory.total,memory.free,utilization.gpu,temperature.gpu",
    "--format=csv,noheader,nounits",
]
COMPUTE_APPS_QUERY = [
    "nvidia-smi",
    "--query-compute-apps=pid,used_memory",
    "--format=csv,noheader,nounits",
]
HEAVY_GPU_MB = 1024

# Runs a command and returns stdout, or None when the tool is missing or fails.
Runner = Callable[[list[str]], str | None]


@dataclass(frozen=True)
class GpuStats:
    model: str
    vram_total_mb: int
    vram_free_mb: int
    utilization_pct: int
    temperature_c: int


def _int(field: str) -> int:
    try:
        return int(float(field.strip()))
    except ValueError:  # "[N/A]", "[Not Supported]"
        return 0


def parse_nvidia_smi(text: str) -> list[GpuStats]:
    gpus = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5 or not parts[0]:
            continue
        gpus.append(GpuStats(parts[0], *(_int(p) for p in parts[1:5])))
    return gpus


def parse_compute_apps(text: str) -> list[tuple[int, int]]:
    """(pid, used MB) for every process holding GPU memory."""
    apps = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit():
            apps.append((int(parts[0]), _int(parts[1])))
    return apps


def compute_yield(
    fullscreen: bool, foreign_gpu_procs_mb: Iterable[int], threshold_mb: int = HEAVY_GPU_MB
) -> bool:
    """Yield when a full-screen app is in front or a foreign process holds >= threshold MB."""
    return fullscreen or any(mb >= threshold_mb for mb in foreign_gpu_procs_mb)


def fullscreen_foreground() -> bool:
    """Whether the foreground window covers its whole monitor (Windows only)."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd or hwnd in (user32.GetDesktopWindow(), user32.GetShellWindow()):
        return False
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info)):
        return False
    m = info.rcMonitor
    return bool(
        rect.left <= m.left
        and rect.top <= m.top
        and rect.right >= m.right
        and rect.bottom >= m.bottom
    )


def run_tool(argv: list[str]) -> str | None:
    if shutil.which(argv[0]) is None:
        return None
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def probe(
    *,
    gpu: bool = True,
    run: Runner = run_tool,
    which: Callable[[str], str | None] = shutil.which,
    fullscreen: Callable[[], bool] = fullscreen_foreground,
    own_pids: Collection[int] = (),
) -> dict[str, Any]:
    """A heartbeat body without the worker name. With no NVIDIA GPU it is a CPU-only snapshot."""
    tools = ["ffmpeg"] if which("ffmpeg") else []
    snapshot: dict[str, Any] = {
        "host": socket.gethostname(),
        "gpu_model": "",
        "vram_total_mb": 0,
        "vram_free_mb": 0,
        "utilization_pct": 0,
        "temperature_c": 0,
        "capabilities": ["cpu", *tools],
        "yielding": compute_yield(fullscreen(), []),
    }
    out = run(NVIDIA_SMI_QUERY) if gpu else None
    gpus = parse_nvidia_smi(out) if out else []
    if not gpus:
        return snapshot
    card = gpus[0]  # one heartbeat per card; multi-GPU hosts run one agent per card
    apps = parse_compute_apps(run(COMPUTE_APPS_QUERY) or "")
    foreign = [mb for pid, mb in apps if pid not in own_pids]
    snapshot.update(
        gpu_model=card.model,
        vram_total_mb=card.vram_total_mb,
        vram_free_mb=card.vram_free_mb,
        utilization_pct=card.utilization_pct,
        temperature_c=card.temperature_c,
        capabilities=["cuda", "nvenc", *tools],
        yielding=compute_yield(fullscreen(), foreign),
    )
    return snapshot
