# SPDX-License-Identifier: AGPL-3.0-or-later
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session

from reelwright.db.models import RenderJob, WorkerHeartbeat
from reelwright.db.session import get_engine, migrate
from reelwright.render import queue
from reelwright.render.registry import record_heartbeat

T0 = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def beat(session: Session, name: str, at: datetime, free: int = 8000) -> None:
    hb = WorkerHeartbeat(
        name=name,
        gpu_model="RTX",
        vram_total_mb=12000,
        vram_free_mb=free,
        capabilities=["cuda", "nvenc"],
    )
    record_heartbeat(session, hb, at)


@pytest.fixture
def session(tmp_db: str) -> Session:
    migrate()
    return Session(get_engine())


def claimed_job(session: Session) -> RenderJob:
    beat(session, "gpu-a", T0)
    job = queue.enqueue(session, queue.JobKind.render, {"input": "a", "output": "b"})
    claimed = queue.next_job_for(session, "gpu-a", T0)
    assert claimed is not None and claimed.id == job.id and claimed.status == "claimed"
    return claimed


def test_job_of_silent_worker_is_requeued_after_60s(session: Session) -> None:
    job = claimed_job(session)
    assert queue.requeue_stale(session, T0 + timedelta(seconds=61)) == 1
    session.refresh(job)
    assert (job.status, job.worker, job.claimed_at, job.attempts) == ("queued", None, None, 1)


def test_job_stays_claimed_within_60s(session: Session) -> None:
    job = claimed_job(session)
    assert queue.requeue_stale(session, T0 + timedelta(seconds=59)) == 0
    session.refresh(job)
    assert (job.status, job.worker) == ("claimed", "gpu-a")


def test_live_claimant_keeps_its_job(session: Session) -> None:
    job = claimed_job(session)
    beat(session, "gpu-a", T0 + timedelta(seconds=50))
    assert queue.requeue_stale(session, T0 + timedelta(seconds=100)) == 0
    session.refresh(job)
    assert job.status == "claimed"


def test_requeued_job_goes_to_another_live_worker(session: Session) -> None:
    job = claimed_job(session)
    later = T0 + timedelta(seconds=90)
    beat(session, "gpu-b", later)
    again = queue.next_job_for(session, "gpu-b", later)
    assert again is not None and again.id == job.id and again.worker == "gpu-b"


def test_job_only_goes_to_the_picked_worker(session: Session) -> None:
    beat(session, "small", T0, free=2000)
    beat(session, "big", T0, free=10000)
    queue.enqueue(session, "render", {"input": "a", "output": "b"})
    assert queue.next_job_for(session, "small", T0) is None
    got = queue.next_job_for(session, "big", T0)
    assert got is not None and got.worker == "big"
    assert queue.next_job_for(session, "unknown", T0) is None


def test_complete_rejects_non_claimant(session: Session) -> None:
    job = claimed_job(session)
    assert job.id is not None
    with pytest.raises(queue.NotClaimant):
        queue.complete(session, job.id, "someone-else", True, {})
    with pytest.raises(queue.JobNotFound):
        queue.complete(session, 999, "gpu-a", True, {})
    done = queue.complete(session, job.id, "gpu-a", True, {"output": "b"})
    assert done.status == "done" and queue.job_to_read(done).outputs == {"output": "b"}
