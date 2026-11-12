# SPDX-License-Identifier: AGPL-3.0-or-later
"""Voice cleanup for the creator's mic: rumble cut, denoise, EQ, de-ess, as one ffmpeg filter chain.

The chain runs once over the joined final audio in the concat job, before loudness normalisation,
so segment seams are never processed separately. The defaults are broadcast-voice values. The
creator's exact mic is not recorded anywhere yet, so tune ``MicProfile`` rather than the code.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MicProfile:
    """Filter settings for one microphone."""

    highpass_hz: int = 80  # rumble, desk thumps, HVAC
    denoise_nf_db: float = -50.0  # afftdn noise floor; higher values eat quiet speech
    mud_hz: int = 250
    mud_gain_db: float = -3.0
    presence_hz: int = 4000
    presence_gain_db: float = 2.0
    deess_intensity: float = 0.4  # deesser i, 0..1


DEFAULT_MIC = MicProfile()


def _num(value: float) -> str:
    return f"{value:g}"


def voice_chain(profile: MicProfile = DEFAULT_MIC) -> str:
    """The ffmpeg audio filter chain that cleans a voice recorded on ``profile``."""
    p = profile
    return ",".join(
        [
            f"highpass=f={p.highpass_hz}",
            f"afftdn=nf={_num(p.denoise_nf_db)}",
            f"equalizer=f={p.mud_hz}:t=q:w=1:g={_num(p.mud_gain_db)}",
            f"equalizer=f={p.presence_hz}:t=q:w=1:g={_num(p.presence_gain_db)}",
            f"deesser=i={_num(p.deess_intensity)}",
        ]
    )
