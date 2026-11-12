# SPDX-License-Identifier: AGPL-3.0-or-later
"""/api/projects/{id}/takes: camera take markers posted by the prompter (issue #50).

Each marker records which beat and script lines a camera recording covers and when the camera
started it. The best-take rough cut (#11) joins camera files to markers by start time with
`match_take`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlmodel import Session, col, select

from reelwright.db.models import (
    CameraTake,
    CameraTakeCreate,
    CameraTakeRead,
    Project,
    ScriptVersion,
)
from reelwright.db.session import get_session

router = APIRouter(prefix="/api/projects/{project_id}/takes", tags=["takes"])
SessionDep = Annotated[Session, Depends(get_session)]

DEFAULT_TOLERANCE_S = 5.0


def _project(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _read(take: CameraTake, version: int) -> CameraTakeRead:
    return CameraTakeRead.model_validate({**take.model_dump(), "script_version": version})


def _version_number(session: Session, script_version_id: int) -> int:
    row = session.get(ScriptVersion, script_version_id)
    return row.version if row is not None else 0


@router.post("", response_model=CameraTakeRead, status_code=status.HTTP_201_CREATED)
def create_take(
    project_id: int, body: CameraTakeCreate, response: Response, session: SessionDep
) -> CameraTakeRead:
    """Store a take marker; a replayed client_id returns the stored row (offline retries)."""
    _project(session, project_id)
    existing = session.exec(
        select(CameraTake).where(CameraTake.client_id == body.client_id)
    ).first()
    if existing is not None:
        if existing.project_id != project_id:
            raise HTTPException(
                status_code=409, detail={"message": "client_id belongs to another project"}
            )
        response.status_code = status.HTTP_200_OK
        return _read(existing, _version_number(session, existing.script_version_id))
    row = session.exec(
        select(ScriptVersion).where(
            ScriptVersion.project_id == project_id, ScriptVersion.version == body.script_version
        )
    ).first()
    if row is None or row.id is None:
        raise HTTPException(
            status_code=422, detail={"message": f"script v{body.script_version} not found"}
        )
    if row.locked_at is None:
        raise HTTPException(
            status_code=422, detail={"message": f"script v{body.script_version} is not locked"}
        )
    take = CameraTake(
        project_id=project_id,
        script_version_id=row.id,
        client_id=body.client_id,
        beat_slug=body.beat_slug,
        line_from=body.line_from,
        line_to=body.line_to,
        retake=body.retake,
        continuous=body.continuous,
        camera=body.camera,
        started_at=body.started_at,
        stopped_at=body.stopped_at,
    )
    session.add(take)
    session.commit()
    session.refresh(take)
    return _read(take, row.version)


@router.get("", response_model=list[CameraTakeRead])
def list_takes(project_id: int, session: SessionDep) -> list[CameraTakeRead]:
    """The project's take markers in recording order (started_at, then id)."""
    _project(session, project_id)
    rows = session.exec(
        select(CameraTake, ScriptVersion.version)
        .join(ScriptVersion, col(CameraTake.script_version_id) == col(ScriptVersion.id))
        .where(CameraTake.project_id == project_id)
        .order_by(col(CameraTake.started_at), col(CameraTake.id))
    ).all()
    return [_read(take, version) for take, version in rows]


def match_take[T: (CameraTake, CameraTakeRead)](
    takes: Iterable[T], file_start: datetime, tolerance_s: float = DEFAULT_TOLERANCE_S
) -> T | None:
    """The take whose started_at is nearest a camera file's start, if within tolerance_s.

    For the rough cut (#11): a camera file's capture start (from its metadata) identifies the
    recording the prompter started, and so its beat and script line range. Continuous-mode takes
    share one file; pass the file start and then use the takes' started_at offsets inside it.
    """
    if file_start.tzinfo is None:
        raise ValueError("file_start needs a timezone")
    best: T | None = None
    best_gap = tolerance_s
    for take in takes:
        gap = abs((take.started_at - file_start).total_seconds())
        if gap <= best_gap:
            best, best_gap = take, gap
    return best
