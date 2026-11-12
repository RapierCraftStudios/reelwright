# SPDX-License-Identifier: AGPL-3.0-or-later
"""Final renders split across free GPUs: segment plan, parallel claims, concat, failure."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import pytest
from spectracut.edit import Clip, Edit, FrameRate, Gap, Track
from sqlmodel import Session

from reelwright.db.models import Project, Render, RenderJob, WorkerHeartbeat
from reelwright.db.session import get_engine, migrate
from reelwright.edit.build import edit_to_otio_json
from reelwright.render import queue, split
from reelwright.render.graph import SAMPLE_RATE
from reelwright.render.registry import record_heartbeat

T0 = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
RENDER_JOB = Path(__file__).resolve().parents[2] / "workers" / "gpu" / "jobs" / "render.py"


def clip(cid: str, media: str, src_in: int, src_out: int) -> Clip:
    return Clip(id=cid, media_ref=media, source_in=src_in, source_out=src_out)


def make_edit(rate: FrameRate, items: list[object], audio: list[object] | None = None) -> Edit:
    tracks = [Track(id="v1", kind="video", items=items)]
    if audio is not None:
        tracks.append(Track(id="a1", kind="audio", items=audio))
    return Edit(id="e1", rate=rate, tracks=tracks)


EDIT = make_edit(
    FrameRate(numerator=30),
    [clip("c1", "D:/f/a.mp4", 0, 1000), clip("c2", "D:/f/b.mp4", 0, 700), Gap(duration=300)],
)  # 2000 frames, boundaries at 1000 and 1700


def beat(session: Session, name: str, free: int = 8000, caps: tuple[str, ...] = ("cuda", "nvenc")):
    hb = WorkerHeartbeat(
        name=name, gpu_model="RTX", vram_total_mb=12000, vram_free_mb=free, capabilities=list(caps)
    )
    record_heartbeat(session, hb, T0)


@pytest.fixture
def session(tmp_db: str) -> Session:
    migrate()
    return Session(get_engine())


@pytest.fixture
def project(session: Session) -> Project:
    p = Project(title="Render test", slug="render-test")
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def start(session: Session, project: Project, edit: Edit = EDIT) -> Render:
    return split.start_render(
        session, project, edit_to_otio_json(edit), "D:/out/final.mp4", T0, min_segment_s=1
    )


def payloads(session: Session, render: Render) -> list[dict[str, object]]:
    out = []
    for i in json.loads(render.job_ids):
        job = session.get(RenderJob, i)
        assert job is not None
        out.append(json.loads(job.payload))
    return out


def finish(session: Session, job: RenderJob, ok: bool = True, **outputs: object) -> Render | None:
    assert job.id is not None and job.worker is not None
    done = queue.complete(session, job.id, job.worker, ok, outputs)
    return split.advance(session, done)


# --- segment plan --------------------------------------------------------------------------------


def test_plan_covers_the_edit_and_cuts_at_the_nearest_clip_boundary() -> None:
    assert split.clip_boundaries(EDIT) == [1000, 1700]
    assert split.plan_segments(EDIT, 2, 30) == [(0, 1000), (1000, 2000)]
    assert split.plan_segments(EDIT, 1, 30) == [(0, 2000)]


def test_plan_without_a_usable_boundary_cuts_at_the_even_split() -> None:
    one_clip = make_edit(FrameRate(numerator=24), [clip("c1", "/m/a.mp4", 0, 999)])
    assert split.plan_segments(one_clip, 2, 30) == [(0, 500), (500, 999)]
    # boundary at 1000 is too close to the start for a 1200-frame minimum: split evenly instead
    assert split.plan_segments(EDIT, 2, 900) == [(0, 1000), (1000, 2000)]
    assert split.plan_segments(EDIT, 2, 1001) == [(0, 2000)]  # shorter than two minimums


@pytest.mark.parametrize("n", [1, 2, 3])
@pytest.mark.parametrize("minimum", [1, 30, 700, 999])
def test_plan_is_contiguous_and_complete(n: int, minimum: int) -> None:
    ranges = split.plan_segments(EDIT, n, minimum)
    assert ranges[0][0] == 0 and ranges[-1][1] == EDIT.duration
    assert all(a[1] == b[0] for a, b in zip(ranges, ranges[1:], strict=False))
    assert all(end - start >= min(minimum, EDIT.duration) for start, end in ranges)


# --- start, claims, concat -----------------------------------------------------------------------


def test_two_free_gpus_get_one_segment_each_at_the_same_time(
    session: Session, project: Project
) -> None:
    beat(session, "gpu-a")
    beat(session, "gpu-b")
    render = start(session, project)
    assert (render.segments, render.status) == (2, "queued")
    specs = payloads(session, render)
    assert [(p["start_frame"], p["end_frame"]) for p in specs] == [(0, 1000), (1000, 2000)]
    assert [p["output"] for p in specs] == ["D:/out/final.seg0.mov", "D:/out/final.seg1.mov"]
    assert all(p["mode"] == "segment" and p["render_id"] == render.id for p in specs)

    a = queue.next_job_for(session, "gpu-a", T0)
    b = queue.next_job_for(session, "gpu-b", T0)
    assert a is not None and b is not None and a.id != b.id
    assert (a.status, b.status) == ("claimed", "claimed")


def test_b_first_claims_too(session: Session, project: Project) -> None:
    beat(session, "gpu-a", free=6000)
    beat(session, "gpu-b", free=9000)  # the picker ranks b first
    render = start(session, project)
    b = queue.next_job_for(session, "gpu-b", T0)
    a = queue.next_job_for(session, "gpu-a", T0)
    assert a is not None and b is not None and {a.id, b.id} == set(json.loads(render.job_ids))


def test_one_free_gpu_gives_one_segment(session: Session, project: Project) -> None:
    beat(session, "gpu-a")
    beat(session, "cpu-1", caps=("cpu", "ffmpeg"))  # not a GPU card
    render = start(session, project)
    assert render.segments == 1
    assert [(p["start_frame"], p["end_frame"]) for p in payloads(session, render)] == [(0, 2000)]


def test_no_free_gpu_still_renders_in_one_segment(session: Session, project: Project) -> None:
    assert start(session, project).segments == 1


def test_concat_waits_for_every_segment_then_preview_is_queued(
    session: Session, project: Project
) -> None:
    beat(session, "gpu-a")
    beat(session, "gpu-b")
    render = start(session, project)
    a = queue.next_job_for(session, "gpu-a", T0)
    b = queue.next_job_for(session, "gpu-b", T0)
    assert a is not None and b is not None
    after_a = finish(session, a)
    assert after_a is not None and after_a.status == "rendering"
    assert after_a.concat_job_id is None

    after_b = finish(session, b)
    assert after_b is not None and after_b.status == "concatenating"
    concat = session.get(RenderJob, after_b.concat_job_id)
    assert concat is not None and concat.kind == "render" and concat.required_caps == ""
    spec = json.loads(concat.payload)
    assert spec["mode"] == "concat" and spec["output"] == "D:/out/final.mp4"
    assert spec["segments"] == ["D:/out/final.seg0.mov", "D:/out/final.seg1.mov"]

    claimed = queue.next_job_for(session, "gpu-a", T0)
    assert claimed is not None and claimed.id == concat.id
    after_concat = finish(session, claimed)
    assert after_concat is not None and after_concat.status == "previewing"
    assert after_concat.output_path == "D:/out/final.mp4"
    preview = session.get(RenderJob, after_concat.preview_job_id)
    assert preview is not None and preview.kind == "proxy"
    assert json.loads(preview.payload) == {
        "render_id": render.id,
        "input": "D:/out/final.mp4",
        "output": f"rw-render-preview-{render.id}.mp4",
        "upload": f"/api/projects/{project.id}/renders/{render.id}/preview",
    }


def test_a_failed_segment_fails_the_render(session: Session, project: Project) -> None:
    beat(session, "gpu-a")
    beat(session, "gpu-b")
    start(session, project)
    a = queue.next_job_for(session, "gpu-a", T0)
    b = queue.next_job_for(session, "gpu-b", T0)
    assert a is not None and b is not None
    failed = finish(session, a, ok=False, error="ffmpeg exited 1")
    assert failed is not None and failed.status == "failed"
    assert failed.error is not None and "ffmpeg exited 1" in failed.error
    later = finish(session, b)  # the other segment finishing later does not revive it
    assert later is not None and later.status == "failed" and later.concat_job_id is None


def test_advance_ignores_jobs_that_are_not_a_renders(session: Session) -> None:
    beat(session, "gpu-a")
    job = queue.enqueue(session, queue.JobKind.proxy, {"asset_id": 1, "input": "a", "output": "b"})
    claimed = queue.next_job_for(session, "gpu-a", T0)
    assert claimed is not None and claimed.id == job.id
    assert finish(session, claimed) is None


def test_unparsable_otio_is_rejected_before_anything_is_queued(
    session: Session, project: Project
) -> None:
    for bad in ("{}", "not json", '{"OTIO_SCHEMA": "Clip.2"}'):
        with pytest.raises(ValueError):
            split.start_render(session, project, bad, "D:/o.mp4", T0)
    assert session.get(Render, 1) is None


# --- real encode: two segments plus concat through the job script --------------------------------


def _have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _source(path: Path, seconds: int) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def _job(spec: dict[str, object], path: Path) -> None:
    path.write_text(json.dumps(spec), encoding="utf-8")
    subprocess.run(
        [sys.executable, str(RENDER_JOB), "--spec", str(path), "--encoder", "libx264"],
        check=True,
        capture_output=True,
    )


@pytest.mark.skipif(not _have_ffmpeg(), reason="needs ffmpeg and ffprobe")
@pytest.mark.parametrize("rate", [FrameRate(numerator=30000, denominator=1001), FrameRate(numerator=24)])
def test_two_segments_concat_without_a_seam(tmp_path: Path, rate: FrameRate) -> None:
    a = _source(tmp_path / "a.mp4", 3)
    b = _source(tmp_path / "b.mp4", 2)
    edit = make_edit(
        rate,
        [clip("c1", str(a), 7, 40), Gap(duration=4), clip("c2", str(b), 3, 30)],
        [clip("c1a", str(a), 7, 40), Gap(duration=4), clip("c2a", str(b), 3, 30)],
    )
    otio_json = edit_to_otio_json(edit)
    final = tmp_path / "final.mp4"
    ranges = split.plan_segments(edit, 2, 10)
    assert len(ranges) == 2
    segments = []
    for i, (lo, hi) in enumerate(ranges):
        out = split.segment_path(str(final), i)
        spec = {"mode": "segment", "otio": otio_json, "start_frame": lo, "end_frame": hi}
        _job({**spec, "output": out}, tmp_path / f"seg{i}.json")
        segments.append(out)
    _job({"mode": "concat", "segments": segments, "output": str(final)}, tmp_path / "cat.json")
    assert final.is_file() and not any(Path(s).exists() for s in segments)

    pts = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts",
            "-of", "csv=p=0", str(final),
        ],
        capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    ticks = sorted(int(p.strip(",")) for p in pts)
    assert len(ticks) == edit.duration == 64
    steps = {b - a for a, b in zip(ticks, ticks[1:], strict=False)}
    assert len(steps) == 1  # constant 1/R spacing, including across the seam
    tb = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
            "stream=time_base", "-of", "csv=p=0", str(final),
        ],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    assert Fraction(steps.pop()) * Fraction(tb) == 1 / rate.fps

    pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(final), "-map", "0:a", "-f", "s16le", "-ac", "2", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    assert len(pcm) // 4 == round(Fraction(edit.duration) * SAMPLE_RATE / rate.fps)
