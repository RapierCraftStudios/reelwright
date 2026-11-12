# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fuzzy alignment of a live word stream to the script's spoken lines (spike § Runtime topology).

The aligner holds a cursor: the next script word it expects to hear. Each heard word is matched
against a forward window (where the reader is going) and a short backward window (a line being
read again). It is deterministic and pure, so recorded ASR word streams replay exactly.

- In sync (right after a seek or a match), the next expected word alone moves the cursor.
- Out of sync (an ad-lib or a mishearing broke the run), it needs 3 of the last 4 heard words to
  match in order ahead of the cursor. Ad-libbed words that match nothing just hold the position.
- A repeated line needs 4 of the last 5 heard words to match in order within the last few lines.
- When a run matches both ahead and behind (identical lines), the nearest match at or after the
  cursor wins.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache

from reelwright.script.format import Script

WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)*")
ADLIB_RE = re.compile(r"\{[^{}]*\}")

FORWARD_TOKENS = 24
BACK_LINES = 3
FORWARD_RUN = 4
FORWARD_MIN = 3
BACK_RUN = 5
BACK_MIN = 4
FUZZY_RATIO = 0.75


@dataclass(frozen=True)
class ScriptLine:
    """One spoken line as the prompter shows it: its source line number and its text."""

    line_no: int
    text: str


@dataclass(frozen=True)
class Position:
    line_no: int
    word: int
    confidence: float


def spoken_lines(script: Script) -> list[ScriptLine]:
    """Spoken, non-blank lines in reading order: the same list the prompter view renders."""
    return [
        ScriptLine(line.line_no, line.text)
        for beat in script.beats
        for line in beat.lines
        if line.kind == "spoken" and line.text.strip()
    ]


def normalize(text: str) -> list[str]:
    """Lowercase word tokens without punctuation or apostrophes; `{ad-lib}` prompts are dropped."""
    return [w.replace("'", "") for w in WORD_RE.findall(ADLIB_RE.sub(" ", text).lower())]


@lru_cache(maxsize=65536)
def similar(heard: str, expected: str) -> bool:
    """Exact, or close enough for a longer word the recogniser slightly misheard."""
    if heard == expected:
        return True
    if min(len(heard), len(expected)) < 4:
        return False
    return SequenceMatcher(None, heard, expected).ratio() >= FUZZY_RATIO


class ScriptAligner:
    def __init__(self, lines: Sequence[ScriptLine]) -> None:
        if not lines:
            raise ValueError("script has no spoken lines")
        self.lines = list(lines)
        self.words: list[str] = []
        self._refs: list[tuple[int, int]] = []  # token -> (line index, word index in line)
        self._line_start: list[int] = []
        for li, line in enumerate(self.lines):
            self._line_start.append(len(self.words))
            for wi, word in enumerate(normalize(line.text)):
                self.words.append(word)
                self._refs.append((li, wi))
        self._index = {line.line_no: li for li, line in enumerate(self.lines)}
        self._recent: deque[str] = deque(maxlen=max(FORWARD_RUN, BACK_RUN))
        self.cursor = 0
        self._in_sync = True
        self.position: Position | None = None

    def word_index(self, line_no: int) -> int:
        """Index in `words` of the first word of a spoken line."""
        if line_no not in self._index:
            raise ValueError(f"line {line_no} is not a spoken line")
        return self._line_start[self._index[line_no]]

    def seek(self, line_no: int) -> None:
        """Put the cursor at the start of a spoken line (manual move, or resuming voice follow)."""
        self.cursor = self.word_index(line_no)
        self._recent.clear()
        self._in_sync = True
        self.position = None

    def feed(self, heard: Iterable[str]) -> Position | None:
        """Feed newly recognised words; return the new position if the cursor moved."""
        moved: Position | None = None
        for chunk in heard:
            for word in normalize(chunk):
                found = self._step(word)
                if found is not None:
                    moved = found
        return moved

    def _step(self, word: str) -> Position | None:
        self._recent.append(word)
        recent = list(self._recent)
        if (
            self._in_sync
            and self.cursor < len(self.words)
            and similar(word, self.words[self.cursor])
        ):
            return self._move(self.cursor, 1.0)
        forward = self._best(recent, range(self.cursor, len(self.words)), FORWARD_RUN, FORWARD_MIN)
        if forward is not None and forward[0] < self.cursor + FORWARD_TOKENS:
            return self._move(*forward)
        back_line = max(0, self._line_of(self.cursor - 1) - BACK_LINES)
        backward = self._best(
            recent, reversed(range(self._line_start[back_line], self.cursor)), BACK_RUN, BACK_MIN
        )
        if backward is not None:
            return self._move(*backward)
        self._in_sync = False
        return None

    def _line_of(self, token: int) -> int:
        if token < 0:
            return 0
        return self._refs[min(token, len(self._refs) - 1)][0]

    def _best(
        self, recent: list[str], candidates: Iterable[int], run: int, needed: int
    ) -> tuple[int, float] | None:
        """First candidate (in the given order) with the highest in-order match of the run."""
        heard = recent[-run:]
        if len(heard) < needed:
            return None
        best: tuple[int, float] | None = None
        best_score = needed - 1
        for j in candidates:
            if j >= self.cursor + FORWARD_TOKENS:
                break
            if not similar(heard[-1], self.words[j]):
                continue
            score = 1 + _lcs(heard[:-1], self.words[max(0, j - run) : j])
            if score > best_score:
                best, best_score = (j, score / len(heard)), score
        return best

    def _move(self, token: int, confidence: float) -> Position:
        self.cursor = token + 1
        self._in_sync = True
        li, wi = self._refs[token]
        self.position = Position(self.lines[li].line_no, wi, round(confidence, 3))
        return self.position


def _lcs(heard: Sequence[str], expected: Sequence[str]) -> int:
    """Longest in-order (fuzzy) common subsequence: tolerates a dropped or inserted word."""
    prev = [0] * (len(expected) + 1)
    for h in heard:
        cur = [0]
        for k, e in enumerate(expected):
            cur.append(prev[k] + 1 if similar(h, e) else max(prev[k + 1], cur[k]))
        prev = cur
    return prev[-1]
