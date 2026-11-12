# SPDX-License-Identifier: AGPL-3.0-or-later
"""OTIO timeline → ffmpeg render graph (spike § Runtime topology, § Render).

``build_render`` turns an Edit, or a ``[start_frame, end_frame)`` range of it, into an ffmpeg argv
and a filter script. Every video piece is converted to the timeline rate and then trimmed and padded
to its exact frame count. Every audio piece is resampled to 48 kHz and then trimmed and padded to
an exact sample count, so the render has the timeline's length to the frame. There is no
``-frames:v`` cap: ffmpeg closes the output when it reaches that cap, which drops audio still
buffered behind the last video frame (up to ~10 ms at a segment end). Sample positions are derived from timeline frames
(``round(frame * 48000 / fps)``), so segments rendered separately add up exactly.

Each piece has its own input with an accurate ``-ss`` seek. The decoder starts near the piece and
does not buffer earlier frames, which keeps memory flat on long renders under the worker's RAM cap.
One video track and one audio track are supported (camera or screen). Overlays, speed changes and
transitions come later and raise ``UnsupportedEdit``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

from spectracut.edit import Clip, Edit, Gap, Track, Transition

SAMPLE_RATE = 48_000
WIDTH = 1920
HEIGHT = 1080
GPU_ENCODER = "h264_nvenc"
CPU_ENCODER = "libx264"
ENCODER_ARGS: dict[str, tuple[str, ...]] = {
    GPU_ENCODER: ("-preset", "p7", "-rc", "vbr", "-cq", "19", "-b:v", "0"),
    CPU_ENCODER: ("-preset", "slow", "-crf", "18"),
}
SEGMENT_AUDIO_CODEC = "pcm_s16le"


class UnsupportedEdit(ValueError):
    """The edit uses something the render graph cannot draw yet."""


@dataclass(frozen=True)
class Piece:
    """One run of timeline frames: a span of a source file, or a gap when media is empty."""

    media: str
    source_in: int  # source frame of the first timeline frame
    start: int  # timeline frame, inclusive
    end: int  # timeline frame, exclusive

    @property
    def frames(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class RenderCommand:
    argv: list[str]
    filter_script: str
    frames: int
    samples: int


def sample_at(frame: int, fps: Fraction) -> int:
    """Audio sample index of a timeline frame boundary."""
    return round(Fraction(frame) * SAMPLE_RATE / fps)


def _track(edit: Edit, kind: str) -> Track | None:
    tracks = [t for t in edit.tracks if t.kind == kind]
    if len(tracks) > 1:
        raise UnsupportedEdit(f"only one {kind} track is supported (overlays later)")
    return tracks[0] if tracks else None


def pieces(track: Track | None, start: int, end: int) -> list[Piece]:
    """The parts of ``track`` inside ``[start, end)``. A short track is padded with a gap."""
    out: list[Piece] = []
    at = 0
    for item in track.items if track is not None else []:
        if isinstance(item, Transition):
            raise UnsupportedEdit("transitions are not supported yet")
        dur = item.duration
        lo, hi = max(at, start), min(at + dur, end)
        if lo < hi:
            if isinstance(item, Gap):
                out.append(Piece("", 0, lo, hi))
            else:
                _check_clip(item)
                out.append(Piece(item.media_ref, item.source_in + (lo - at), lo, hi))
        at += dur
        if at >= end:
            break
    covered = out[-1].end if out else start
    if covered < end:
        out.append(Piece("", 0, covered, end))
    return out


def _check_clip(clip: Clip) -> None:
    if clip.nested is not None:
        raise UnsupportedEdit(f"clip {clip.id}: nested edits are not supported yet")
    if clip.speed != 1.0 or clip.ramps:
        raise UnsupportedEdit(f"clip {clip.id}: speed changes are not supported yet")
    if not clip.media_ref:
        raise UnsupportedEdit(f"clip {clip.id}: no media")


def _seconds(frame: int, fps: Fraction) -> str:
    """Seek time, floored to the microsecond so an accurate seek keeps the frame itself."""
    us = math.floor(Fraction(frame) / fps * 1_000_000)
    return f"{us // 1_000_000}.{us % 1_000_000:06d}"


def build_render(
    edit: Edit,
    start_frame: int,
    end_frame: int,
    output: str,
    *,
    hw: bool,
    filter_script_path: str,
    audio_codec: str = SEGMENT_AUDIO_CODEC,
    width: int = WIDTH,
    height: int = HEIGHT,
) -> RenderCommand:
    """ffmpeg argv and filter script rendering timeline frames ``[start_frame, end_frame)``."""
    if not 0 <= start_frame < end_frame <= edit.duration:
        raise ValueError(f"range [{start_frame}, {end_frame}) is outside 0..{edit.duration}")
    fps = edit.rate.fps
    rate = f"{fps.numerator}/{fps.denominator}"
    video = pieces(_track(edit, "video"), start_frame, end_frame)
    audio = pieces(_track(edit, "audio"), start_frame, end_frame)

    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    lines: list[str] = []
    n_inputs = 0

    def add_input(piece: Piece) -> int:
        nonlocal n_inputs
        if hw:
            argv.extend(["-hwaccel", "cuda"])
        argv.extend(["-ss", _seconds(piece.source_in, fps), "-i", piece.media])
        n_inputs += 1
        return n_inputs - 1

    for n, p in enumerate(video):
        if p.media:
            src = f"[{add_input(p)}:v:0]"
            lines.append(
                f"{src}fps={rate},scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
                f"tpad=stop_mode=clone:stop={p.frames},"
                f"trim=end_frame={p.frames},setpts=PTS-STARTPTS[v{n}]"
            )
        else:
            lines.append(
                f"color=c=black:s={width}x{height}:r={rate},format=yuv420p,"
                f"trim=end_frame={p.frames},setpts=PTS-STARTPTS[v{n}]"
            )
    for n, p in enumerate(audio):
        count = sample_at(p.end, fps) - sample_at(p.start, fps)
        src = f"[{add_input(p)}:a:0]" if p.media else f"anullsrc=r={SAMPLE_RATE}:cl=stereo,"
        if p.media:
            src += f"aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,"
        lines.append(f"{src}apad,atrim=end_sample={count},asetpts=PTS-STARTPTS[a{n}]")
    vs = "".join(f"[v{n}]" for n in range(len(video)))
    as_ = "".join(f"[a{n}]" for n in range(len(audio)))
    lines.append(f"{vs}concat=n={len(video)}:v=1:a=0[vout]")
    lines.append(f"{as_}concat=n={len(audio)}:v=0:a=1[aout]")

    frames = end_frame - start_frame
    samples = sample_at(end_frame, fps) - sample_at(start_frame, fps)
    encoder = GPU_ENCODER if hw else CPU_ENCODER
    argv += ["-/filter_complex", filter_script_path, "-map", "[vout]", "-map", "[aout]"]
    argv += ["-r", rate, "-fps_mode", "cfr"]
    argv += ["-c:v", encoder, *ENCODER_ARGS[encoder], "-pix_fmt", "yuv420p"]
    argv += ["-c:a", audio_codec, "-ar", str(SAMPLE_RATE), "-ac", "2", output]
    return RenderCommand(argv, ";\n".join(lines) + "\n", frames, samples)


def concat_command(segments: Sequence[str], output: str, list_path: str) -> list[str]:
    """Join segment files: video stream-copied (each segment opens on a keyframe), AAC once."""
    if not segments:
        raise ValueError("no segments to concatenate")
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_path,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        output,
    ]


def concat_list(segments: Sequence[str]) -> str:
    """The concat demuxer list file body for ``segments``."""
    return "".join("file '" + s.replace("'", "'\\''") + "'\n" for s in segments)
