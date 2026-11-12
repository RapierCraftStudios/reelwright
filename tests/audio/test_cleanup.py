# SPDX-License-Identifier: AGPL-3.0-or-later
"""Voice cleanup chain: filter order and values, and real ffmpeg accepts it."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from reelwright.audio.cleanup import DEFAULT_MIC, MicProfile, voice_chain


def test_default_chain_order_and_values() -> None:
    assert DEFAULT_MIC == MicProfile()
    assert voice_chain() == (
        "highpass=f=80,afftdn=nf=-25,equalizer=f=250:t=q:w=1:g=-3,"
        "equalizer=f=4000:t=q:w=1:g=2,deesser=i=0.4"
    )


def test_custom_profile() -> None:
    mic = MicProfile(
        highpass_hz=100,
        denoise_nf_db=-30.5,
        mud_hz=300,
        mud_gain_db=-4.5,
        presence_hz=5000,
        presence_gain_db=3.0,
        deess_intensity=0.6,
    )
    names = [f.split("=", 1)[0] for f in voice_chain(mic).split(",")]
    assert names == ["highpass", "afftdn", "equalizer", "equalizer", "deesser"]
    assert voice_chain(mic) == (
        "highpass=f=100,afftdn=nf=-30.5,equalizer=f=300:t=q:w=1:g=-4.5,"
        "equalizer=f=5000:t=q:w=1:g=3,deesser=i=0.6"
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_ffmpeg_accepts_the_chain() -> None:
    done = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
            "-af", voice_chain(), "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
