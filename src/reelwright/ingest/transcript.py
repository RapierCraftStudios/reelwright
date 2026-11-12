# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transcripts: word normalisation, the transcribe job, and storing what the worker uploads.

A transcribe job runs where the footage is (a GPU worker picked by the picker, or a CPU worker
when no GPU worker could run it). The job writes word-timed JSON (workers/gpu/jobs/transcribe.py)
and the worker uploads it to /api/assets/{id}/transcript. Only that JSON reaches the dev server,
behind the free-disk floor; audio and footage never do.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from sqlmodel import Session, select

from reelwright.db.models import Asset, Transcript, TranscriptRead, TranscriptUpload, TranscriptWord
from reelwright.ingest import proxy
from reelwright.render import queue
from reelwright.render.queue import JobKind

# large-v3 in float16 plus alignment fits both cards (12 GB 4070 Ti, 8 GB 3070 Laptop).
TRANSCRIBE_MIN_VRAM_MB = 6144
MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024
TRANSCRIPT_CONTENT_TYPE = "application/json"


def _field(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _words_of(segment: Any) -> Iterable[Any]:
    words = _field(segment, "words")
    return words or ()


def words_from_segments(segments: Iterable[Any]) -> list[TranscriptWord]:
    """WhisperX (word, start, end, score) or faster-whisper (word, start, end, probability) words.

    Segments and words may be dicts or objects. Words without a start or end (WhisperX leaves
    some numerals unaligned) are dropped; blank words too.
    """
    out: list[TranscriptWord] = []
    for segment in segments:
        for w in _words_of(segment):
            text = str(_field(w, "word") or "").strip()
            start, end = _field(w, "start"), _field(w, "end")
            if not text or start is None or end is None:
                continue
            conf = _field(w, "score")
            if conf is None:
                conf = _field(w, "probability")
            confidence = min(max(float(conf if conf is not None else 0.0), 0.0), 1.0)
            start_f = max(float(start), 0.0)
            out.append(
                TranscriptWord(
                    word=text, start=start_f, end=max(float(end), start_f), confidence=confidence
                )
            )
    return out


def transcript_name(asset_id: int) -> str:
    return f"rw-transcript-{asset_id}.json"


def enqueue_transcribe(session: Session, asset: Asset) -> int:
    """Queue a transcribe job (cuda cap). The picker chooses the worker, never a host."""
    assert asset.id is not None
    payload = {
        "asset_id": asset.id,
        "input": asset.path,
        "output": transcript_name(asset.id),
        "upload": f"/api/assets/{asset.id}/transcript",
        "content_type": TRANSCRIPT_CONTENT_TYPE,
    }
    job = queue.enqueue(session, JobKind.transcribe, payload, min_vram_mb=TRANSCRIBE_MIN_VRAM_MB)
    assert job.id is not None
    asset.transcript_job_id = job.id
    session.add(asset)
    session.commit()
    session.refresh(asset)
    return job.id


def check_free_floor(incoming_bytes: int, budget: proxy.ProxyBudget | None = None) -> None:
    """Raise DiskBudgetExceeded when storing incoming_bytes would drop free disk below the floor."""
    budget = budget or proxy.ProxyBudget.from_env()
    free = budget.free_bytes()
    if free - incoming_bytes < budget.floor_bytes:
        raise proxy.DiskBudgetExceeded(
            f"free disk below floor: {free - incoming_bytes} < {budget.floor_bytes} bytes"
        )


def store_transcript(
    session: Session,
    asset: Asset,
    upload: TranscriptUpload,
    size_bytes: int,
    budget: proxy.ProxyBudget | None = None,
) -> Transcript:
    """Replace the asset's transcript with the upload, after the free-disk floor check."""
    assert asset.id is not None
    check_free_floor(size_bytes, budget)
    for old in session.exec(select(Transcript).where(Transcript.asset_id == asset.id)).all():
        session.delete(old)
    row = Transcript(
        asset_id=asset.id,
        engine=upload.engine,
        model=upload.model,
        device=upload.device,
        language=upload.language,
        text=" ".join(w.word for w in upload.words),
        words=json.dumps([w.model_dump() for w in upload.words]),
        word_count=len(upload.words),
    )
    session.add(row)
    session.flush()
    asset.transcript_id = row.id
    session.add(asset)
    session.commit()
    session.refresh(row)
    session.refresh(asset)
    return row


def transcript_to_read(row: Transcript) -> TranscriptRead:
    assert row.id is not None
    return TranscriptRead(
        id=row.id,
        asset_id=row.asset_id,
        engine=row.engine,
        model=row.model,
        device=row.device,
        language=row.language,
        text=row.text,
        words=[TranscriptWord.model_validate(w) for w in json.loads(row.words)],
        created_at=row.created_at,
    )
