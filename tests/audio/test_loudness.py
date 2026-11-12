# SPDX-License-Identifier: AGPL-3.0-or-later
"""EBU R128 mastering: real two-pass encodes land on -14 LUFS / -1 dBTP; parsers; compliance."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from reelwright.audio.cleanup import voice_chain
from reelwright.audio.loudness import (
    Loudness,
    LoudnessError,
    compliant,
    ebur128_argv,
    master,
    measure,
    measure_filter,
    normalize_filter,
    parse_ebur128,
    parse_loudnorm_json,
)

HAVE_FFMPEG = shutil.which("ffmpeg") is not None

LOUDNORM = """[Parsed_loudnorm_0 @ 0x55]
{
\t"input_i" : "-40.10",
\t"input_tp" : "-36.99",
\t"input_lra" : "0.00",
\t"input_thresh" : "-50.10",
\t"output_i" : "-14.00",
\t"output_tp" : "-11.00",
\t"output_lra" : "0.00",
\t"output_thresh" : "-24.00",
\t"normalization_type" : "linear",
\t"target_offset" : "0.00"
}
"""

EBUR128 = """[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -14.3 LUFS
    Threshold: -24.3 LUFS

  Loudness range:
    LRA:         0.0 LU

  True peak:
    Peak:       -1.2 dBFS
"""


def _fixture(path: Path, audio: str) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25:duration=6",
            "-f", "lavfi", "-i", audio,
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le", "-shortest", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


QUIET = "sine=frequency=440:sample_rate=48000:duration=6,volume=0.01"
HOT = (
    "sine=frequency=440:sample_rate=48000:duration=6,volume=0.99[s];"
    "anoisesrc=color=pink:amplitude=0.5:sample_rate=48000:duration=6[n];"
    "[s][n]amix=inputs=2:normalize=0,alimiter=limit=0.99"
)
SPEECH_LEVEL = "sine=frequency=440:sample_rate=48000:duration=6,volume=0.3"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg")
@pytest.mark.parametrize(
    ("audio", "pre"),
    [
        (QUIET, None),
        (HOT, None),
        # afftdn treats anything under its -25 dB floor as noise, so cleanup runs on voice level
        (SPEECH_LEVEL, voice_chain()),
        (HOT, voice_chain()),
    ],
    ids=["quiet", "hot", "cleaned", "cleaned-hot"],
)
def test_master_lands_on_target(tmp_path: Path, audio: str, pre: str | None) -> None:
    source = _fixture(tmp_path / "in.mov", audio)
    out = tmp_path / "out.mp4"
    level = master(str(source), str(out), pre)
    again = measure(str(out))  # re-measured on the delivered AAC/mp4
    assert again == level
    assert abs(again.integrated + 14) <= 1, again
    assert again.true_peak <= -1, again
    assert compliant(again)
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.split() == ["h264", "aac"]


def test_parse_loudnorm_json_takes_the_last_block() -> None:
    got = parse_loudnorm_json("{ noise }\n" + LOUDNORM.replace("-40.10", "-1") + LOUDNORM)
    assert got["input_i"] == "-40.10" and got["target_offset"] == "0.00"
    with pytest.raises(ValueError):
        parse_loudnorm_json("Input #0, lavfi\n")


def test_filters() -> None:
    assert measure_filter() == "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json"
    assert measure_filter("highpass=f=80") == (
        "highpass=f=80,loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json"
    )
    second = normalize_filter(parse_loudnorm_json(LOUDNORM), "highpass=f=80")
    assert second == (
        "highpass=f=80,loudnorm=I=-14:TP=-1.5:LRA=11:measured_I=-40.1:measured_TP=-36.99:"
        "measured_LRA=0:measured_thresh=-50.1:offset=0:linear=true:print_format=summary,"
        "aresample=48000"
    )
    with pytest.raises(ValueError, match="no audible audio"):
        normalize_filter({**parse_loudnorm_json(LOUDNORM), "input_i": "-inf"})
    with pytest.raises(ValueError):
        normalize_filter({"input_i": "-20"})


def test_parse_ebur128_summary() -> None:
    assert parse_ebur128("[Parsed_ebur128_0] t: 0.1 M: -20\n" + EBUR128) == Loudness(-14.3, -1.2)
    with pytest.raises(ValueError):
        parse_ebur128("no summary")
    with pytest.raises(ValueError):
        parse_ebur128("Summary:\n  I: -14 LUFS\n")


def test_ebur128_argv_maps_first_audio() -> None:
    argv = ebur128_argv("a.mp4")
    assert argv[argv.index("-i") + 1] == "a.mp4" and "-nostats" in argv
    assert argv[argv.index("-map") + 1] == "0:a:0"
    assert argv[argv.index("-af") + 1].startswith("ebur128=peak=true")
    assert argv[-3:] == ["-f", "null", "-"]


@pytest.mark.parametrize(
    ("integrated", "peak", "ok"),
    [
        (-15.0, -1.0, True),
        (-13.0, -1.0, True),
        (-14.0, -3.0, True),
        (-15.1, -1.0, False),
        (-12.9, -2.0, False),
        (-14.0, -0.9, False),
    ],
)
def test_compliance_edges(integrated: float, peak: float, ok: bool) -> None:
    assert compliant(Loudness(integrated, peak)) is ok


def test_ffmpeg_failures_raise() -> None:
    with pytest.raises(LoudnessError):
        measure("x.mp4", capture=lambda argv: (1, ""))
    with pytest.raises(LoudnessError):
        master("x.mov", "y.mp4", capture=lambda argv: (1, ""))
    calls: list[list[str]] = []

    def second_fails(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return (0, LOUDNORM) if len(calls) == 1 else (1, "")

    with pytest.raises(LoudnessError, match="normalising"):
        master("x.mov", "y.mp4", capture=second_fails)
