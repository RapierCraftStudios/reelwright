# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build the rough cut as a ``spectracut.edit.Edit`` and export it as OTIO.

One video track and one linked audio track of clips cut from the source assets, with a marker
per script line (beat slug and line number), so the edit points back to the beats. The OTIO is
the hand-off to Resolve or Kdenlive (spike § Guardrails: every automated edit is reversible).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from spectracut.edit import Clip, Edit, FrameRate, Marker, Track, to_otio

from reelwright.edit.cutter import Span


@dataclass(frozen=True)
class CutLine:
    """The spans kept for one script line's best take."""

    beat_slug: str
    line_no: int
    text: str
    spans: Sequence[Span]


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_")
    return slug[:120] or "edit"


def _frames(rate: FrameRate, seconds: float) -> int:
    return int(rate.frames(Fraction(seconds).limit_denominator(1_000_000)))


def build_edit(
    name: str, lines: Sequence[CutLine], media: Mapping[int, str], rate: FrameRate
) -> Edit:
    """The rough cut; ``media`` maps asset id to its source path. Sub-frame spans are dropped."""
    video: list[Clip] = []
    audio: list[Clip] = []
    markers: list[Marker] = []
    at = 0
    for line in lines:
        line_start = at
        for span in line.spans:
            source_in = _frames(rate, span.start)
            source_out = _frames(rate, span.end)
            if source_out <= source_in:
                continue
            n = len(video) + 1
            label = f"{line.beat_slug} L{line.line_no}"
            common = {
                "name": label,
                "media_ref": media[span.asset_id],
                "source_in": source_in,
                "source_out": source_out,
            }
            video.append(Clip(id=f"v{n:04d}", **common))
            audio.append(Clip(id=f"a{n:04d}", link=f"v{n:04d}", **common))
            at += source_out - source_in
        if at > line_start:
            markers.append(
                Marker(
                    name=f"{line.beat_slug}:{line.line_no}",
                    frame=line_start,
                    note=line.text,
                    color="GREEN",
                )
            )
    return Edit(
        id=_slug(name),
        name=name,
        rate=rate,
        tracks=[
            Track(id="video", kind="video", items=video),
            Track(id="audio", kind="audio", items=audio),
        ],
        markers=markers,
    )


def edit_to_otio_json(edit: Edit) -> str:
    """The edit as an OTIO JSON document (what a ``.otio`` file holds)."""
    return str(to_otio(edit).to_json_string())
