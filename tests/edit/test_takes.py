# SPDX-License-Identifier: AGPL-3.0-or-later
"""Best-take picking: three takes of a line, fumbles lose, prompter retake marks are honoured."""

from __future__ import annotations

from reelwright.db.models import TranscriptWord
from reelwright.edit import (
    RetakeMark,
    align_script,
    pick_best_takes,
    score_attempts,
    script_lines,
)
from reelwright.edit.takes import count_restarts
from reelwright.script import parse

SCRIPT = (
    "---\nformat: long\nworking_title: T\n---\n"
    "## beat: hook | intent: grab\nToday we test the new camera.\n"
)
LINE = "Today we test the new camera."
FUMBLE = "Today we test the the um new camera."


def say(text: str, at: float, word_s: float = 0.3, gap_s: float = 0.1) -> list[TranscriptWord]:
    out: list[TranscriptWord] = []
    for word in text.split():
        out.append(TranscriptWord(word=word, start=at, end=at + word_s, confidence=0.9))
        at += word_s + gap_s
    return out


def three_takes() -> list[TranscriptWord]:
    # take 1 fumbled at 0 s, take 2 clean at 5 s, take 3 clean at 10 s.
    return say(FUMBLE, 0.0) + say(LINE, 5.0) + say(LINE, 10.0)


def pick(marks: list[RetakeMark] | None = None) -> float:
    script = parse(SCRIPT).script
    takes = score_attempts(align_script(script, {1: three_takes()}), marks or [])
    chosen, missing = pick_best_takes(script_lines(script), takes)
    assert missing == []
    assert len(chosen) == 1
    return chosen[0].attempt.start


def test_aligns_three_attempts_and_scores_the_fumble_lower() -> None:
    script = parse(SCRIPT).script
    attempts = align_script(script, {1: three_takes()})
    assert [a.start for a in attempts] == [0.0, 5.0, 10.0]
    takes = score_attempts(attempts)
    fumbled, clean, _ = takes
    assert fumbled.scores.fillers == 1
    assert fumbled.scores.restarts == 1
    assert fumbled.scores.total < clean.scores.total


def test_picks_a_take_without_fumbles() -> None:
    assert pick() in (5.0, 10.0)


def test_honours_a_prompter_retake_mark() -> None:
    assert pick() == 10.0  # equal clean takes: the last read wins
    # The creator pressed retake right after take 3: take 2 is picked instead.
    assert pick([RetakeMark(asset_id=1, at=12.9)]) == 5.0
    # A mark on another asset or long after the take does not count.
    assert pick([RetakeMark(asset_id=2, at=12.9)]) == 10.0
    assert pick([RetakeMark(asset_id=1, at=30.0)]) == 10.0


def test_retake_mark_beats_a_better_score() -> None:
    marks = [RetakeMark(asset_id=1, at=7.6), RetakeMark(asset_id=1, at=12.9)]
    # Both clean takes marked: the fumbled unmarked take is the only eligible one.
    assert pick(marks) == 0.0


def test_all_takes_marked_falls_back_to_the_best_score() -> None:
    marks = [RetakeMark(asset_id=1, at=t) for t in (2.0, 7.6, 12.9)]
    assert pick(marks) == 10.0


def test_line_with_no_attempt_is_reported_missing() -> None:
    script = parse(SCRIPT + "## beat: cta | intent: sub\nSubscribe for more gear tests.\n").script
    takes = score_attempts(align_script(script, {1: three_takes()}))
    chosen, missing = pick_best_takes(script_lines(script), takes)
    assert [t.line.beat_slug for t in chosen] == ["hook"]
    assert [m.beat_slug for m in missing] == ["cta"]


def test_count_restarts() -> None:
    assert count_restarts("so we so we go".split()) == 1
    assert count_restarts("the the cat".split()) == 1
    assert count_restarts("a clean line".split()) == 0
