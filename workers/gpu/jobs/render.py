# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render job: one segment of an Edit, or the concat of finished segments, written by ffmpeg.

Run by the worker agent (reelwright.render.worker, render executors) under its resource policy
(low priority, RAM cap). The job payload arrives as a JSON spec file because a long rough cut
does not fit on a Windows command line. ffmpeg reads the filter graph from a script file
for the same reason.

    python workers/gpu/jobs/render.py --spec rw-render-7.json --encoder h264_nvenc

Segment spec: {"mode": "segment", "otio", "start_frame", "end_frame", "output"} gives a .mkv with
H.264 video and PCM audio. Concat spec: {"mode": "concat", "segments": [...], "output"} gives an
.mp4 with the video stream-copied and AAC audio, and deletes the segments once it is written.
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
        argv = [*cmd.argv[:-1], "-f", "matroska", cmd.argv[-1]]
        rc = run(argv)
    if rc != 0:
        part.unlink(missing_ok=True)
        raise RenderFailed(f"ffmpeg exited {rc} on segment {out.name}")
    part.replace(out)
    return out


def concat_segments(spec: Mapping[str, Any], run: Run = _run) -> Path:
    segments = [str(s) for s in spec["segments"]]
    out = Path(str(spec["output"]))
    part = _part(str(out))
    with tempfile.TemporaryDirectory(prefix="rw-concat-") as tmp:
        listing = Path(tmp) / "segments.txt"
        listing.write_text(concat_list([os.path.abspath(s) for s in segments]), encoding="utf-8")
        rc = run(concat_command(segments, str(part), str(listing)))
    if rc != 0:
        part.unlink(missing_ok=True)
        raise RenderFailed(f"ffmpeg exited {rc} concatenating {len(segments)} segments")
    part.replace(out)
    for seg in segments:
        Path(seg).unlink(missing_ok=True)
    return out


def main(argv: Sequence[str] | None = None, run: Run = _run) -> int:
    parser = argparse.ArgumentParser(prog="render", description=__doc__.splitlines()[0])
    parser.add_argument("--spec", required=True)
    parser.add_argument("--encoder", default=GPU_ENCODER, choices=(GPU_ENCODER, CPU_ENCODER))
    args = parser.parse_args(argv)
    try:
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        if spec.get("mode") == "concat":
            out = concat_segments(spec, run)
        else:
            out = render_segment(spec, args.encoder, run)
    except (RenderFailed, KeyError, ValueError, OSError) as e:
        print(f"render failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"rendered {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
