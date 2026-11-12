# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transcribe job: word-timed speech of one media file, written as JSON for upload.

Run by the worker agent (reelwright.render.worker, transcribe executors) under its resource policy
(low priority, RAM cap). Engines are tried in order: WhisperX (word alignment), then faster-whisper
(word_timestamps). When every engine fails on CUDA (no driver, out of memory) the chain runs
again on the CPU with int8. Engines are imported only when tried, so this module imports without
them; install them on the worker host (`uv tool install` of reelwright plus whisperx or
faster-whisper).

    python workers/gpu/jobs/transcribe.py --input D:\\DJI\\a.MP4 --output rw-transcript-1.json \\
        --device cuda --compute-type float16
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from reelwright.ingest.transcript import words_from_segments

DEFAULT_MODEL = "large-v3"
CPU_COMPUTE_TYPE = "int8"

# An engine takes (input path, device, compute_type, model) and returns (segments, language).
Engine = Callable[[str, str, str, str], tuple[list[Any], str]]


def whisperx_engine(src: str, device: str, compute_type: str, model: str) -> tuple[list[Any], str]:
    import whisperx  # type: ignore[import-not-found]  # only on worker hosts

    audio = whisperx.load_audio(src)
    asr = whisperx.load_model(model, device, compute_type=compute_type)
    result = asr.transcribe(audio)
    language = str(result.get("language") or "")
    align_model, meta = whisperx.load_align_model(language_code=language, device=device)
    aligned = whisperx.align(
        result["segments"], align_model, meta, audio, device, return_char_alignments=False
    )
    return list(aligned["segments"]), language


def faster_whisper_engine(
    src: str, device: str, compute_type: str, model: str
) -> tuple[list[Any], str]:
    from faster_whisper import WhisperModel  # type: ignore[import-not-found]  # worker hosts only

    asr = WhisperModel(model, device=device, compute_type=compute_type)
    segments, info = asr.transcribe(src, word_timestamps=True)
    return list(segments), str(getattr(info, "language", "") or "")


DEFAULT_ENGINES: tuple[tuple[str, Engine], ...] = (
    ("whisperx", whisperx_engine),
    ("faster-whisper", faster_whisper_engine),
)


class TranscribeFailed(RuntimeError):
    """Every engine failed on every device tried."""


def transcribe(
    src: str,
    device: str = "cuda",
    compute_type: str = "float16",
    model: str = DEFAULT_MODEL,
    engines: Sequence[tuple[str, Engine]] = DEFAULT_ENGINES,
) -> dict[str, Any]:
    """Run the engine chain on device, then on the CPU if device was not already the CPU."""
    attempts = [(device, compute_type)]
    if device != "cpu":
        attempts.append(("cpu", CPU_COMPUTE_TYPE))
    errors: list[str] = []
    for dev, ctype in attempts:
        for name, engine in engines:
            try:
                segments, language = engine(src, dev, ctype, model)
            except (ImportError, RuntimeError, OSError, ValueError, MemoryError) as e:
                errors.append(f"{name}@{dev}: {type(e).__name__}: {e}")
                continue
            words = words_from_segments(segments)
            return {
                "engine": name,
                "model": model,
                "device": dev,
                "language": language,
                "words": [w.model_dump() for w in words],
            }
    raise TranscribeFailed("; ".join(errors) or "no engine configured")


def main(
    argv: Sequence[str] | None = None, engines: Sequence[tuple[str, Engine]] = DEFAULT_ENGINES
) -> int:
    parser = argparse.ArgumentParser(prog="transcribe", description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--device", default="cuda", choices=("cuda", "cpu"))
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)
    try:
        result = transcribe(args.input, args.device, args.compute_type, args.model, engines)
    except TranscribeFailed as e:
        print(f"transcribe failed: {e}", file=sys.stderr)
        return 1
    out = Path(args.output)
    part = out.with_name(out.name + ".part")
    part.write_text(json.dumps(result), encoding="utf-8")
    part.replace(out)
    print(f"transcribed {len(result['words'])} words with {result['engine']} on {result['device']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
