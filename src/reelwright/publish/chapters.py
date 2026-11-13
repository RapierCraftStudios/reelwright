# SPDX-License-Identifier: AGPL-3.0-or-later
"""YouTube chapters from the beat start times in the final edit.

The rough cut puts a marker named ``"{beat_slug}:{line_no}"`` at the record frame where each
script line starts (``reelwright.edit.build``). A beat starts at its first marker. YouTube only
shows chapters when the first one is at 0:00, there are at least three, and each lasts at least
ten seconds, so anything else yields no chapters at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

MIN_CHAPTERS = 3
MIN_CHAPTER_S = 10


@dataclass(frozen=True)
class Chapter:
    start_s: float
    title: str


def _default_title(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title() or slug


def beat_starts(edit: Any) -> list[tuple[str, int]]:
    """(beat slug, first record frame) for each beat, in timeline order."""
    first: dict[str, int] = {}
    for marker in sorted(edit.markers, key=lambda m: m.frame):
        slug, sep, _line = str(marker.name).rpartition(":")
        if not sep or not slug:
            continue
        if slug not in first:
            first[slug] = int(marker.frame)
    return sorted(first.items(), key=lambda item: item[1])


def chapters(edit: Any, titles: Mapping[str, str] | None = None) -> list[Chapter]:
    """Chapters for the edit, or [] when YouTube would not show them."""
    titles = titles or {}
    starts = beat_starts(edit)
    if len(starts) < MIN_CHAPTERS:
        return []
    fps = Fraction(edit.rate.fps)
    frames = [0, *(frame for _, frame in starts[1:])]
    ends = [*frames[1:], int(edit.duration)]
    if any(
        Fraction(end - start) / fps < MIN_CHAPTER_S for start, end in zip(frames, ends, strict=True)
    ):
        return []
    return [
        Chapter(
            start_s=float(Fraction(frame) / fps), title=titles.get(slug) or _default_title(slug)
        )
        for (slug, _), frame in zip(starts, frames, strict=True)
    ]


def _stamp(seconds: float, hours: bool) -> str:
    total = int(seconds)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if hours else f"{m}:{s:02d}"


def format_chapters(items: Sequence[Chapter]) -> str:
    """One ``M:SS Title`` line per chapter (``H:MM:SS`` once the video passes an hour)."""
    hours = any(c.start_s >= 3600 for c in items)
    return "\n".join(f"{_stamp(c.start_s, hours)} {c.title}" for c in items)
