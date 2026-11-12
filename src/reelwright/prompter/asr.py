# SPDX-License-Identifier: AGPL-3.0-or-later
"""Streaming speech recognition for the teleprompter (spike § Stack: a small streaming Whisper).

Audio arrives as PCM16 little-endian mono at 16 kHz. `feed` returns only newly committed words.

- `FasterWhisperStreamingAsr` re-transcribes a short rolling buffer every hop and commits the words
  two consecutive hypotheses agree on, then trims the buffer at the last committed word. It runs
  on the dev server's CPU (int8). faster-whisper is imported only when this engine is built, the
  same way the GPU transcribe job does it (workers/gpu/jobs/transcribe.py). Install it on the host
  (`uv pip install faster-whisper`); without it the engine raises `AsrUnavailable`.
- `ReadAlongMockAsr` needs no model: it "reads" the script aloud at a fixed rate of audio, so dev
  and CI exercise the real WebSocket and aligner path.

`REELWRIGHT_PROMPTER_ASR` picks the engine: `faster-whisper` (default) or `mock`. Audio is only
ever held in memory.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from functools import lru_cache
from typing import Any, Protocol

from reelwright.prompter.align import normalize

SAMPLE_RATE = 16_000
BYTES_PER_SECOND = SAMPLE_RATE * 2
ENGINE_ENV = "REELWRIGHT_PROMPTER_ASR"
MODEL_ENV = "REELWRIGHT_PROMPTER_ASR_MODEL"
MOCK_RATE_ENV = "REELWRIGHT_PROMPTER_MOCK_MS_PER_WORD"
DEFAULT_MODEL = "tiny.en"

# A transcriber takes PCM16 audio and returns (word, end seconds from the start of that audio).
Transcriber = Callable[[bytes], list[tuple[str, float]]]


class AsrUnavailable(RuntimeError):
    """The configured speech engine can't run here (not installed, unknown name)."""


class StreamingAsr(Protocol):
    def feed(self, pcm: bytes) -> list[str]:
        """Add audio; return the words committed since the last call."""
        ...

    def reset(self, word_index: int = 0) -> None:
        """Drop buffered audio. `word_index` is where the reader is; a real recogniser ignores it."""
        ...


class FasterWhisperStreamingAsr:
    def __init__(
        self,
        transcriber: Transcriber,
        *,
        hop_s: float = 0.25,
        window_s: float = 3.0,
        max_s: float = 6.0,
    ) -> None:
        self._transcribe = transcriber
        self._hop = _even(hop_s)
        self._window = _even(window_s)
        self._max = _even(max_s)
        self.reset()

    def reset(self, word_index: int = 0) -> None:
        self._buffer = bytearray()
        self._since = 0
        self._prev: list[tuple[str, float]] = []
        self._committed = 0  # hypothesis words at the start of the buffer already committed

    def feed(self, pcm: bytes) -> list[str]:
        self._buffer += pcm
        self._since += len(pcm)
        if self._since < self._hop:
            return []
        self._since = 0
        hyp = self._transcribe(bytes(self._buffer))
        agreed = 0
        while (
            agreed < min(len(hyp), len(self._prev))
            and normalize(hyp[agreed][0]) == normalize(self._prev[agreed][0])
        ):
            agreed += 1
        new = [word for word, _ in hyp[self._committed : agreed]]
        self._committed = max(self._committed, agreed)
        self._prev = hyp
        if len(self._buffer) > self._window and self._committed:
            self._trim(_even(hyp[self._committed - 1][1]))
        if len(self._buffer) > self._max:
            # No agreement for too long (noise, crosstalk): start over on recent audio.
            del self._buffer[: len(self._buffer) - self._window]
            self._prev, self._committed = [], 0
        return new

    def _trim(self, cut: int) -> None:
        cut = max(0, min(cut, len(self._buffer)))
        del self._buffer[:cut]
        shift = cut / BYTES_PER_SECOND
        self._prev = [(w, end - shift) for w, end in self._prev[self._committed :]]
        self._committed = 0


class ReadAlongMockAsr:
    """Emits the next script word for every `ms_per_word` of audio received."""

    def __init__(self, words: Sequence[str], ms_per_word: int = 300) -> None:
        if ms_per_word <= 0:
            raise ValueError("ms_per_word must be positive")
        self.words = list(words)
        self._per_word = _even(ms_per_word / 1000)
        self.reset()

    def reset(self, word_index: int = 0) -> None:
        self._next = max(0, word_index)
        self._bytes = 0

    def feed(self, pcm: bytes) -> list[str]:
        self._bytes += len(pcm)
        out: list[str] = []
        while self._bytes >= self._per_word and self._next < len(self.words):
            self._bytes -= self._per_word
            out.append(self.words[self._next])
            self._next += 1
        return out


def _even(seconds: float) -> int:
    """Seconds of audio -> a byte count on a sample boundary."""
    n = int(seconds * BYTES_PER_SECOND)
    return n - n % 2


@lru_cache(maxsize=2)
def _whisper_model(name: str) -> Any:
    try:
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]  # host install
    except ImportError as exc:
        raise AsrUnavailable("faster-whisper is not installed on this host") from exc
    return WhisperModel(name, device="cpu", compute_type="int8")


def faster_whisper_transcriber(model_name: str = DEFAULT_MODEL) -> Transcriber:
    """Load (once per process) a small faster-whisper model and wrap it as a Transcriber."""
    model = _whisper_model(model_name)
    try:
        import numpy as np  # type: ignore[import-not-found]  # comes with faster-whisper
    except ImportError as exc:
        raise AsrUnavailable("numpy is not installed on this host") from exc
    language = "en" if model_name.endswith(".en") else None

    def transcribe(pcm: bytes) -> list[tuple[str, float]]:
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = model.transcribe(
            audio,
            language=language,
            beam_size=1,
            word_timestamps=True,
            condition_on_previous_text=False,
            vad_filter=False,
        )
        return [
            (str(w.word).strip(), float(w.end))
            for segment in segments
            for w in segment.words or ()
            if str(w.word).strip()
        ]

    return transcribe


def make_asr(words: Sequence[str], env: Mapping[str, str] | None = None) -> StreamingAsr:
    """The engine named by REELWRIGHT_PROMPTER_ASR. Slow (model load): call off the event loop."""
    env = os.environ if env is None else env
    kind = env.get(ENGINE_ENV, "faster-whisper").strip() or "faster-whisper"
    if kind == "mock":
        return ReadAlongMockAsr(words, ms_per_word=int(env.get(MOCK_RATE_ENV, "300")))
    if kind == "faster-whisper":
        model = env.get(MODEL_ENV, DEFAULT_MODEL).strip() or DEFAULT_MODEL
        return FasterWhisperStreamingAsr(faster_whisper_transcriber(model))
    raise AsrUnavailable(f"unknown {ENGINE_ENV} value {kind!r}; use faster-whisper or mock")
