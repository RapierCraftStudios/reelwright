# SPDX-License-Identifier: AGPL-3.0-or-later
"""/api/projects/{id}/renders: final renders of an Edit (spike § Route/API map, § Render).

POST takes the Edit's OTIO and an absolute .mp4 path on the capture PC. The render is split
across the free GPU cards that the picker reports, then concatenated beside that path. The preview
proxy that a worker uploads to PUT .../preview goes through the proxy disk budget and free-disk
floor (507 when a write would cross either). A worker runs media QA next to the final file and
PUTs only its JSON report to .../qa. No raw footage or final render is stored on the dev server.
"""

from __future__ import annotations

from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session, col, select

from reelwright.api.assets import _sync_chunks
from reelwright.api.workers import WorkerAuth
from reelwright.db.models import Project, QAReport, Render, RenderCreate, RenderRead
from reelwright.db.session import get_session
from reelwright.ingest import proxy
from reelwright.render import registry, split
from reelwright.render.graph import UnsupportedEdit

router = APIRouter(prefix="/api/projects/{project_id}/renders", tags=["render"])
SessionDep = Annotated[Session, Depends(get_session)]


def _project(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _render(session: Session, project_id: int, render_id: int) -> Render:
    render = session.get(Render, render_id)
    if render is None or render.project_id != project_id:
        raise HTTPException(status_code=404, detail="render not found")
    return render


def preview_name(render_id: int) -> str:
    return f"render-{render_id}.preview.mp4"


@router.post("", response_model=RenderRead, status_code=status.HTTP_201_CREATED)
def post_render(project_id: int, body: RenderCreate, session: SessionDep) -> RenderRead:
    """Queue a final render: one segment per free GPU card (at most two), then a concat."""
    project = _project(session, project_id)
    try:
        render = split.start_render(
            session, project, body.otio, body.output_path, registry.utcnow()
        )
    except (UnsupportedEdit, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        ) from None
    return split.render_to_read(render)


@router.get("", response_model=list[RenderRead])
def list_renders(project_id: int, session: SessionDep) -> list[RenderRead]:
    _project(session, project_id)
    rows = session.exec(
        select(Render).where(Render.project_id == project_id).order_by(col(Render.id))
    ).all()
    return [split.render_to_read(r) for r in rows]


@router.get("/{render_id}", response_model=RenderRead)
def get_render(project_id: int, render_id: int, session: SessionDep) -> RenderRead:
    return split.render_to_read(_render(session, project_id, render_id))


@router.put("/{render_id}/preview", response_model=RenderRead, dependencies=WorkerAuth)
async def put_preview(
    project_id: int, render_id: int, request: Request, session: SessionDep
) -> RenderRead:
    """A worker uploads the preview proxy of the final render, under the proxy disk budget."""
    render = _render(session, project_id, render_id)
    budget = proxy.ProxyBudget.from_env()
    chunks = _sync_chunks(request.stream().__aiter__())
    try:
        dest = await anyio.to_thread.run_sync(
            proxy.guarded_write, chunks, preview_name(render_id), budget
        )
    except proxy.DiskBudgetExceeded as e:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE, detail=str(e)
        ) from None
    render.preview_path = str(dest)
    render.status = split.RenderStatus.done
    session.add(render)
    session.commit()
    session.refresh(render)
    return split.render_to_read(render)


@router.put("/{render_id}/qa", response_model=RenderRead, dependencies=WorkerAuth)
def put_qa(project_id: int, render_id: int, body: QAReport, session: SessionDep) -> RenderRead:
    """A worker uploads the media QA report of the final render; its verdict gates publishing."""
    render = _render(session, project_id, render_id)
    render.qa_report = body.model_dump_json()
    render.qa_status = "passed" if body.passed else "failed"
    session.add(render)
    session.commit()
    session.refresh(render)
    return split.render_to_read(render)


@router.get("/{render_id}/qa", response_model=QAReport)
def get_qa(project_id: int, render_id: int, session: SessionDep) -> QAReport:
    render = _render(session, project_id, render_id)
    if render.qa_report is None:
        raise HTTPException(status_code=404, detail="qa report not ready")
    return QAReport.model_validate_json(render.qa_report)
