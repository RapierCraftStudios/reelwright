# SPDX-License-Identifier: AGPL-3.0-or-later
"""/api/prompter/{id}: teleprompter take marks (spike § Route/API map)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, col, select

from reelwright.db.models import Project, ScriptVersion, TakeMark, TakeMarkCreate, TakeMarkRead
from reelwright.db.session import get_session

router = APIRouter(prefix="/api/prompter/{project_id}", tags=["prompter"])
SessionDep = Annotated[Session, Depends(get_session)]


def _project(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _read(mark: TakeMark, version: int) -> TakeMarkRead:
    return TakeMarkRead.model_validate({**mark.model_dump(), "script_version": version})


@router.post("/marks", response_model=TakeMarkRead, status_code=status.HTTP_201_CREATED)
def create_mark(project_id: int, body: TakeMarkCreate, session: SessionDep) -> TakeMarkRead:
    """Record a take mark against the locked script version the reader is on."""
    _project(session, project_id)
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
    mark = TakeMark(
        project_id=project_id,
        script_version_id=row.id,
        kind=body.kind.value,
        beat_slug=body.beat_slug,
        line_no=body.line_no,
        marked_at=body.marked_at,
        offset_ms=body.offset_ms,
    )
    session.add(mark)
    session.commit()
    session.refresh(mark)
    return _read(mark, row.version)


@router.get("/marks", response_model=list[TakeMarkRead])
def list_marks(project_id: int, session: SessionDep) -> list[TakeMarkRead]:
    _project(session, project_id)
    rows = session.exec(
        select(TakeMark, ScriptVersion.version)
        .join(ScriptVersion, col(TakeMark.script_version_id) == col(ScriptVersion.id))
        .where(TakeMark.project_id == project_id)
        .order_by(col(TakeMark.marked_at), col(TakeMark.id))
    ).all()
    return [_read(mark, version) for mark, version in rows]
