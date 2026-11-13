# SPDX-License-Identifier: AGPL-3.0-or-later
"""Final renders: split by segment across free GPUs, concatenate, report output and preview.

``start_render`` asks the picker how many GPU cards are free right now (never a host name). It
splits the timeline into that many segments, at most two, and queues one ``render`` job per
segment. ``advance`` runs on every job completion. When all segments are done it queues one concat
job. When the concat is done it records the output (on the capture PC, next to the segments) and
queues a preview proxy, which the worker uploads to ``/api/projects/{id}/renders/{rid}/preview``,
and then a media QA job, whose JSON report the worker uploads to ``.../renders/{rid}/qa``. The QA
verdict lives in ``Render.qa_status`` beside the render lifecycle, not in it.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from fractions import Fraction
from typing import Any

from spectracut.edit import Edit, Transition, from_otio
from sqlmodel import Session

from reelwright.db.models import Project, QAReport, Render, RenderJob, RenderRead
from reelwright.render import queue
from reelwright.render.graph import build_render
from reelwright.render.picker import rank
from reelwright.render.queue import JobKind, JobStatus
from reelwright.render.registry import live_workers

MAX_SEGMENTS = 2
MIN_SEGMENT_S = 60


class RenderStatus(StrEnum):
    queued = "queued"
    rendering = "rendering"
    concatenating = "concatenating"
    previewing = "previewing"
    done = "done"
    failed = "failed"


class Mode(StrEnum):
    segment = "segment"
    concat = "concat"


@dataclass
class _RenderJobShape:
    """What a segment job asks of the picker (queue.DEFAULT_CAPS for render)."""

    min_vram_mb: int = 0
    required_caps: str = ",".join(queue.DEFAULT_CAPS[JobKind.render])


def parse_otio(text: str) -> Edit:
    """An OTIO JSON document as an Edit; ValueError when it is not a readable timeline."""
    otio: Any = importlib.import_module("opentimelineio")  # untyped package

    try:
        timeline = otio.adapters.read_from_string(text, "otio_json")
    except Exception as e:  # OTIO raises its own exception types and plain JSON errors
        raise ValueError(f"not an OTIO timeline: {e}") from None
    if not isinstance(timeline, otio.schema.Timeline):
        raise ValueError("not an OTIO timeline")
    edit: Edit = from_otio(timeline)
    if edit.duration <= 0:
        raise ValueError("the timeline is empty")
    return edit


def clip_boundaries(edit: Edit) -> list[int]:
    """Timeline frames where one video item ends and the next begins."""
    out: list[int] = []
    for track in edit.tracks:
        if track.kind != "video":
            continue
        at = 0
        for item in track.items:
            if isinstance(item, Transition):
                continue
            at += item.duration
            if 0 < at < edit.duration:
                out.append(at)
    return sorted(set(out))


def plan_segments(edit: Edit, n: int, min_segment_frames: int) -> list[tuple[int, int]]:
    """Contiguous ``[start, end)`` ranges covering the edit, cut near even splits.

    A cut goes to the clip boundary nearest the even split when one keeps both sides at least
    ``min_segment_frames`` long. Otherwise it goes at the split frame itself; the graph trims the
    clip, so the cut stays frame-exact.
    """
    total = edit.duration
    if total <= 0:
        raise ValueError("the edit is empty")
    n = max(1, min(n, total // max(1, min_segment_frames)))
    boundaries = clip_boundaries(edit)
    cuts: list[int] = []
    for i in range(1, n):
        last = cuts[-1] if cuts else 0
        lo, hi = last + min_segment_frames, total - min_segment_frames * (n - i)
        if lo > hi:
            break
        target = round(total * i / n)
        near = [b for b in boundaries if lo <= b <= hi]
        cut = min(near, key=lambda b: (abs(b - target), b)) if near else min(max(target, lo), hi)
        cuts.append(cut)
    edges = [0, *cuts, total]
    return [(a, b) for a, b in zip(edges, edges[1:], strict=False) if b > a]


def segment_path(output_path: str, index: int) -> str:
    """Segment files sit next to the final output on the capture PC."""
    stem = output_path[: -len(".mp4")] if output_path.lower().endswith(".mp4") else output_path
    return f"{stem}.seg{index}.mov"


def free_gpus(session: Session, now: datetime) -> int:
    return len(rank(live_workers(session, now), _RenderJobShape(), now))


def start_render(
    session: Session,
    project: Project,
    otio_json: str,
    output_path: str,
    now: datetime,
    min_segment_s: float = MIN_SEGMENT_S,
) -> Render:
    """Validate the edit, plan segments for the free cards and queue the segment jobs."""
    assert project.id is not None
    edit = parse_otio(otio_json)
    # Raises UnsupportedEdit before anything is queued.
    build_render(edit, 0, edit.duration, "check.mkv", hw=False, filter_script_path="check.txt")
    n = min(MAX_SEGMENTS, max(1, free_gpus(session, now)))
    min_frames = max(1, edit.rate.frames(Fraction(min_segment_s).limit_denominator(1000)))
    ranges = plan_segments(edit, n, min_frames)
    render = Render(
        project_id=project.id,
        status=RenderStatus.queued,
        otio=otio_json,
        fps_num=edit.rate.numerator,
        fps_den=edit.rate.denominator,
        frames=edit.duration,
        segments=len(ranges),
        target_path=output_path,
    )
    session.add(render)
    session.commit()
    session.refresh(render)
    ids: list[int] = []
    for i, (start, end) in enumerate(ranges):
        payload = {
            "render_id": render.id,
            "mode": Mode.segment.value,
            "otio": otio_json,
            "start_frame": start,
            "end_frame": end,
            "output": segment_path(output_path, i),
        }
        job = queue.enqueue(session, JobKind.render, payload)
        assert job.id is not None
        ids.append(job.id)
    render.job_ids = json.dumps(ids)
    session.add(render)
    session.commit()
    session.refresh(render)
    return render


def _fail(session: Session, render: Render, job: RenderJob, outputs: dict[str, Any]) -> None:
    detail = outputs.get("error") or f"returncode {outputs.get('returncode')}"
    render.status = RenderStatus.failed
    render.error = f"{job.kind} job {job.id} failed: {detail}"[:2000]
    session.add(render)
    session.commit()


def advance(session: Session, job: RenderJob) -> Render | None:
    """Move the job's render on after the job completed; None for jobs that are not a render's."""
    payload: dict[str, Any] = json.loads(job.payload)
    render_id = payload.get("render_id")
    if render_id is None:
        return None
    render = session.get(Render, int(render_id))
    if render is None:
        return None
    outputs: dict[str, Any] = json.loads(job.outputs)
    if job.kind == JobKind.qa:
        _qa_done(session, render, job)
        return render
    if render.status in (RenderStatus.done, RenderStatus.failed):
        return render
    if job.status == JobStatus.failed:
        _fail(session, render, job, outputs)
        return render
    mode = payload.get("mode")
    if job.kind == JobKind.render and mode == Mode.segment:
        _segment_done(session, render)
    elif job.kind == JobKind.render and mode == Mode.concat:
        _concat_done(session, render, str(payload["output"]))
    elif job.kind == JobKind.proxy and render.preview_path:
        render.status = RenderStatus.done
        session.add(render)
        session.commit()
    session.refresh(render)
    return render


def _segment_done(session: Session, render: Render) -> None:
    ids: list[int] = json.loads(render.job_ids)
    jobs = [session.get(RenderJob, i) for i in ids]
    if render.concat_job_id is not None:
        return
    if not all(j is not None and j.status == JobStatus.done for j in jobs):
        render.status = RenderStatus.rendering
        session.add(render)
        session.commit()
        return
    segments = [str(json.loads(j.payload)["output"]) for j in jobs if j is not None]
    payload = {
        "render_id": render.id,
        "mode": Mode.concat.value,
        "segments": segments,
        "output": render.target_path,
        "master": True,  # voice cleanup and loudness normalisation over the joined audio
    }
    # Stream copy plus one AAC encode: any live worker can do it, no GPU needed.
    job = queue.enqueue(session, JobKind.render, payload, required_caps=())
    render.concat_job_id = job.id
    render.status = RenderStatus.concatenating
    session.add(render)
    session.commit()


def preview_upload_path(render: Render) -> str:
    return f"/api/projects/{render.project_id}/renders/{render.id}/preview"


def qa_upload_path(render: Render) -> str:
    return f"/api/projects/{render.project_id}/renders/{render.id}/qa"


QA_PENDING = "pending"
QA_ERROR = "error"


def _qa_done(session: Session, render: Render, job: RenderJob) -> None:
    """A finished QA job: the verdict arrives with the report PUT; a failed job is an error."""
    if job.status != JobStatus.failed or render.qa_status not in (None, QA_PENDING):
        return
    render.qa_status = QA_ERROR
    session.add(render)
    session.commit()
    session.refresh(render)


def _concat_done(session: Session, render: Render, output: str) -> None:
    render.output_path = output
    payload = {
        "render_id": render.id,
        "input": output,
        "output": f"rw-render-preview-{render.id}.mp4",
        "upload": preview_upload_path(render),
    }
    job = queue.enqueue(session, JobKind.proxy, payload)
    render.preview_job_id = job.id
    qa_payload = {
        "render_id": render.id,
        "input": output,
        "output": f"rw-render-qa-{render.id}.json",
        "upload": qa_upload_path(render),
        "content_type": "application/json",
    }
    qa_job = queue.enqueue(session, JobKind.qa, qa_payload, required_caps=())
    render.qa_job_id = qa_job.id
    render.qa_status = QA_PENDING
    render.status = RenderStatus.previewing
    session.add(render)
    session.commit()


def render_to_read(render: Render) -> RenderRead:
    assert render.id is not None
    return RenderRead(
        id=render.id,
        project_id=render.project_id,
        status=render.status,
        frames=render.frames,
        fps=render.fps_num / render.fps_den,
        segments=render.segments,
        target_path=render.target_path,
        output_path=render.output_path,
        preview_path=render.preview_path,
        error=render.error,
        created_at=render.created_at,
        qa_status=render.qa_status,
        qa=QAReport.model_validate_json(render.qa_report) if render.qa_report else None,
    )
