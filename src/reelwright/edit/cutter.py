# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cut a take to source spans: trim long silences to a natural gap, drop isolated fillers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from reelwright.db.models import TranscriptWord
from reelwright.edit.align import filler_runs, normalise

SILENCE_MAX_S = 0.6
NATURAL_GAP_S = 0.25
FILLER_PAUSE_S = 0.15
PAD_S = 0.08


@dataclass(frozen=True)
class Span:
    """Seconds ``start..end`` of an asset's source media."""

    asset_id: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def _isolated(words: Sequence[TranscriptWord], first: int, last: int) -> bool:
    before = first == 0 or words[first].start - words[first - 1].end >= FILLER_PAUSE_S
    after = last == len(words) - 1 or words[last + 1].start - words[last].end >= FILLER_PAUSE_S
    return before and after


def cut_take(
    asset_id: int, words: Sequence[TranscriptWord], scripted: Sequence[str] = ()
) -> list[Span]:
    """Source spans for one take's words.

    Fillers the script does not say are removed when a pause sits on both sides of them (a
    filler run into speech is left in: cutting it would clip a word). Pauses over
    ``SILENCE_MAX_S`` are cut down to ``NATURAL_GAP_S``; shorter pauses are kept as spoken.
    """
    toks = [normalise(w.word) for w in words]
    removed: set[int] = set()
    for first, last in filler_runs(toks, scripted):
        if _isolated(words, first, last):
            removed.update(range(first, last + 1))
    kept = [k for k in range(len(words)) if k not in removed]
    if not kept:
        return []
    half = NATURAL_GAP_S / 2
    spans: list[Span] = []
    start = max(words[kept[0]].start - PAD_S, 0.0)
    for prev, nxt in zip(kept, kept[1:], strict=False):
        p, q = words[prev], words[nxt]
        if nxt != prev + 1:  # a filler was removed between them
            close = p.end + min(half, max(words[prev + 1].start - p.end, 0.0))
            reopen = q.start - min(half, max(q.start - words[nxt - 1].end, 0.0))
        elif q.start - p.end > SILENCE_MAX_S:
            close, reopen = p.end + half, q.start - half
        else:
            continue
        spans.append(Span(asset_id, start, close))
        start = reopen
    spans.append(Span(asset_id, start, words[kept[-1]].end + PAD_S))
    merged: list[Span] = []
    for s in spans:
        if merged and s.start <= merged[-1].end:
            merged[-1] = Span(asset_id, merged[-1].start, max(merged[-1].end, s.end))
        else:
            merged.append(s)
    return merged
