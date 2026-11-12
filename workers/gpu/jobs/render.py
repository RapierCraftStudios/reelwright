# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render job: one segment of an Edit, or the concat of finished segments, written by ffmpeg.

Run by the worker agent (reelwright.render.worker, render executors) under its resource policy
(low priority, RAM cap). The job payload arrives as a JSON spec file because a long rough cut
does not fit on a Windows command line. ffmpeg reads the filter graph from a script file
for the same reason.

    python workers/gpu/jobs/render.py --spec rw-render-7.json --encoder h264_nvenc

Segment spec: {"mode": "segment", "otio", "start_frame", "end_frame", "output"} gives a .mov with
H.264 video and PCM audio. QuickTime keeps the exact 1/fps timescale; Matroska rounds timestamps
to milliseconds, which jitters the frame spacing across a seam at 30000/1001. Concat spec:
{"mode": "concat", "segments": [...], "output"} gives an .mp4 with the video stream-copied and AAC
audio, and deletes the segments once it is written. With ``"master": true`` the joined audio is
mastered on the way: a loudness analysis pass first, then voice cleanup and a linear gain to
-14 LUFS / -1 dBTP in the concat itself (reelwright.audio).
Every output is written to ``<output>.part`` first and then moved into place.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from reelwright.audio.cleanup import voice_chain
from reelwright.audio.loudness import Capture, analysis_argv, normalize_filter, parse_loudnorm_json
from reelwright.render.graph import (
    CPU_ENCODER,
    GPU_ENCODER,
    build_render,
    concat_command,
    concat_list,
)
from reelwright.render.split import parse_otio

Run = Callable[[list[str]], int]


class RenderFailed(RuntimeError):
    pass


def _run(argv: list[str]) -> int:
    return subprocess.run(argv, check=False).returncode


def _measure(argv: list[str]) -> tuple[int, str]:
    done = subprocess.run(argv, capture_output=True, text=True, errors="replace", check=False)
    return done.returncode, done.stderr


def _part(output: str) -> Path:
    out = Path(output)
    return out.with_name(out.name + ".part")


def render_segment(spec: Mapping[str, Any], encoder: str, run: Run = _run) -> Path:
    edit = parse_otio(str(spec["otio"]))
    out = Path(str(spec["output"]))
    part = _part(str(out))
    with tempfile.TemporaryDirectory(prefix="rw-render-") as tmp:
        script = Path(tmp) / "graph.txt"
        cmd = build_render(
            edit,
            int(spec["start_frame"]),
            int(spec["end_frame"]),
            str(part),
            hw=encoder == GPU_ENCODER,
            filter_script_path=str(script),
        )
        script.write_text(cmd.filter_script, encoding="utf-8")
        argv = [*cmd.argv[:-1], "-f", "mov", cmd.argv[-1]]
        rc = run(argv)
    if rc != 0:
        part.unlink(missing_ok=True)
        raise RenderFailed(f"ffmpeg exited {rc} on segment {out.name}")
    part.replace(out)
    return out


def _master_filter(listing: Path, measure: Capture) -> str:
    """Cleanup plus the second loudnorm pass, from an analysis pass over the joined audio."""
    inputs = ["-f", "concat", "-safe", "0", "-i", str(listing)]
    rc, err = measure(analysis_argv(inputs, voice_chain()))
    if rc != 0:
        raise RenderFailed(f"ffmpeg exited {rc} in the loudness analysis pass")
    return normalize_filter(parse_loudnorm_json(err), voice_chain())


def concat_segments(
    spec: Mapping[str, Any], run: Run = _run, measure: Capture = _measure
) -> Path:
    segments = [str(s) for s in spec["segments"]]
    out = Path(str(spec["output"]))
    part = _part(str(out))
    with tempfile.TemporaryDirectory(prefix="rw-concat-") as tmp:
        listing = Path(tmp) / "segments.txt"
        listing.write_text(concat_list([os.path.abspath(s) for s in segments]), encoding="utf-8")
        audio_filter = _master_filter(listing, measure) if spec.get("master") else None
        rc = run(concat_command(segments, str(part), str(listing), audio_filter=audio_filter))
    if rc != 0:
        part.unlink(missing_ok=True)
        raise RenderFailed(f"ffmpeg exited {rc} concatenating {len(segments)} segments")
    part.replace(out)
    for seg in segments:
        Path(seg).unlink(missing_ok=True)
    return out


def main(
    argv: Sequence[str] | None = None, run: Run = _run, measure: Capture = _measure
) -> int:
    parser = argparse.ArgumentParser(prog="render", description=__doc__.splitlines()[0])
    parser.add_argument("--spec", required=True)
    parser.add_argument("--hwaccel", default="", help="accepted for symmetry; implied by encoder")
    parser.add_argument("--encoder", default=GPU_ENCODER, choices=(GPU_ENCODER, CPU_ENCODER))
    args = parser.parse_args(argv)
    try:
        spec_path = Path(args.spec)
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        if (
            spec_path.name.startswith("rw-render-")
            and spec_path.parent.resolve() == Path(tempfile.gettempdir()).resolve()
        ):
            spec_path.unlink(missing_ok=True)  # written by the worker agent for this run only
        if spec.get("mode") == "concat":
            out = concat_segments(spec, run, measure)
        else:
            out = render_segment(spec, args.encoder, run)
    except (RenderFailed, KeyError, ValueError, OSError) as e:
        print(f"render failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"rendered {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
