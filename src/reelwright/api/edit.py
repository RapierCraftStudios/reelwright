# SPDX-License-Identifier: AGPL-3.0-or-later
"""/api/projects/{id}/edit: build the best-take rough cut, return its OTIO (spike § Route/API map).

The cut is built from the script and the word-timed transcripts only (JSON already on the dev
server); footage is referenced by its path on the capture PC and never read here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from spectracut.edit import FrameRate
from sqlmodel import Session, col, select

from reelwright.db.models import Asset, Project, ScriptVersion, Transcript, TranscriptWord
from reelwright.db.session import get_session
from reelwright.edit import (
    CutLine,
    RetakeMark,
    align_script,
    build_edit,
    cut_take,
    edit_to_otio_json,
    pick_best_takes,
    score_attempts,
    script_lines,
)
from reelwright.ingest.transcript import transcript_to_read
from reelwright.script import parse

router = APIRouter(prefix="/api/projects/{project_id}/edit", tags=["edit"])
SessionDep = Annotated[Session, Depends(get_session)]


class EditRequest(BaseModel):
    asset_ids: list[int] = Field(min_length=1, max_length=200)
    retake_marks: list[RetakeMark] = Field(default_factory=list, max_length=10_000)
    fps: int = Field(default=30, ge=1, le=120)


class TakeReport(BaseModel):
    beat: str
    line_no: int
    asset_id: int
    start: float
    end: float
    fluency: float
    fillers: int
    restarts: int
    energy: float
    confidence: float
    retake: bool
    total: float


class EditResponse(BaseModel):
    otio: str
    duration_s: float
    takes: list[TakeReport]
    missing_lines: list[int]


def _script_version(session: Session, project_id: int) -> ScriptVersion | None:
    """The latest locked script version, else the latest version."""
    base = select(ScriptVersion).where(ScriptVersion.project_id == project_id)
    locked = session.exec(
        base.where(col(ScriptVersion.locked_at).is_not(None))
        .order_by(col(ScriptVersion.version).desc())
        .limit(1)
    ).first()
    if locked is not None:
        return locked
    return session.exec(base.order_by(col(ScriptVersion.version).desc()).limit(1)).first()


def _transcripts(
    session: Session, asset_ids: list[int]
) -> tuple[dict[int, list[TranscriptWord]], dict[int, str]]:
    words: dict[int, list[TranscriptWord]] = {}
    media: dict[int, str] = {}
    for asset_id in dict.fromkeys(asset_ids):
        asset = session.get(Asset, asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail=f"asset {asset_id} not found")
        row = session.get(Transcript, asset.transcript_id) if asset.transcript_id else None
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"asset {asset_id} has no transcript yet",
            )
        words[asset_id] = transcript_to_read(row).words
        media[asset_id] = asset.path
    return words, media


@router.post("", response_model=EditResponse)
def build_rough_cut(project_id: int, body: EditRequest, session: SessionDep) -> EditResponse:
    """Align the transcripts to the script, pick the best take per line, cut, return OTIO."""
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    version = _script_version(session, project_id)
    if version is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no script yet")
    result = parse(version.source)
    if result.errors:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"script version {version.version} has parse errors",
        )
    transcripts, media = _transcripts(session, body.asset_ids)
    lines = script_lines(result.script)
    takes = score_attempts(align_script(result.script, transcripts), body.retake_marks)
    chosen, missing = pick_best_takes(lines, takes)
    cut = [
        CutLine(
            beat_slug=t.line.beat_slug,
            line_no=t.line.line_no,
            text=t.line.text,
            spans=cut_take(t.attempt.asset_id, t.attempt.words, t.line.tokens),
        )
        for t in chosen
    ]
    edit = build_edit(project.slug, cut, media, FrameRate.of(body.fps))
    return EditResponse(
        otio=edit_to_otio_json(edit),
        duration_s=float(edit.seconds),
        takes=[
            TakeReport(
                beat=t.line.beat_slug,
                line_no=t.line.line_no,
                asset_id=t.attempt.asset_id,
                start=t.attempt.start,
                end=t.attempt.end,
                fluency=t.scores.fluency,
                fillers=t.scores.fillers,
                restarts=t.scores.restarts,
                energy=t.scores.energy,
                confidence=t.scores.confidence,
                retake=t.scores.retake,
                total=t.scores.total,
            )
            for t in chosen
        ],
        missing_lines=[line.line_no for line in missing],
    )
