# SPDX-License-Identifier: AGPL-3.0-or-later
"""The render job queue: enqueue, claim for the picked worker, complete, requeue dead claims.

Workers pull: a worker asks for its next job and gets the oldest queued job that the picker would
send to it. A job claimed by a worker that stops heart-beating goes back to the queue after 60 s.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlmodel import Session, col, select

from reelwright.db.models import RenderJob, RenderJobRead, Worker
from reelwright.render.picker import as_utc, caps_of, pick

REQUEUE_AFTER_S = 60


class JobKind(StrEnum):
    transcode = "transcode"
    proxy = "proxy"
    render = "render"
    transcribe = "transcribe"
    qa = "qa"


class JobStatus(StrEnum):
    queued = "queued"
    claimed = "claimed"
    done = "done"
    failed = "failed"


# Caps a kind needs on a GPU worker. A CPU worker runs every kind without them (slower).
DEFAULT_CAPS: dict[JobKind, tuple[str, ...]] = {
    JobKind.transcode: ("nvenc",),
    JobKind.proxy: ("nvenc",),
    JobKind.render: ("nvenc",),
    JobKind.transcribe: ("cuda",),
    JobKind.qa: (),  # one ffmpeg analysis pass on the CPU: any live worker
}


class JobNotFound(LookupError):
    pass


class NotClaimant(PermissionError):
    pass


def enqueue(
    session: Session,
    kind: JobKind | str,
    payload: Mapping[str, Any] | None = None,
    min_vram_mb: int = 0,
    required_caps: Iterable[str] | None = None,
) -> RenderJob:
    kind = JobKind(kind)
    caps = DEFAULT_CAPS[kind] if required_caps is None else tuple(required_caps)
    job = RenderJob(
        kind=kind.value,
        min_vram_mb=min_vram_mb,
        required_caps=",".join(sorted(set(caps))),
        payload=json.dumps(dict(payload or {})),
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def requeue_stale(session: Session, now: datetime, timeout_s: int = REQUEUE_AFTER_S) -> int:
    """Put jobs whose claimant has not heart-beaten for timeout_s back in the queue."""
    beats = {w.name: as_utc(w.last_heartbeat) for w in session.exec(select(Worker)).all()}
    claimed = session.exec(select(RenderJob).where(RenderJob.status == JobStatus.claimed)).all()
    count = 0
    for job in claimed:
        last = beats.get(job.worker or "")
        if last is not None and (as_utc(now) - last).total_seconds() <= timeout_s:
            continue
        job.status = JobStatus.queued
        job.worker = None
        job.claimed_at = None
        job.attempts += 1
        session.add(job)
        count += 1
    if count:
        session.commit()
    return count


def next_job_for(session: Session, worker_name: str, now: datetime) -> RenderJob | None:
    """Claim the oldest queued job the picker sends to this worker, if any."""
    requeue_stale(session, now)
    me = session.exec(select(Worker).where(Worker.name == worker_name)).first()
    if me is None:
        return None
    workers = list(session.exec(select(Worker)).all())
    queued = session.exec(
        select(RenderJob)
        .where(RenderJob.status == JobStatus.queued)
        .order_by(col(RenderJob.created_at), col(RenderJob.id))
    ).all()
    for job in queued:
        chosen = pick(workers, job, now)
        if chosen is None or chosen.name != worker_name:
            continue
        job.status = JobStatus.claimed
        job.worker = worker_name
        job.claimed_at = as_utc(now)
        me.busy = True
        session.add(job)
        session.add(me)
        session.commit()
        session.refresh(job)
        return job
    return None


def complete(
    session: Session, job_id: int, worker_name: str, ok: bool, outputs: Mapping[str, Any]
) -> RenderJob:
    job = session.get(RenderJob, job_id)
    if job is None:
        raise JobNotFound(job_id)
    if job.status != JobStatus.claimed or job.worker != worker_name:
        raise NotClaimant(f"job {job_id} is not claimed by {worker_name}")
    job.status = JobStatus.done if ok else JobStatus.failed
    job.outputs = json.dumps(dict(outputs))
    session.add(job)
    me = session.exec(select(Worker).where(Worker.name == worker_name)).first()
    if me is not None:
        me.busy = False
        session.add(me)
    session.commit()
    session.refresh(job)
    return job


def job_to_read(job: RenderJob) -> RenderJobRead:
    assert job.id is not None
    return RenderJobRead(
        id=job.id,
        kind=job.kind,
        status=job.status,
        min_vram_mb=job.min_vram_mb,
        required_caps=sorted(caps_of(job.required_caps)),
        payload=json.loads(job.payload),
        worker=job.worker,
        attempts=job.attempts,
        outputs=json.loads(job.outputs),
    )
