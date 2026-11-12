# SPDX-License-Identifier: AGPL-3.0-or-later
"""/api/workers: heartbeat, job pull and completion for render workers (spike § Route/API map).

Heartbeat, next and complete need `Authorization: Bearer <REELWRIGHT_WORKER_TOKEN>` when that
env var is set. With it unset (dev, CI, mock provider) they are open; the API binds to loopback.
"""

from __future__ import annotations

import hmac
import os
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlmodel import Session

from reelwright.db.models import JobComplete, RenderJobRead, WorkerHeartbeat, WorkerRead
from reelwright.db.session import get_session
from reelwright.render import queue, registry

TOKEN_ENV = "REELWRIGHT_WORKER_TOKEN"

router = APIRouter(prefix="/api/workers", tags=["workers"])
SessionDep = Annotated[Session, Depends(get_session)]


def require_worker_token(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = os.environ.get(TOKEN_ENV)
    if not expected:
        return
    scheme, _, value = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(value.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="worker token required",
            headers={"WWW-Authenticate": "Bearer"},
        )


WorkerAuth = [Depends(require_worker_token)]


@router.get("", response_model=list[WorkerRead])
def get_workers(session: SessionDep) -> list[WorkerRead]:
    """Live workers (heartbeat within 60 s) with their GPU stats."""
    return [registry.to_read(w) for w in registry.live_workers(session, registry.utcnow())]


@router.post("/heartbeat", response_model=WorkerRead, dependencies=WorkerAuth)
def heartbeat(body: WorkerHeartbeat, session: SessionDep) -> WorkerRead:
    now = registry.utcnow()
    worker = registry.record_heartbeat(session, body, now)
    queue.requeue_stale(session, now)
    return registry.to_read(worker)


@router.get(
    "/next",
    response_model=RenderJobRead,
    responses={204: {"description": "No job for this worker right now"}},
    dependencies=WorkerAuth,
)
def next_job(worker: str, session: SessionDep) -> RenderJobRead | Response:
    job = queue.next_job_for(session, worker, registry.utcnow())
    if job is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return queue.to_read(job)


@router.post("/jobs/{job_id}/complete", response_model=RenderJobRead, dependencies=WorkerAuth)
def complete_job(job_id: int, body: JobComplete, session: SessionDep) -> RenderJobRead:
    try:
        job = queue.complete(session, job_id, body.worker, body.ok, body.outputs)
    except queue.JobNotFound:
        raise HTTPException(status_code=404, detail="job not found") from None
    except queue.NotClaimant as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    return queue.to_read(job)
