# SPDX-License-Identifier: AGPL-3.0-or-later
"""EBU R128 loudness: two-pass normalisation to -14 LUFS / -1 dBTP, and measurement.

The YouTube target is -14 LUFS integrated with a true peak at most -1 dBTP (spike § Design system
and quality gates). Pass one runs ``loudnorm`` in analysis mode and prints its measurement as JSON.
Pass two feeds that measurement back with ``linear=true``, so the whole programme gets one gain
and keeps its dynamics. loudnorm aims at -1.5 dBTP, because the AAC encode after it can raise
inter-sample peaks a few tenths of a dB. The delivered file is then still at most -1 dBTP.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from reelwright.render.graph import SAMPLE_RATE

TARGET_I = -14.0
TARGET_TP = -1.0
TARGET_LRA = 11.0
TOLERANCE_LU = 1.0
LOUDNORM_TP = -1.5  # headroom for the AAC encode after loudnorm
AAC_BITRATE = "192k"

# Runs a command and returns (returncode, stderr). ffmpeg prints its measurements on stderr.
Capture = Callable[[list[str]], tuple[int, str]]


class LoudnessError(RuntimeError):
    """ffmpeg failed or printed no measurement."""


@dataclass(frozen=True)
class Loudness:
    integrated: float  # LUFS
    true_peak: float  # dBTP


def capture_stderr(argv: list[str]) -> tuple[int, str]:
    done = subprocess.run(argv, capture_output=True, text=True, errors="replace", check=False)
    return done.returncode, done.stderr


def _loudnorm(extra: str = "") -> str:
    base = f"loudnorm=I={TARGET_I:g}:TP={LOUDNORM_TP:g}:LRA={TARGET_LRA:g}"
    return f"{base}:{extra}" if extra else base


def _chain(pre: str | None, *filters: str) -> str:
    return ",".join([f for f in (pre, *filters) if f])


def measure_filter(pre: str | None = None) -> str:
    """Pass one: optional cleanup ``pre`` chain, then loudnorm analysis printed as JSON."""
    return _chain(pre, _loudnorm("print_format=json"))


def parse_loudnorm_json(stderr: str) -> dict[str, str]:
    """The measurement block loudnorm prints at the end of pass one."""
    blocks = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr)
    if not blocks:
        raise ValueError("no loudnorm measurement in ffmpeg output")
    data: dict[str, Any] = json.loads(blocks[-1])
    return {k: str(v) for k, v in data.items()}


def normalize_filter(measured: Mapping[str, str], pre: str | None = None) -> str:
    """Pass two: one linear gain to the target, from the pass-one measurement."""
    try:
        values = {
            "measured_I": float(measured["input_i"]),
            "measured_TP": float(measured["input_tp"]),
            "measured_LRA": float(measured["input_lra"]),
            "measured_thresh": float(measured["input_thresh"]),
            "offset": float(measured["target_offset"]),
        }
    except (KeyError, ValueError) as e:
        raise ValueError(f"bad loudnorm measurement: {e}") from None
    for name, value in values.items():
        if value != value or value in (float("inf"), float("-inf")):  # silent input
            raise ValueError(f"loudnorm measured {name}={value}: no audible audio")
    args = ":".join(f"{k}={v:g}" for k, v in values.items())
    return _chain(pre, _loudnorm(f"{args}:linear=true:print_format=summary"),
                  f"aresample={SAMPLE_RATE}")  # fmt: skip


def ebur128_argv(path: str) -> list[str]:
    """Measure integrated loudness and true peak of the first audio stream of ``path``."""
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-i", path,
        "-map", "0:a:0", "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-",
    ]  # fmt: skip


_SUMMARY = re.compile(r"Summary:(?P<body>.*)", re.S)
_I = re.compile(r"\bI:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*LUFS")
_PEAK = re.compile(r"True peak:\s*Peak:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*dBFS")


def parse_ebur128(stderr: str) -> Loudness:
    """Integrated loudness and true peak from the ebur128 summary (the last one printed)."""
    summary = _SUMMARY.findall(stderr)
    if not summary:
        raise ValueError("no ebur128 summary in ffmpeg output")
    body = summary[-1]
    i, peak = _I.search(body), _PEAK.search(body)
    if i is None or peak is None:
        raise ValueError("ebur128 summary lacks integrated loudness or true peak")
    return Loudness(float(i.group(1)), float(peak.group(1)))


def compliant(level: Loudness) -> bool:
    """-14 LUFS plus or minus 1 LU, true peak at most -1 dBTP."""
    return (
        abs(level.integrated - TARGET_I) <= TOLERANCE_LU + 1e-9
        and level.true_peak <= TARGET_TP + 1e-9
    )


def measure(path: str, capture: Capture = capture_stderr) -> Loudness:
    rc, err = capture(ebur128_argv(path))
    if rc != 0:
        raise LoudnessError(f"ffmpeg exited {rc} measuring {path}")
    return parse_ebur128(err)


def analysis_argv(inputs: list[str], pre: str | None = None) -> list[str]:
    """Pass one over ``inputs`` (ffmpeg input arguments, e.g. ``["-i", path]``)."""
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats", *inputs,
        "-map", "0:a:0", "-af", measure_filter(pre), "-f", "null", "-",
    ]  # fmt: skip


def analyse(
    inputs: list[str], pre: str | None = None, capture: Capture = capture_stderr
) -> dict[str, str]:
    rc, err = capture(analysis_argv(inputs, pre))
    if rc != 0:
        raise LoudnessError(f"ffmpeg exited {rc} in the loudness analysis pass")
    return parse_loudnorm_json(err)


def master(
    source: str, output: str, pre: str | None = None, capture: Capture = capture_stderr
) -> Loudness:
    """Clean (``pre``) and normalise ``source`` into ``output``; the measured result."""
    measured = analyse(["-i", source], pre, capture)
    argv = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", source,
        "-map", "0:v?", "-map", "0:a:0", "-c:v", "copy",
        "-af", normalize_filter(measured, pre),
        "-c:a", "aac", "-b:a", AAC_BITRATE, "-ar", str(SAMPLE_RATE), output,
    ]  # fmt: skip
    rc, _ = capture(argv)
    if rc != 0:
        raise LoudnessError(f"ffmpeg exited {rc} normalising {source}")
    return measure(output, capture)
