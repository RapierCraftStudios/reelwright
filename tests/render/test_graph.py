# SPDX-License-Identifier: AGPL-3.0-or-later
"""OTIO edit → ffmpeg render graph: frame-exact lengths, sample-exact audio, unsupported edits."""

from __future__ import annotations

import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest
from spectracut.edit import Clip, Edit, FrameRate, Gap, SpeedRamp, Track, Transition

from reelwright.render.graph import (
    SAMPLE_RATE,
    UnsupportedEdit,
    build_render,
    concat_command,
    concat_list,
    pieces,
    sample_at,
)

RATES = [
    FrameRate(numerator=24),
    FrameRate(numerator=30),
    FrameRate(numerator=30000, denominator=1001),
]


def make_edit(rate: FrameRate, video: list[object], audio: list[object] | None = None) -> Edit:
    tracks = [Track(id="v1", kind="video", items=video)]
    if audio is not None:
        tracks.append(Track(id="a1", kind="audio", items=audio))
    return Edit(id="e1", rate=rate, tracks=tracks)


def clip(cid: str, media: str, src_in: int, src_out: int, **kw: object) -> Clip:
    return Clip(id=cid, media_ref=media, source_in=src_in, source_out=src_out, **kw)


def sample_edit(rate: FrameRate) -> Edit:
    return make_edit(
        rate,
        [clip("c1", "/m/a.mp4", 10, 47), Gap(duration=5), clip("c2", "/m/b.mp4", 0, 29)],
        [clip("c1a", "/m/a.mp4", 10, 47), Gap(duration=5), clip("c2a", "/m/b.mp4", 0, 29)],
    )


def trim_frames(script: str) -> list[int]:
    return [int(n) for n in re.findall(r"trim=end_frame=(\d+)", script)]


def audio_samples(script: str) -> list[int]:
    return [int(n) for n in re.findall(r"atrim=end_sample=(\d+)", script)]


@pytest.mark.parametrize("rate", RATES, ids=lambda r: f"{r.numerator}/{r.denominator}")
def test_trims_sum_to_the_timeline_length(rate: FrameRate) -> None:
    edit = sample_edit(rate)
    cmd = build_render(edit, 0, edit.duration, "out.mkv", hw=False, filter_script_path="g.txt")
    assert cmd.frames == edit.duration == 71
    assert sum(trim_frames(cmd.filter_script)) == edit.duration
    argv = cmd.argv
    assert "-frames:v" not in argv  # it would cut the audio buffered behind the last frame
    assert argv[argv.index("-r") + 1] == f"{rate.fps.numerator}/{rate.fps.denominator}"
    expected = round(Fraction(edit.duration) * SAMPLE_RATE / rate.fps)
    assert cmd.samples == expected == sum(audio_samples(cmd.filter_script))


@pytest.mark.parametrize("rate", RATES, ids=lambda r: f"{r.numerator}/{r.denominator}")
def test_split_ranges_add_up_to_the_whole(rate: FrameRate) -> None:
    edit = sample_edit(rate)
    cuts = [0, 20, 40, 41, edit.duration]  # mid-clip, in the gap, one frame
    parts = [
        build_render(edit, a, b, f"s{a}.mkv", hw=False, filter_script_path="g.txt")
        for a, b in zip(cuts, cuts[1:], strict=False)
    ]
    assert sum(p.frames for p in parts) == edit.duration
    whole = build_render(edit, 0, edit.duration, "o.mkv", hw=False, filter_script_path="g.txt")
    assert sum(p.samples for p in parts) == whole.samples
    for p in parts:
        assert sum(trim_frames(p.filter_script)) == p.frames
        assert sum(audio_samples(p.filter_script)) == p.samples


def test_partial_range_trims_the_cut_clip_and_seeks_into_it() -> None:
    edit = sample_edit(FrameRate(numerator=24))
    video = pieces(edit.tracks[0], 20, 50)
    assert [(p.media, p.source_in, p.start, p.end) for p in video] == [
        ("/m/a.mp4", 30, 20, 37),
        ("", 0, 37, 42),
        ("/m/b.mp4", 0, 42, 50),
    ]
    cmd = build_render(edit, 20, 50, "o.mkv", hw=False, filter_script_path="g.txt")
    # source frame 30 at 24 fps = 1.25 s
    assert cmd.argv[cmd.argv.index("-ss") + 1] == "1.250000"
    assert "color=c=black" in cmd.filter_script and "anullsrc" in cmd.filter_script


def test_short_track_and_no_audio_track_are_padded_with_silence() -> None:
    edit = make_edit(FrameRate(numerator=30), [clip("c1", "/m/a.mp4", 0, 60)])
    cmd = build_render(edit, 0, 60, "o.mkv", hw=False, filter_script_path="g.txt")
    assert "anullsrc" in cmd.filter_script and cmd.samples == 2 * SAMPLE_RATE
    assert pieces(None, 0, 10)[0].media == ""


def test_sample_positions_are_cumulative_not_per_frame() -> None:
    fps = Fraction(30000, 1001)
    assert sample_at(1, fps) == 1602  # 1601.6 rounded
    assert sum(sample_at(i + 1, fps) - sample_at(i, fps) for i in range(30000)) == sample_at(
        30000, fps
    )


@pytest.mark.parametrize(
    ("video", "match"),
    [
        ([clip("c1", "/m/a.mp4", 0, 30, speed=2.0)], "speed"),
        (
            [
                clip(
                    "c1",
                    "/m/a.mp4",
                    0,
                    30,
                    ramps=[SpeedRamp(start=0, end=10, speed_from=1, speed_to=2)],
                )
            ],
            "speed",
        ),
        (
            [clip("c1", "/m/a.mp4", 0, 30), Transition(), clip("c2", "/m/b.mp4", 0, 30)],
            "transition",
        ),
        ([clip("c1", "", 0, 30)], "no media"),
    ],
)
def test_unsupported_edits_raise(video: list[object], match: str) -> None:
    edit = make_edit(FrameRate(numerator=24), video)
    with pytest.raises(UnsupportedEdit, match=match):
        build_render(edit, 0, edit.duration, "o.mkv", hw=False, filter_script_path="g.txt")


def test_two_video_tracks_are_overlays_and_unsupported() -> None:
    edit = Edit(
        id="e1",
        tracks=[
            Track(id="v1", items=[clip("c1", "/m/a.mp4", 0, 30)]),
            Track(id="v2", items=[clip("c2", "/m/b.mp4", 0, 30)]),
        ],
    )
    with pytest.raises(UnsupportedEdit, match="one video track"):
        build_render(edit, 0, 30, "o.mkv", hw=False, filter_script_path="g.txt")


def test_range_outside_the_edit_is_rejected() -> None:
    edit = sample_edit(FrameRate(numerator=24))
    for a, b in ((0, 0), (-1, 5), (5, edit.duration + 1)):
        with pytest.raises(ValueError):
            build_render(edit, a, b, "o.mkv", hw=False, filter_script_path="g.txt")


def test_hw_uses_nvenc_and_cuda_cpu_uses_x264() -> None:
    edit = sample_edit(FrameRate(numerator=24))
    gpu = build_render(edit, 0, 10, "o.mkv", hw=True, filter_script_path="g.txt").argv
    cpu = build_render(edit, 0, 10, "o.mkv", hw=False, filter_script_path="g.txt").argv
    assert "h264_nvenc" in gpu and "cuda" in gpu and gpu[gpu.index("-preset") + 1] == "p7"
    assert gpu[gpu.index("-rc") + 1] == "vbr"
    assert "libx264" in cpu and not {"cuda", "h264_nvenc"} & set(cpu)
    assert cpu[cpu.index("-/filter_complex") + 1] == "g.txt"  # graph read from the script file


def test_concat_copies_video_and_encodes_aac_once() -> None:
    argv = concat_command(["/x/a.seg0.mkv", "/x/a.seg1.mkv"], "/x/a.mp4.part", "/t/list.txt")
    assert argv[argv.index("-f") + 1] == "concat" and argv[argv.index("-safe") + 1] == "0"
    assert argv[argv.index("-c:v") + 1] == "copy" and argv[argv.index("-c:a") + 1] == "aac"
    assert "+faststart" in argv
    assert concat_list(["/x/it's.mkv"]) == "file '/x/it'\\''s.mkv'\n"
    with pytest.raises(ValueError):
        concat_command([], "o.mp4", "l.txt")


def _have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def make_source(path: Path, seconds: int, rate: str = "25") -> Path:
    """A test clip with video (testsrc at ``rate``) and 44.1 kHz audio (sine)."""
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=320x240:rate={rate}:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def probe_frames(path: Path) -> int:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return int(out.strip().strip(","))


@pytest.mark.skipif(not _have_ffmpeg(), reason="needs ffmpeg and ffprobe")
def test_real_encode_has_the_timeline_length_to_the_frame(tmp_path: Path) -> None:
    a = make_source(tmp_path / "a.mp4", 3)
    b = make_source(tmp_path / "b.mp4", 2)
    edit = make_edit(
        FrameRate(numerator=30000, denominator=1001),
        [clip("c1", str(a), 7, 40), Gap(duration=4), clip("c2", str(b), 3, 30)],
        [clip("c1a", str(a), 7, 40), Gap(duration=4), clip("c2a", str(b), 3, 30)],
    )
    script = tmp_path / "graph.txt"
    out = tmp_path / "out.mkv"
    cmd = build_render(
        edit, 0, edit.duration, str(out), hw=False, filter_script_path=str(script),
        width=320, height=240,
    )  # fmt: skip
    script.write_text(cmd.filter_script, encoding="utf-8")
    argv = [*cmd.argv[:4], "-loglevel", "error", *cmd.argv[4:]]
    subprocess.run(argv, check=True)
    assert probe_frames(out) == edit.duration == 64
