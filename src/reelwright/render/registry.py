# SPDX-License-Identifier: AGPL-3.0-or-later
"""The worker registry: heartbeats in, live workers out."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, col, select

from reelwright.db.models import RenderJob, Worker, WorkerHeartbeat, WorkerRead
from reelwright.render.picker import LIVENESS_S, as_utc, caps_of, is_live

__all__ = ["LIVENESS_S", "live_workers", "record_heartbeat", "worker_to_read", "utcnow"]


def utcnow() -> datetime:
    return datetime.now(UTC)


def _holds_claim(session: Session, name: str) -> bool:
    query = select(RenderJob.id).where(RenderJob.worker == name, RenderJob.status == "claimed")
    return session.exec(query).first() is not None


def record_heartbeat(session: Session, hb: WorkerHeartbeat, now: datetime) -> Worker:
    """Upsert the worker's snapshot. A worker holding a claimed job stays busy."""
    worker = session.exec(select(Worker).where(Worker.name == hb.name)).first()
    if worker is None:
        worker = Worker(name=hb.name)
    worker.host = hb.host
    worker.gpu_model = hb.gpu_model
    worker.vram_total_mb = hb.vram_total_mb
    worker.vram_free_mb = hb.vram_free_mb
    worker.utilization_pct = hb.utilization_pct
    worker.temperature_c = hb.temperature_c
    worker.capabilities = ",".join(sorted({c.strip() for c in hb.capabilities if c.strip()}))
    worker.yielding = hb.yielding
    worker.busy = hb.busy or _holds_claim(session, hb.name)
    worker.last_heartbeat = as_utc(now)
    session.add(worker)
    session.commit()
    session.refresh(worker)
    return worker


def live_workers(session: Session, now: datetime) -> list[Worker]:
    rows = session.exec(select(Worker).order_by(col(Worker.name))).all()
    return [w for w in rows if is_live(w, now)]


def worker_to_read(worker: Worker) -> WorkerRead:
    return WorkerRead(
        name=worker.name,
        host=worker.host,
        gpu_model=worker.gpu_model,
        vram_total_mb=worker.vram_total_mb,
        vram_free_mb=worker.vram_free_mb,
        utilization_pct=worker.utilization_pct,
        temperature_c=worker.temperature_c,
        capabilities=sorted(caps_of(worker.capabilities)),
        yielding=worker.yielding,
        busy=worker.busy,
        last_heartbeat=as_utc(worker.last_heartbeat),
    )
