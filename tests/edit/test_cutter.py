# SPDX-License-Identifier: AGPL-3.0-or-later
"""Silences over 600 ms trimmed to a natural gap; fillers removed only when isolated."""

from __future__ import annotations

import pytest

from reelwright.db.models import TranscriptWord
from reelwright.edit import Span, cut_take
from reelwright.edit.cutter import NATURAL_GAP_S, PAD_S


def w(word: str, start: float, end: float) -> TranscriptWord:
    return TranscriptWord(word=word, start=start, end=end, confidence=0.9)


def covered(spans: list[Span], t: float) -> bool:
    return any(s.start <= t <= s.end for s in spans)


def test_silence_over_600_ms_is_trimmed_to_a_natural_gap() -> None:
    spans = cut_take(1, [w("hello", 1.0, 1.3), w("there", 2.5, 2.8)])
    assert len(spans) == 2
    kept_pause = (spans[0].end - 1.3) + (2.5 - spans[1].start)
    assert kept_pause == pytest.approx(NATURAL_GAP_S)
    assert spans[0].start == pytest.approx(1.0 - PAD_S)
    assert spans[1].end == pytest.approx(2.8 + PAD_S)


def test_shorter_pause_is_kept() -> None:
    spans = cut_take(1, [w("hello", 1.0, 1.3), w("there", 1.8, 2.1)])
    assert spans == [Span(1, pytest.approx(1.0 - PAD_S), pytest.approx(2.1 + PAD_S))]  # type: ignore[arg-type]


def test_isolated_filler_is_removed() -> None:
    words = [w("so", 0.5, 0.8), w("um", 1.1, 1.4), w("yes", 1.7, 2.0)]
    spans = cut_take(1, words)
    assert len(spans) == 2
    assert not covered(spans, 1.25)
    assert covered(spans, 0.6) and covered(spans, 1.8)


def test_filler_phrase_is_removed_when_isolated() -> None:
    words = [w("so", 0.5, 0.8), w("you", 1.1, 1.2), w("know", 1.2, 1.4), w("yes", 1.7, 2.0)]
    assert not covered(cut_take(1, words), 1.3)


def test_filler_inside_speech_is_kept() -> None:
    words = [w("it", 0.5, 0.6), w("was", 0.62, 0.8), w("like", 0.82, 1.0), w("great", 1.02, 1.3)]
    spans = cut_take(1, words)
    assert len(spans) == 1 and covered(spans, 0.9)


def test_scripted_filler_word_is_kept_even_when_isolated() -> None:
    words = [w("i", 0.5, 0.6), w("like", 0.9, 1.1), w("it", 1.4, 1.5)]
    assert covered(cut_take(1, words, scripted=("i", "like", "it")), 1.0)
    assert not covered(cut_take(1, words), 1.0)


def test_head_padding_is_clamped_at_zero() -> None:
    assert cut_take(1, [w("hi", 0.02, 0.3)])[0].start == 0.0


def test_only_fillers_cut_to_nothing() -> None:
    assert cut_take(1, [w("um", 0.5, 0.8)]) == []
