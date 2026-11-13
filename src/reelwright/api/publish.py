# SPDX-License-Identifier: AGPL-3.0-or-later
"""/api/publish: the approval queue (spike § Route/API map, § Guardrails).

A draft holds the exact payload that will be sent. Approving it records who approved, when and
the payload hash the client displayed, then sends it through the YouTube adapter; a hash that no
longer matches is a 409 and records nothing. Until the OAuth client exists (#16) the provider is
the registry's mock.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlmodel import Session, col, select

from reelwright.api.edit import _script_version
from reelwright.db.models import (
    ApprovalCreate,
    ApprovalRead,
    Project,
    Publication,
    PublicationCreate,
    PublicationFields,
    PublicationRead,
    PublicationUpdate,
    Render,
)
from reelwright.db.session import get_session
from reelwright.providers.registry import Service, get_provider
from reelwright.publish import queue, youtube
from reelwright.render import registry
from reelwright.script import parse

router = APIRouter(prefix="/api/publish", tags=["publish"])
drafts = APIRouter(prefix="/api/projects/{project_id}/publish", tags=["publish"])
SessionDep = Annotated[Session, Depends(get_session)]


def get_youtube() -> Any:
    """The YouTube provider: the mock without credentials (tests override it with a shared one)."""
    return get_provider(Service.youtube)


YouTubeDep = Annotated[Any, Depends(get_youtube)]


def beat_titles(session: Session, project_id: int) -> dict[str, str]:
    """Chapter titles per beat slug from the locked script: the beat intent, else its type."""
    version = _script_version(session, project_id)
    if version is None:
        return {}
    result = parse(version.source)
    return {b.slug: (b.intent or b.type).strip() for b in result.script.beats if b.intent or b.type}


def _publication(session: Session, publication_id: int) -> Publication:
    publication = session.get(Publication, publication_id)
    if publication is None:
        raise HTTPException(status_code=404, detail="publication not found")
    return publication


def to_read(session: Session, publication: Publication) -> PublicationRead:
    render = session.get(Render, publication.render_id)
    has_preview = render is not None and bool(render.preview_path)
    return PublicationRead(
        id=publication.id or 0,
        project_id=publication.project_id,
        render_id=publication.render_id,
        platform=publication.platform,
        status=publication.status,
        payload=queue.load_payload(publication),
        payload_hash=publication.payload_hash,
        remote_id=publication.remote_id,
        publish_at=publication.publish_at,
        error=publication.error,
        created_at=publication.created_at,
        updated_at=publication.updated_at,
        approvals=[
            ApprovalRead.model_validate(a, from_attributes=True)
            for a in queue.approvals(session, publication)
        ],
        diff=queue.diff(session, publication),
        preview_url=f"/api/publish/{publication.id}/preview" if has_preview else None,
    )


def _build(session: Session, render: Render, fields: PublicationFields) -> dict[str, Any]:
    return youtube.build_payload(session, render, fields, beat_titles(session, render.project_id))


@drafts.post("", response_model=PublicationRead, status_code=status.HTTP_201_CREATED)
def create_publication(
    project_id: int, body: PublicationCreate, session: SessionDep
) -> PublicationRead:
    """A draft upload of one of the project's renders, waiting for approval."""
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    render = session.get(Render, body.render_id)
    if render is None or render.project_id != project_id:
        raise HTTPException(status_code=404, detail="render not found")
    fields = PublicationFields.model_validate(body.model_dump(exclude={"render_id"}))
    payload = _build(session, render, fields)
    publication = queue.create_draft(
        session, project_id, render.id or 0, payload, registry.utcnow()
    )
    return to_read(session, publication)


@router.get("", response_model=list[PublicationRead])
def list_publications(session: SessionDep, status: str | None = None) -> list[PublicationRead]:
    queue.refresh_live(session, registry.utcnow())
    query = select(Publication).order_by(col(Publication.id))
    if status:
        query = query.where(Publication.status == status)
    return [to_read(session, p) for p in session.exec(query).all()]


@router.get("/{publication_id}", response_model=PublicationRead)
def get_publication(publication_id: int, session: SessionDep) -> PublicationRead:
    queue.refresh_live(session, registry.utcnow())
    return to_read(session, _publication(session, publication_id))


@router.put("/{publication_id}", response_model=PublicationRead)
def update_publication(
    publication_id: int, body: PublicationUpdate, session: SessionDep
) -> PublicationRead:
    """Edit a draft (or a failed upload, which goes back to draft); the payload is re-hashed."""
    publication = _publication(session, publication_id)
    render = session.get(Render, publication.render_id)
    if render is None:
        raise HTTPException(status_code=404, detail="render not found")
    current = youtube.payload_fields(queue.load_payload(publication)).model_dump()
    current.update(body.model_dump(exclude_unset=True))
    fields = PublicationFields.model_validate(current)
    try:
        publication = queue.update_draft(
            session, publication, _build(session, render, fields), registry.utcnow()
        )
    except queue.QueueConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from None
    return to_read(session, publication)


@router.post("/{publication_id}/approve", response_model=PublicationRead)
def approve_publication(
    publication_id: int, body: ApprovalCreate, session: SessionDep, provider: YouTubeDep
) -> PublicationRead:
    """Record the approval of the displayed payload hash, then upload (or schedule) it."""
    publication = _publication(session, publication_id)
    try:
        publication = queue.approve(
            session, publication, body.approved_by, body.payload_hash, registry.utcnow(), provider
        )
    except queue.QueueConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from None
    except queue.PublishFailed as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)) from None
    return to_read(session, publication)


@router.post("/{publication_id}/retry", response_model=PublicationRead)
def retry_publication(
    publication_id: int, session: SessionDep, provider: YouTubeDep
) -> PublicationRead:
    """Send a failed upload again under its existing approval."""
    publication = _publication(session, publication_id)
    try:
        publication = queue.retry(session, publication, provider, registry.utcnow())
    except queue.QueueConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from None
    except queue.PublishFailed as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)) from None
    return to_read(session, publication)


@router.get("/{publication_id}/preview", include_in_schema=False)
def preview(publication_id: int, session: SessionDep) -> FileResponse:
    """The render's preview proxy (never raw footage)."""
    publication = _publication(session, publication_id)
    render = session.get(Render, publication.render_id)
    if render is None or not render.preview_path or not Path(render.preview_path).is_file():
        raise HTTPException(status_code=404, detail="no preview yet")
    return FileResponse(render.preview_path, media_type="video/mp4")
