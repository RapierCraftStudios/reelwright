# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pre-publish media QA of a final render (spike § Design system and quality gates).

One ffmpeg pass over the file finds black and frozen video, silence longer than 1.5 s that was not
marked deliberate, and the integrated loudness and true peak. Any finding fails the report. A
render whose QA has not passed cannot become a publish draft: ``require_publishable`` is the gate
the publish queue calls before drafting.

QA runs on a worker next to the final file (raw and final media never land on the dev server) and
uploads only this JSON report.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from reelwright.audio.loudness import (
    TARGET_I,
    TARGET_TP,
    TOLERANCE_LU,
    Capture,
    Loudness,
    capture_stderr,
    compliant,
    parse_ebur128,
)
from reelwright.db.models import QAInterval, QALoudness, QAReport, Render

BLACK_MIN_S = 0.5
BLACK_PIXEL_TH = 0.10
FREEZE_MIN_S = 2.0
FREEZE_NOISE = "-60dB"
SILENCE_MIN_S = 1.5
SILENCE_NOISE = "-50dB"

PASSED = "passed"


class QAFailed(RuntimeError):
    """ffmpeg could not analyse the file."""


class QABlocked(Exception):
    """The render has no passing QA report, so it cannot be drafted for publishing."""


def qa_argv(path: str) -> list[str]:
    video = (
        f"blackdetect=d={BLACK_MIN_S:g}:pix_th={BLACK_PIXEL_TH:g},"
        f"freezedetect=n={FREEZE_NOISE}:d={FREEZE_MIN_S:g}"
    )
    audio = f"silencedetect=n={SILENCE_NOISE}:d={SILENCE_MIN_S:g},ebur128=peak=true:framelog=quiet"
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-i", path,
        "-map", "0:v:0", "-vf", video, "-map", "0:a:0", "-af", audio, "-f", "null", "-",
    ]  # fmt: skip


_NUM = r"(-?\d+(?:\.\d+)?)"
_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_BLACK = re.compile(rf"black_start:\s*{_NUM}\s+black_end:\s*{_NUM}")
_FREEZE_START = re.compile(rf"freeze_start:\s*{_NUM}")
_FREEZE_END = re.compile(rf"freeze_end:\s*{_NUM}")
_SILENCE_START = re.compile(rf"silence_start:\s*{_NUM}")
_SILENCE_END = re.compile(rf"silence_end:\s*{_NUM}")


def _interval(start: float, end: float) -> QAInterval:
    start, end = max(0.0, start), max(0.0, end)
    return QAInterval(start=round(start, 3), end=round(end, 3), duration=round(end - start, 3))


def _media_duration(stderr: str) -> float | None:
    m = _DURATION.search(stderr)
    if m is None:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _spans(lines: Iterable[str], start: re.Pattern[str], end: re.Pattern[str], until: float | None
           ) -> list[QAInterval]:  # fmt: skip
    """start/end log pairs as intervals; one still open at the end of the file runs to its end."""
    out: list[QAInterval] = []
    opened: float | None = None
    for line in lines:
        if (m := start.search(line)) is not None:
            opened = float(m.group(1))
        elif (m := end.search(line)) is not None and opened is not None:
            out.append(_interval(opened, float(m.group(1))))
            opened = None
    if opened is not None and until is not None and until > opened:
        out.append(_interval(opened, until))
    return out


def _deliberate(span: QAInterval, ranges: Sequence[tuple[float, float]]) -> bool:
    return any(lo <= span.start and span.end <= hi for lo, hi in ranges)


def parse_qa(stderr: str, deliberate: Sequence[tuple[float, float]] = ()) -> QAReport:
    """The QA report from the stderr of ``qa_argv``.

    ``deliberate`` lists ``(start, end)`` seconds of intended pauses: a silence wholly inside one
    is not a finding.
    """
    lines = stderr.splitlines()
    until = _media_duration(stderr)
    black = [
        _interval(float(m.group(1)), float(m.group(2)))
        for line in lines
        if (m := _BLACK.search(line)) is not None
    ]
    frozen = _spans(lines, _FREEZE_START, _FREEZE_END, until)
    silence = [
        s
        for s in _spans(lines, _SILENCE_START, _SILENCE_END, until)
        if s.duration > SILENCE_MIN_S - 1e-3 and not _deliberate(s, deliberate)
    ]
    try:
        level: Loudness | None = parse_ebur128(stderr)
    except ValueError:
        level = None

    issues = [f"black video {s.start:.2f}-{s.end:.2f} s ({s.duration:.2f} s)" for s in black]
    issues += [f"frozen video {s.start:.2f}-{s.end:.2f} s ({s.duration:.2f} s)" for s in frozen]
    issues += [f"silence {s.start:.2f}-{s.end:.2f} s ({s.duration:.2f} s)" for s in silence]
    if level is None:
        issues.append("loudness could not be measured")
    elif not compliant(level):
        issues.append(
            f"loudness {level.integrated:.1f} LUFS, true peak {level.true_peak:.1f} dBTP "
            f"(target {TARGET_I:g} ±{TOLERANCE_LU:g} LUFS, peak at most {TARGET_TP:g} dBTP)"
        )
    return QAReport(
        passed=not issues,
        black=black,
        frozen=frozen,
        silence=silence,
        loudness=None if level is None else QALoudness(
            integrated=level.integrated, true_peak=level.true_peak
        ),
        issues=issues,
    )  # fmt: skip


def run_qa(
    path: str,
    deliberate: Sequence[tuple[float, float]] = (),
    capture: Capture = capture_stderr,
) -> QAReport:
    rc, err = capture(qa_argv(path))
    if rc != 0:
        tail = err.strip().splitlines()[-1:] or [""]
        raise QAFailed(f"ffmpeg exited {rc} analysing {path}: {tail[0]}")
    return parse_qa(err, deliberate)


def require_publishable(render: Render) -> None:
    """Raise QABlocked unless the render's media QA passed. Publish drafts call this first."""
    if render.qa_status != PASSED:
        state = render.qa_status or "not run"
        raise QABlocked(f"render {render.id} cannot be published: media QA {state}")
