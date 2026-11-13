# SPDX-License-Identifier: AGPL-3.0-or-later
"""Proxies on the dev server: a disk budget, a free-disk floor and a guarded streaming write.

The dev server is nearly full (spike § Guardrails), so raw footage never lands here. Only 720p
proxies do. Each proxy write is checked against the proxy budget (bytes under REELWRIGHT_PROXY_DIR)
and the free-disk floor before it starts and after every chunk. A write that would cross either
limit is aborted and its partial file deleted.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlmodel import Session

from reelwright.db.models import Asset
from reelwright.render import queue
from reelwright.render.queue import JobKind

PROXY_DIR_ENV = "REELWRIGHT_PROXY_DIR"
BUDGET_ENV = "REELWRIGHT_PROXY_BUDGET_MB"
FLOOR_ENV = "REELWRIGHT_FREE_DISK_FLOOR_MB"
DEFAULT_PROXY_DIR = "./data/proxies"
DEFAULT_BUDGET_MB = 10240
DEFAULT_FLOOR_MB = 5120
MB = 1024 * 1024

DiskUsage = Callable[[Path], Any]  # returns an object with .free (bytes), like shutil.disk_usage


class DiskBudgetExceeded(OSError):
    """A proxy write would exceed the proxy budget or drop free disk below the floor."""


def _disk_usage(path: Path) -> Any:
    return shutil.disk_usage(path)


@dataclass
class ProxyBudget:
    proxy_dir: Path
    budget_bytes: int
    floor_bytes: int
    disk_usage: DiskUsage = field(default=_disk_usage)

    @classmethod
    def from_env(cls, disk_usage: DiskUsage = _disk_usage) -> ProxyBudget:
        return cls(
            proxy_dir=Path(os.environ.get(PROXY_DIR_ENV) or DEFAULT_PROXY_DIR),
            budget_bytes=int(os.environ.get(BUDGET_ENV) or DEFAULT_BUDGET_MB) * MB,
            floor_bytes=int(os.environ.get(FLOOR_ENV) or DEFAULT_FLOOR_MB) * MB,
            disk_usage=disk_usage,
        )

    def used_bytes(self) -> int:
        if not self.proxy_dir.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.proxy_dir.rglob("*") if p.is_file())

    def free_bytes(self) -> int:
        self.proxy_dir.mkdir(parents=True, exist_ok=True)
        return int(self.disk_usage(self.proxy_dir).free)


def check_disk(budget: ProxyBudget, incoming_bytes: int, used_bytes: int | None = None) -> None:
    """Raise DiskBudgetExceeded unless incoming_bytes more fits the budget and keeps the floor."""
    used = budget.used_bytes() if used_bytes is None else used_bytes
    if used + incoming_bytes > budget.budget_bytes:
        raise DiskBudgetExceeded(
            f"proxy budget exceeded: {used + incoming_bytes} > {budget.budget_bytes} bytes"
        )
    free = budget.free_bytes()
    if free - incoming_bytes < budget.floor_bytes:
        raise DiskBudgetExceeded(
            f"free disk below floor: {free - incoming_bytes} < {budget.floor_bytes} bytes"
        )


def _dest(budget: ProxyBudget, dest_name: str) -> Path:
    root = budget.proxy_dir.resolve()
    dest = (root / dest_name).resolve()
    if dest == root or not dest.is_relative_to(root) or dest.name.endswith(".part"):
        raise ValueError(f"proxy destination must stay inside the proxy dir: {dest_name!r}")
    return dest


def guarded_write(chunks: Iterable[bytes], dest_name: str, budget: ProxyBudget) -> Path:
    """Stream chunks to proxy_dir/dest_name, checking budget and floor before and per chunk."""
    dest = _dest(budget, dest_name)
    existing = dest.stat().st_size if dest.is_file() else 0
    used = budget.used_bytes() - existing  # the file is replaced, so it doesn't count
    check_disk(budget, 0, used)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    written = 0
    try:
        with part.open("wb") as fh:
            for chunk in chunks:
                if not chunk:
                    continue
                check_disk(budget, len(chunk), used + written)
                fh.write(chunk)
                fh.flush()  # free-disk readings must see what we wrote
                written += len(chunk)
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return dest


def proxy_name(asset_id: int) -> str:
    return f"asset-{asset_id}.proxy.mp4"


def enqueue_proxy(session: Session, asset: Asset) -> int:
    """Queue a 720p proxy encode. The picker chooses the worker (nvenc by default), never a host."""
    assert asset.id is not None
    payload = {
        "asset_id": asset.id,
        "input": asset.path,
        "output": f"rw-proxy-{asset.id}.mp4",
        "upload": f"/api/assets/{asset.id}/proxy",
    }
    job = queue.enqueue(session, JobKind.proxy, payload)
    assert job.id is not None
    asset.proxy_job_id = job.id
    session.add(asset)
    session.commit()
    session.refresh(asset)
    return job.id
