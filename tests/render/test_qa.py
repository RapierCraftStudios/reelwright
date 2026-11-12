# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pre-publish media QA: real fixtures with black video and silence, log parsing, publish gate."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reelwright.db.models import QAReport, Render
from reelwright.render.qa import (
    QABlocked,
    QAFailed,
    parse_qa,
    qa_argv,
    require_publishable,
    run_qa,
)

QA_JOB = Path(__file__).resolve().parents[2] / "workers" / "gpu" / "jobs" / "qa.py"
HAVE_FFMPEG = shutil.which("ffmpeg") is not None

SUMMARY = """[Parsed_ebur128_3 @ 0x1] Summary:

  Integrated loudness:
    I:         {i} LUFS
    Threshold: -24.0 LUFS

  True peak:
    Peak:       {tp} dBFS
"""


def _summary(i: float = -14.2, tp: float = -1.5) -> str:
    return SUMMARY.format(i=i, tp=tp)


def _fixture(path: Path, clean: bool) -> Path:
    if clean:
        video = "testsrc=size=160x120:rate=25:duration=6"
        audio = "sine=frequency=440:sample_rate=48000:duration=6"
        graph = f"{video}[v];{audio}[a]"
    else:  # testsrc 2 s, black 2 s, testsrc 2 s; tone 1.5 s, silence 3 s, tone 1.5 s
        graph = (
            "testsrc=size=160x120:rate=25:duration=2[v0];"
            "color=c=black:size=160x120:rate=25:duration=2[v1];"
            "testsrc=size=160x120:rate=25:duration=2[v2];"
            "[v0][v1][v2]concat=n=3:v=1:a=0[v];"
            "sine=frequency=440:sample_rate=48000:duration=1.5[a0];"
            "anullsrc=r=48000:cl=mono,atrim=duration=3[a1];"
            "sine=frequency=440:sample_rate=48000:duration=1.5[a2];"
            "[a0][a1][a2]concat=n=3:v=0:a=1[a]"
        )
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=48000", "-filter_complex", graph,
            "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-t", "6", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg")
def test_qa_flags_a_2s_black_frame_and_a_3s_silence(tmp_path: Path) -> None:
    report = run_qa(str(_fixture(tmp_path / "bad.mp4", clean=False)))
    assert report.passed is False
    assert len(report.black) == 1 and abs(report.black[0].duration - 2.0) <= 0.15
    assert abs(report.black[0].start - 2.0) <= 0.15
    assert len(report.silence) == 1 and abs(report.silence[0].duration - 3.0) <= 0.15
    assert abs(report.silence[0].start - 1.5) <= 0.15
    assert any(i.startswith("black video") for i in report.issues)
    assert any(i.startswith("silence") for i in report.issues)
    assert report.loudness is not None
    pause = (report.silence[0].start - 0.1, report.silence[0].end + 0.1)
    assert run_qa(str(tmp_path / "bad.mp4"), deliberate=[pause]).silence == []


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg")
def test_clean_fixture_has_no_black_or_silence(tmp_path: Path) -> None:
    report = run_qa(str(_fixture(tmp_path / "ok.mp4", clean=True)))
    assert (report.black, report.frozen, report.silence) == ([], [], [])
    assert report.loudness is not None  # a raw tone is not mastered, so only loudness may fail
    assert all(i.startswith("loudness") for i in report.issues)


def test_parse_blackdetect_freezedetect_silencedetect() -> None:
    log = "\n".join(
        [
            "  Duration: 00:00:10.00, start: 0.000000, bitrate: 100 kb/s",
            "[blackdetect @ 0x1] black_start:2 black_end:4.04 black_duration:2.04",
            "[freezedetect @ 0x2] lavfi.freezedetect.freeze_start: 5.2",
            "[freezedetect @ 0x2] lavfi.freezedetect.freeze_duration: 2.5",
            "[freezedetect @ 0x2] lavfi.freezedetect.freeze_end: 7.7",
            "[silencedetect @ 0x3] silence_start: 1.5",
            "[silencedetect @ 0x3] silence_end: 4.5 | silence_duration: 3",
            "[silencedetect @ 0x3] silence_start: 6",
            "[silencedetect @ 0x3] silence_end: 6.8 | silence_duration: 0.8",
            "[freezedetect @ 0x2] lavfi.freezedetect.freeze_start: 8",  # open to the end
            _summary(),
        ]
    )
    report = parse_qa(log)
    assert [(s.start, s.end, s.duration) for s in report.black] == [(2.0, 4.04, 2.04)]
    assert [(s.start, s.end) for s in report.frozen] == [(5.2, 7.7), (8.0, 10.0)]
    assert [(s.start, s.end, s.duration) for s in report.silence] == [(1.5, 4.5, 3.0)]
    assert report.loudness is not None and report.loudness.integrated == -14.2
    assert len(report.issues) == 4 and report.passed is False


def test_deliberate_range_exempts_a_silence() -> None:
    log = "silence_start: 10\nsilence_end: 13 | silence_duration: 3\n" + _summary()
    assert len(parse_qa(log).silence) == 1
    exempt = parse_qa(log, deliberate=[(9.5, 13.5)])
    assert exempt.silence == [] and exempt.passed is True
    partial = parse_qa(log, deliberate=[(11.0, 13.5)])  # only part of it was intended
    assert len(partial.silence) == 1 and partial.passed is False


def test_loudness_is_part_of_the_verdict() -> None:
    assert parse_qa(_summary()).passed is True
    loud = parse_qa(_summary(i=-11.0))
    assert loud.passed is False and loud.issues[0].startswith("loudness -11.0 LUFS")
    assert parse_qa(_summary(tp=-0.5)).passed is False
    unmeasured = parse_qa("no summary")
    assert unmeasured.loudness is None and unmeasured.issues == ["loudness could not be measured"]


def test_qa_argv_is_one_pass_with_every_detector() -> None:
    argv = qa_argv("final.mp4")
    vf, af = argv[argv.index("-vf") + 1], argv[argv.index("-af") + 1]
    assert "blackdetect=d=0.5:pix_th=0.1" in vf and "freezedetect=n=-60dB:d=2" in vf
    assert "silencedetect=n=-50dB:d=1.5" in af and "ebur128=peak=true" in af
    assert argv[-3:] == ["-f", "null", "-"]


def test_run_qa_reports_an_ffmpeg_failure() -> None:
    with pytest.raises(QAFailed, match="exited 1"):
        run_qa("x.mp4", capture=lambda argv: (1, "x.mp4: No such file or directory"))


def _render(state: str | None) -> Render:
    return Render(
        id=7, project_id=1, otio="{}", fps_num=30, frames=1, target_path="D:/o.mp4", qa_status=state
    )


@pytest.mark.parametrize("state", [None, "pending", "failed", "error"])
def test_publish_draft_is_blocked_unless_qa_passed(state: str | None) -> None:
    render = _render(state)
    with pytest.raises(QABlocked, match="render 7 cannot be published"):
        require_publishable(render)


def test_passed_qa_is_publishable() -> None:
    require_publishable(_render("passed"))


def _qa_job() -> Any:
    spec = importlib.util.spec_from_file_location("rw_qa_job", QA_JOB)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_qa_job_writes_the_report_and_exits_by_analysis_not_verdict(tmp_path: Path) -> None:
    job = _qa_job()
    out = tmp_path / "r.json"
    bad = "black_start:0 black_end:2 black_duration:2\n" + _summary()
    assert job.main(["--input", "f.mp4", "--output", str(out)], lambda argv: (0, bad)) == 0
    report = QAReport.model_validate(json.loads(out.read_text(encoding="utf-8")))
    assert report.passed is False and report.black[0].duration == 2.0
    assert job.main(["--input", "f.mp4", "--output", str(out)], lambda argv: (0, _summary())) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["passed"] is True
    gone = tmp_path / "g.json"
    assert job.main(["--input", "f.mp4", "--output", str(gone)], lambda argv: (1, "boom")) == 1
    assert not gone.exists()
