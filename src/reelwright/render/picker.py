# SPDX-License-Identifier: AGPL-3.0-or-later
"""The free-GPU picker (spike § Runtime topology).

A job goes to the best free card: live heartbeat, not busy, not yielding, low utilisation, enough
free VRAM and every required capability. Hosts are never named here; only heartbeats count.
A CPU worker runs the same job kinds (slower) and is picked only when no live GPU worker could
ever run the job, e.g. in dev and CI. A busy or yielding GPU makes the job wait instead.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Protocol

from reelwright.db.models import Worker

LIVENESS_S = 60
BUSY_UTIL_PCT = 80
GPU_CAPS = frozenset({"cuda", "nvenc"})


class JobLike(Protocol):
    min_vram_mb: int
    required_caps: str


def caps_of(text: str) -> frozenset[str]:
    return frozenset(c.strip() for c in text.split(",") if c.strip())


def as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo; every timestamp here is UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def is_live(worker: Worker, now: datetime) -> bool:
    return (as_utc(now) - as_utc(worker.last_heartbeat)).total_seconds() <= LIVENESS_S


def is_cpu(worker: Worker) -> bool:
    return not (caps_of(worker.capabilities) & GPU_CAPS)


def could_ever_run(worker: Worker, job: JobLike) -> bool:
    """A GPU worker that has the caps and enough total VRAM, free or not."""
    return caps_of(job.required_caps) <= caps_of(worker.capabilities) and (
        worker.vram_total_mb >= job.min_vram_mb
    )


def eligible(worker: Worker, job: JobLike, now: datetime) -> bool:
    """A free GPU card that can run the job right now."""
    return (
        not is_cpu(worker)
        and is_live(worker, now)
        and not worker.busy
        and not worker.yielding
        and worker.utilization_pct < BUSY_UTIL_PCT
        and worker.vram_free_mb >= job.min_vram_mb
        and caps_of(job.required_caps) <= caps_of(worker.capabilities)
    )


def rank(workers: Iterable[Worker], job: JobLike, now: datetime) -> list[Worker]:
    """Eligible GPU workers, best first: most free VRAM, then least busy, then coolest."""
    free = [w for w in workers if eligible(w, job, now)]
    return sorted(free, key=lambda w: (-w.vram_free_mb, w.utilization_pct, w.temperature_c, w.name))


def pick(workers: Iterable[Worker], job: JobLike, now: datetime) -> Worker | None:
    """The worker that should run the job now, or None when it has to wait."""
    pool = list(workers)
    ranked = rank(pool, job, now)
    if ranked:
        return ranked[0]
    live = [w for w in pool if is_live(w, now)]
    if any(not is_cpu(w) and could_ever_run(w, job) for w in live):
        return None
    cpu_caps_needed = caps_of(job.required_caps) - GPU_CAPS
    cpus = [
        w
        for w in live
        if is_cpu(w)
        and not w.busy
        and not w.yielding
        and cpu_caps_needed <= caps_of(w.capabilities)
    ]
    return min(cpus, key=lambda w: w.name) if cpus else None
