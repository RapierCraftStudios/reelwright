# SPDX-License-Identifier: AGPL-3.0-or-later
"""Align word-timed transcripts to script lines: every spoken attempt of every line.

A creator reads each line one or more times. For each spoken line of the script this finds the
spans of a transcript whose words best match the line (a token-level fuzzy match), so repeated
attempts become candidate takes (spike § Data model: Take).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from reelwright.db.models import TranscriptWord
from reelwright.script.format import Script

MIN_MATCH_RATIO = 0.6
ANCHOR_TOKENS = 3

# Words and phrases that are filler when they are not in the script line itself.
FILLERS = frozenset({"um", "umm", "uh", "uhh", "er", "erm", "ah", "hmm", "mm", "like"})
FILLER_PHRASES: tuple[tuple[str, ...], ...] = (("you", "know"), ("i", "mean"))

_NON_WORD = re.compile(r"[^\w']+")


def normalise(word: str) -> str:
    """Lowercase, punctuation stripped (apostrophes kept): ``"Hello,"`` → ``"hello"``."""
    return _NON_WORD.sub("", word.lower()).strip("'")


def tokens(text: str) -> list[str]:
    return [t for t in (normalise(w) for w in text.split()) if t]


@dataclass(frozen=True)
class AlignLine:
    """One spoken line of the script, pointing back to its beat."""

    beat_slug: str
    line_no: int
    text: str
    tokens: tuple[str, ...]

    @property
    def key(self) -> tuple[str, int]:
        return (self.beat_slug, self.line_no)


@dataclass(frozen=True)
class Attempt:
    """One reading of a script line in one asset: words ``first..last`` inclusive."""

    line: AlignLine
    asset_id: int
    first: int
    last: int
    start: float
    end: float
    ratio: float
    words: tuple[TranscriptWord, ...]


def script_lines(script: Script) -> list[AlignLine]:
    """The spoken lines of a script in order; lines with no words are skipped."""
    out: list[AlignLine] = []
    for beat in script.beats:
        for line in beat.spoken:
            toks = tuple(tokens(line.text))
            if toks:
                out.append(AlignLine(beat.slug, line.line_no, line.text, toks))
    return out


def filler_runs(toks: Sequence[str], scripted: Sequence[str] = ()) -> list[tuple[int, int]]:
    """``(first, last)`` index runs of filler words or phrases that the script does not say."""
    allowed = set(scripted)
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(toks):
        phrase = next(
            (p for p in FILLER_PHRASES if tuple(toks[i : i + len(p)]) == p and p[0] not in allowed),
            None,
        )
        if phrase is not None:
            runs.append((i, i + len(phrase) - 1))
            i += len(phrase)
            continue
        if toks[i] in FILLERS and toks[i] not in allowed:
            runs.append((i, i))
        i += 1
    return runs


def find_attempts(
    line: AlignLine,
    asset_id: int,
    words: Sequence[TranscriptWord],
    min_ratio: float = MIN_MATCH_RATIO,
) -> list[Attempt]:
    """Non-overlapping attempts of ``line`` in ``words``, best matches first claimed."""
    stream = [normalise(w.word) for w in words]
    target = list(line.tokens)
    n = len(target)
    slack = max(2, n // 2)
    heads = set(target[:ANCHOR_TOKENS])
    tails = set(target[-ANCHOR_TOKENS:])
    matcher = SequenceMatcher(None, autojunk=False)
    matcher.set_seq2(target)
    candidates: list[tuple[float, int, int]] = []
    for i, tok in enumerate(stream):
        if tok not in heads:
            continue
        best: tuple[float, int, int] | None = None
        for length in range(max(1, n - slack), n + slack + 1):
            j = i + length - 1
            if j >= len(stream):
                break
            if stream[j] not in tails:
                continue
            matcher.set_seq1(stream[i : j + 1])
            ratio = matcher.ratio()
            if ratio >= min_ratio and (best is None or ratio > best[0]):
                best = (ratio, i, j)
        if best is not None:
            candidates.append(best)
    taken: list[tuple[int, int]] = []
    out: list[Attempt] = []
    for ratio, i, j in sorted(candidates, key=lambda c: (-c[0], c[1])):
        if any(i <= b and a <= j for a, b in taken):
            continue
        taken.append((i, j))
        out.append(
            Attempt(
                line=line,
                asset_id=asset_id,
                first=i,
                last=j,
                start=words[i].start,
                end=words[j].end,
                ratio=ratio,
                words=tuple(words[i : j + 1]),
            )
        )
    return sorted(out, key=lambda a: a.start)


def align_script(
    script: Script, transcripts: Mapping[int, Sequence[TranscriptWord]]
) -> list[Attempt]:
    """Every attempt of every spoken line across the assets' transcripts."""
    lines = script_lines(script)
    out: list[Attempt] = []
    for asset_id, words in transcripts.items():
        for line in lines:
            out.extend(find_attempts(line, asset_id, words))
    return sorted(out, key=lambda a: (a.asset_id, a.start))
