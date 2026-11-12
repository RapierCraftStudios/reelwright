# SPDX-License-Identifier: AGPL-3.0-or-later
"""Streaming ASR engines for voice follow (issue #7): mock timing, agreement commit, selection."""

from __future__ import annotations

import sys

import pytest

from reelwright.prompter import asr
from reelwright.prompter.asr import (
    BYTES_PER_SECOND,
    AsrUnavailable,
    FasterWhisperStreamingAsr,
    ReadAlongMockAsr,
    make_asr,
)

WORDS = ["three", "weeks", "ago", "i", "deleted", "every"]


def _ms(ms: int) -> bytes:
    return bytes(BYTES_PER_SECOND * ms // 1000)


def test_mock_emits_one_word_per_interval_of_audio() -> None:
    mock = ReadAlongMockAsr(WORDS, ms_per_word=100)
    assert mock.feed(_ms(50)) == []
    assert mock.feed(_ms(50)) == ["three"]
    assert mock.feed(_ms(250)) == ["weeks", "ago"]
    assert mock.feed(_ms(50)) == ["i"]  # the 50 ms left over from the last call counts
    mock.reset(4)
    assert mock.feed(_ms(100)) == ["deleted"]
    assert mock.feed(_ms(1000)) == ["every"]  # stops at the end of the script
    with pytest.raises(ValueError):
        ReadAlongMockAsr(WORDS, ms_per_word=0)


class FakeTranscriber:
    """Returns a scripted hypothesis per call and records how much audio it was given."""

    def __init__(self, hypotheses: list[list[tuple[str, float]]]) -> None:
        self.hypotheses = hypotheses
        self.sizes: list[int] = []

    def __call__(self, pcm: bytes) -> list[tuple[str, float]]:
        self.sizes.append(len(pcm))
        return self.hypotheses[min(len(self.sizes) - 1, len(self.hypotheses) - 1)]


def test_words_commit_once_two_hypotheses_agree() -> None:
    fake = FakeTranscriber(
        [
            [("Three", 0.4)],
            [("Three", 0.4), ("weaks", 0.7)],
            [("three,", 0.4), ("weeks", 0.7), ("ago", 0.9)],
            [("three", 0.4), ("weeks", 0.7), ("ago", 0.9), ("I", 1.0)],
        ]
    )
    stream = FasterWhisperStreamingAsr(fake, hop_s=0.25, window_s=3.0)
    assert stream.feed(_ms(100)) == []  # under one hop: not transcribed
    assert fake.sizes == []
    assert stream.feed(_ms(150)) == []  # first hypothesis: nothing to agree with
    assert stream.feed(_ms(250)) == ["Three"]
    assert stream.feed(_ms(250)) == []  # "weaks" became "weeks": not agreed yet
    assert stream.feed(_ms(250)) == ["weeks", "ago"]  # committed words are never repeated
    assert fake.sizes == [BYTES_PER_SECOND * ms // 1000 for ms in (250, 500, 750, 1000)]


def test_buffer_is_trimmed_at_the_last_committed_word() -> None:
    fake = FakeTranscriber([[("a", 0.5), ("b", 1.5)]])
    stream = FasterWhisperStreamingAsr(fake, hop_s=0.25, window_s=1.0, max_s=10.0)
    stream.feed(_ms(1500))
    assert stream.feed(_ms(500)) == ["a", "b"]  # 2 s buffered, over the 1 s window: cut at 1.5 s
    stream.feed(_ms(250))
    assert fake.sizes[-1] == len(_ms(750))  # the 0.5 s after "b" plus the new 0.25 s


def test_buffer_without_agreement_is_capped() -> None:
    hyps = [[(f"w{i}", 0.5)] for i in range(40)]  # never agrees
    fake = FakeTranscriber(hyps)
    stream = FasterWhisperStreamingAsr(fake, hop_s=0.25, window_s=1.0, max_s=2.0)
    for _ in range(20):
        assert stream.feed(_ms(250)) == []
    assert max(fake.sizes) <= BYTES_PER_SECOND * 2 + BYTES_PER_SECOND // 4


def test_make_asr_picks_the_engine_from_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = make_asr(
        WORDS, {"REELWRIGHT_PROMPTER_ASR": "mock", "REELWRIGHT_PROMPTER_MOCK_MS_PER_WORD": "50"}
    )
    assert isinstance(mock, ReadAlongMockAsr)
    assert mock.feed(_ms(100)) == ["three", "weeks"]
    with pytest.raises(AsrUnavailable):
        make_asr(WORDS, {"REELWRIGHT_PROMPTER_ASR": "cloud"})
    loaded: list[str] = []

    def fake_transcriber(name: str) -> asr.Transcriber:
        loaded.append(name)
        return lambda pcm: []

    monkeypatch.setattr(asr, "faster_whisper_transcriber", fake_transcriber)
    assert isinstance(make_asr(WORDS, {}), FasterWhisperStreamingAsr)
    assert isinstance(
        make_asr(WORDS, {"REELWRIGHT_PROMPTER_ASR_MODEL": "base.en"}), FasterWhisperStreamingAsr
    )
    assert loaded == ["tiny.en", "base.en"]


def test_missing_faster_whisper_is_asr_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    asr._whisper_model.cache_clear()
    with pytest.raises(AsrUnavailable):
        make_asr(WORDS, {"REELWRIGHT_PROMPTER_ASR": "faster-whisper"})
