# SPDX-License-Identifier: AGPL-3.0-or-later
"""Score each attempt of a line as a take and pick the best take per line.

Scores (spike § Data model: Take): fluency (script match minus restarts), filler words,
energy (speech rate against the line's other takes, plus word confidence: only word-timed JSON
reaches the dev server, never audio) and prompter retake marks. A take the creator marked as a
retake on the prompter is never picked while an unmarked take of the line exists.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median

from pydantic import BaseModel, Field

from reelwright.edit.align import Attempt, ScriptLine, filler_runs, normalise

# A retake mark counts for the take it follows until the next attempt or this many seconds.
RETAKE_WINDOW_S = 3.0
RESTART_PENALTY = 0.15
FILLER_PENALTY = 0.1
ENERGY_WEIGHT = 0.1
CONFIDENCE_WEIGHT = 0.2


class RetakeMark(BaseModel):
    """A retake key press on the prompter: seconds from the start of an asset."""

    asset_id: int = Field(ge=0)
    at: float = Field(ge=0)


@dataclass(frozen=True)
class Scores:
    fluency: float
    fillers: int
    restarts: int
    energy: float
    confidence: float
    retake: bool
    total: float


@dataclass(frozen=True)
class Take:
    attempt: Attempt
    scores: Scores

    @property
    def line(self) -> ScriptLine:
        return self.attempt.line


def count_restarts(toks: Sequence[str]) -> int:
    """Immediate repeats of a word or a two-word phrase ("the the", "so we so we")."""
    count = 0
    for k in range(1, len(toks)):
        if toks[k] == toks[k - 1]:
            count += 1
        elif k >= 3 and toks[k - 1 : k + 1] == toks[k - 3 : k - 1]:
            count += 1
    return count


def _retake_marked(
    attempt: Attempt, others: Sequence[Attempt], marks: Sequence[RetakeMark]
) -> bool:
    after = [
        o.start
        for o in others
        if o.asset_id == attempt.asset_id and o.start >= attempt.end and o is not attempt
    ]
    until = min([attempt.end + RETAKE_WINDOW_S, *after])
    return any(m.asset_id == attempt.asset_id and attempt.start <= m.at < until for m in marks)


def _rate(attempt: Attempt) -> float:
    return len(attempt.words) / max(attempt.end - attempt.start, 0.1)


def score_attempts(attempts: Sequence[Attempt], marks: Sequence[RetakeMark] = ()) -> list[Take]:
    """Score every attempt; energy is relative to the other attempts of the same line."""
    rates: dict[tuple[str, int], list[float]] = {}
    for a in attempts:
        rates.setdefault(a.line.key, []).append(_rate(a))
    takes: list[Take] = []
    for a in attempts:
        toks = [normalise(w.word) for w in a.words]
        spoken = [t for t in toks if t]
        fillers = filler_runs(spoken, a.line.tokens)
        filler_idx = {k for first, last in fillers for k in range(first, last + 1)}
        restarts = count_restarts([t for k, t in enumerate(spoken) if k not in filler_idx])
        fluency = a.ratio - RESTART_PENALTY * restarts
        energy = min(_rate(a) / median(rates[a.line.key]), 1.2)
        confidence = sum(w.confidence for w in a.words) / len(a.words)
        retake = _retake_marked(a, attempts, marks)
        total = (
            fluency
            - FILLER_PENALTY * len(fillers)
            + ENERGY_WEIGHT * energy
            + CONFIDENCE_WEIGHT * confidence
        )
        takes.append(
            Take(
                attempt=a,
                scores=Scores(
                    fluency=round(fluency, 4),
                    fillers=len(fillers),
                    restarts=restarts,
                    energy=round(energy, 4),
                    confidence=round(confidence, 4),
                    retake=retake,
                    total=round(total, 4),
                ),
            )
        )
    return takes


def pick_best_takes(
    lines: Sequence[ScriptLine], takes: Sequence[Take]
) -> tuple[list[Take], list[ScriptLine]]:
    """The best take per line in script order, and the lines with no take at all.

    Retake-marked takes are excluded unless every take of the line is marked; ties go to the
    later take (the creator's last read).
    """
    by_line: dict[tuple[str, int], list[Take]] = {}
    for t in takes:
        by_line.setdefault(t.line.key, []).append(t)
    chosen: list[Take] = []
    missing: list[ScriptLine] = []
    for line in lines:
        options = by_line.get(line.key, [])
        if not options:
            missing.append(line)
            continue
        eligible = [t for t in options if not t.scores.retake] or options
        chosen.append(max(eligible, key=lambda t: (t.scores.total, t.attempt.start)))
    return chosen, missing
