# SPDX-License-Identifier: AGPL-3.0-or-later
"""/api/projects/{id}/script: versioned script storage, parse and lock (spike § Route/API map)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, col, select

from reelwright.db.models import Project, ScriptPut, ScriptVersion, ScriptVersionRead
from reelwright.db.session import get_session
from reelwright.script import ParseResult, Script, ScriptError, parse

router = APIRouter(prefix="/api/projects/{project_id}/script", tags=["script"])
SessionDep = Annotated[Session, Depends(get_session)]


class ScriptDoc(BaseModel):
    version: ScriptVersionRead
    locked: bool
    script: Script
    errors: list[ScriptError]


def _project(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _latest(session: Session, project_id: int) -> ScriptVersion | None:
    query = (
        select(ScriptVersion)
        .where(ScriptVersion.project_id == project_id)
        .order_by(col(ScriptVersion.version).desc())
        .limit(1)
    )
    return session.exec(query).first()


def _doc(row: ScriptVersion, result: ParseResult | None = None) -> ScriptDoc:
    result = result or parse(row.source)
    return ScriptDoc(
        version=ScriptVersionRead.model_validate(row),
        locked=row.locked_at is not None,
        script=result.script,
        errors=result.errors,
    )


def _errors(errors: list[ScriptError]) -> list[dict[str, Any]]:
    return [e.model_dump() for e in errors]


@router.get("", response_model=ScriptDoc)
def get_script(project_id: int, session: SessionDep) -> ScriptDoc:
    _project(session, project_id)
    row = _latest(session, project_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no script yet")
    return _doc(row)


@router.get("/versions", response_model=list[ScriptVersionRead])
def list_versions(project_id: int, session: SessionDep) -> list[ScriptVersion]:
    _project(session, project_id)
    query = (
        select(ScriptVersion)
        .where(ScriptVersion.project_id == project_id)
        .order_by(col(ScriptVersion.version).desc())
    )
    return list(session.exec(query).all())


@router.put("", response_model=ScriptDoc, status_code=status.HTTP_201_CREATED)
def put_script(project_id: int, body: ScriptPut, session: SessionDep) -> ScriptDoc:
    """Store a new version. Parse errors reject it (422); a locked script refuses edits (409)."""
    _project(session, project_id)
    latest = _latest(session, project_id)
    if latest is not None and latest.locked_at is not None:
        raise HTTPException(
            status_code=409, detail={"message": f"script v{latest.version} is locked"}
        )
    result = parse(body.source)
    if result.errors:
        raise HTTPException(
            status_code=422,
            detail={"message": "script has parse errors", "errors": _errors(result.errors)},
        )
    row = ScriptVersion(
        project_id=project_id,
        version=(latest.version + 1) if latest else 1,
        source=body.source,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _doc(row, result)


@router.post("/parse", response_model=ParseResult)
def parse_script(project_id: int, body: ScriptPut, session: SessionDep) -> ParseResult:
    """Parse without storing: drives the editor's live highlighting and inline errors."""
    _project(session, project_id)
    return parse(body.source)


@router.post("/lock", response_model=ScriptDoc)
def lock_script(project_id: int, session: SessionDep) -> ScriptDoc:
    """Freeze the latest version. Refused while any [TODO …] placeholder remains."""
    _project(session, project_id)
    row = _latest(session, project_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no script yet")
    if row.locked_at is not None:
        raise HTTPException(status_code=409, detail={"message": f"v{row.version} already locked"})
    result = parse(row.source)
    if result.errors or result.script.todos:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "resolve every [TODO …] and parse error before locking",
                "todos": [{"line": t.line, "text": t.text} for t in result.script.todos],
                "errors": _errors(result.errors),
            },
        )
    row.locked_at = datetime.now(UTC)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _doc(row, result)
