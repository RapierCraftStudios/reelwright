# SPDX-License-Identifier: AGPL-3.0-or-later
"""/api/projects/{id}/script: versioned storage, parse, lock, generate (spike § Route/API map)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from spectracut.providers.base import ProviderError, TextProvider
from sqlmodel import Session, col, select

from reelwright.db.models import Project, ScriptPut, ScriptVersion, ScriptVersionRead
from reelwright.db.session import get_engine, get_session
from reelwright.providers.registry import Service, get_provider
from reelwright.script import ParseResult, Script, ScriptError, parse
from reelwright.script.writer import DraftScript, WriterError, write_steps

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


class GenerateRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=500)
    notes: str = Field(default="", max_length=20_000)


def get_text_provider() -> TextProvider:
    """The LLM through SpectraCut's providers layer: the mock until a real adapter registers."""
    provider: TextProvider = get_provider(Service.llm)
    return provider


TextProviderDep = Annotated[TextProvider, Depends(get_text_provider)]


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _store_draft(project_id: int, draft: DraftScript) -> ScriptDoc:
    """Store the draft as the next version in its own session; the request's may be closed."""
    with Session(get_engine()) as session:
        latest = _latest(session, project_id)
        if latest is not None and latest.locked_at is not None:
            raise WriterError(f"script v{latest.version} was locked during generation")
        row = ScriptVersion(
            project_id=project_id,
            version=(latest.version + 1) if latest else 1,
            source=draft.source,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _doc(row, draft.result)


@router.post("/generate")
def generate_script(
    project_id: int, body: GenerateRequest, session: SessionDep, provider: TextProviderDep
) -> StreamingResponse:
    """Write a draft with the LLM writer, streaming progress as server-sent events.

    Events: ``progress`` ``{"stage": ...}`` per writer stage, then ``done`` with the stored
    version, or ``error`` ``{"message": ...}`` (nothing stored). A locked script refuses (409).
    """
    project = _project(session, project_id)
    latest = _latest(session, project_id)
    if latest is not None and latest.locked_at is not None:
        raise HTTPException(
            status_code=409, detail={"message": f"script v{latest.version} is locked"}
        )
    fmt = project.format

    def events() -> Iterator[str]:
        steps = write_steps(provider, topic=body.topic, notes=body.notes, fmt=fmt)
        try:
            while True:
                try:
                    stage = next(steps)
                except StopIteration as finished:
                    draft: DraftScript = finished.value
                    break
                yield _sse("progress", {"stage": stage})
            doc = _store_draft(project_id, draft)
        except (WriterError, ProviderError) as exc:
            yield _sse("error", {"message": str(exc)})
            return
        yield _sse("done", doc.model_dump(mode="json"))

    return StreamingResponse(events(), media_type="text/event-stream")
