# SPDX-License-Identifier: AGPL-3.0-or-later
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from reelwright.db.models import Worker
from reelwright.render.picker import pick, rank

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def gpu(
    name: str,
    *,
    free: int = 8000,
    total: int = 12000,
    util: int = 5,
    temp: int = 50,
    caps: str = "cuda,ffmpeg,nvenc",
    yielding: bool = False,
    busy: bool = False,
    age: float = 0,
) -> Worker:
    return Worker(
        name=name,
        gpu_model="RTX",
        vram_total_mb=total,
        vram_free_mb=free,
        utilization_pct=util,
        temperature_c=temp,
        capabilities=caps,
        yielding=yielding,
        busy=busy,
        last_heartbeat=NOW - timedelta(seconds=age),
    )


def cpu(name: str = "ci-cpu", age: float = 0, busy: bool = False) -> Worker:
    return Worker(
        name=name, capabilities="cpu,ffmpeg", busy=busy, last_heartbeat=NOW - timedelta(seconds=age)
    )


@dataclass
class Job:
    min_vram_mb: int = 0
    required_caps: str = "nvenc"


def test_picks_free_card_with_most_free_vram() -> None:
    a, b = gpu("legion", free=6000, total=8000), gpu("mrdubey", free=10000)
    assert pick([a, b], Job(min_vram_mb=4000), NOW) is b
    assert [w.name for w in rank([a, b], Job(), NOW)] == ["mrdubey", "legion"]


def test_skips_card_without_enough_vram() -> None:
    small, big = gpu("small", free=3000, total=8000), gpu("big", free=7000)
    assert pick([small, big], Job(min_vram_mb=6000), NOW) is big
    assert pick([small], Job(min_vram_mb=6000), NOW) is None


def test_skips_card_missing_required_capability() -> None:
    no_nvenc = gpu("cuda-only", free=11000, caps="cuda")
    ok = gpu("nvenc", free=4000)
    assert pick([no_nvenc, ok], Job(required_caps="nvenc"), NOW) is ok
    assert pick([no_nvenc], Job(required_caps="nvenc"), NOW) is None


def test_never_picks_a_yielding_host_even_if_best() -> None:
    yielding = gpu("mrdubey", free=11000, yielding=True)
    other = gpu("legion", free=5000, total=8000)
    assert pick([yielding, other], Job(), NOW) is other
    assert pick([yielding], Job(), NOW) is None


def test_skips_busy_hot_and_stale_workers() -> None:
    busy = gpu("busy", free=11000, busy=True)
    loaded = gpu("loaded", free=11000, util=95)
    stale = gpu("stale", free=11000, age=61)
    assert pick([busy, loaded, stale], Job(), NOW) is None
    fresh = gpu("fresh", free=1000, age=59)
    assert pick([busy, loaded, stale, fresh], Job(), NOW) is fresh


def test_cpu_fallback_when_no_gpu_worker_is_live() -> None:
    fallback = cpu()
    assert pick([fallback], Job(required_caps="nvenc", min_vram_mb=6000), NOW) is fallback
    assert pick([fallback, gpu("gone", age=120)], Job(), NOW) is fallback
    assert pick([cpu(busy=True)], Job(), NOW) is None


def test_busy_or_yielding_gpu_makes_job_wait_instead_of_cpu() -> None:
    fallback = cpu()
    assert pick([fallback, gpu("g", busy=True)], Job(), NOW) is None
    assert pick([fallback, gpu("g", yielding=True)], Job(), NOW) is None
    # A GPU that could never run it (too little total VRAM) does not block the fallback.
    assert pick([fallback, gpu("tiny", total=4000, free=4000)], Job(min_vram_mb=6000), NOW) is (
        fallback
    )


def test_free_gpu_preferred_over_cpu() -> None:
    card = gpu("card")
    assert pick([cpu("a-cpu"), card], Job(required_caps=""), NOW) is card
