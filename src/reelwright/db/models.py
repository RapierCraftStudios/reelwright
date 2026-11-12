# SPDX-License-Identifier: AGPL-3.0-or-later
"""SQLModel tables and API schemas (spike § Data model)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import field_validator, model_validator
from sqlalchemy import Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class ProjectFormat(StrEnum):
    long = "long"
    short = "short"
    vlog = "vlog"


def _now() -> datetime:
    return datetime.now(UTC)


class ProjectBase(SQLModel):
    title: str = Field(min_length=1, max_length=200)
    format: ProjectFormat = ProjectFormat.long
    target_publish_date: date | None = None


class Project(ProjectBase, table=True):
    """One video."""

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True, max_length=220)
    status: str = Field(default="draft", max_length=32)
    created_at: datetime = Field(default_factory=_now)


class ProjectCreate(ProjectBase):
    pass


class ProjectRead(ProjectBase):
    id: int
    slug: str
    status: str
    created_at: datetime


class ScriptVersion(SQLModel, table=True):
    """One saved version of a project's script markdown (spike § Data model: versioned script)."""

    __table_args__ = (UniqueConstraint("project_id", "version"),)

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    version: int
    source: str = Field(sa_type=Text)
    locked_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)


class ScriptPut(SQLModel):
    source: str = Field(max_length=500_000)


class ScriptVersionRead(SQLModel):
    id: int
    project_id: int
    version: int
    source: str
    locked_at: datetime | None
    created_at: datetime


# --- Teleprompter take marks (spike § Route/API map: /api/prompter/:project) --------------------


class TakeMarkKind(StrEnum):
    retake = "retake"


class TakeMark(SQLModel, table=True):
    """A marker pressed on the teleprompter while recording; points back to a locked script line.

    `marked_at` is the device's wall clock so the best-take cut (#11) can match it to footage time;
    `offset_ms` is the time since the reader pressed start.
    """

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    script_version_id: int = Field(foreign_key="scriptversion.id", index=True)
    kind: str = Field(default=TakeMarkKind.retake.value, max_length=16)
    beat_slug: str = Field(max_length=64)
    line_no: int
    marked_at: datetime
    offset_ms: int
    created_at: datetime = Field(default_factory=_now)


class TakeMarkCreate(SQLModel):
    kind: TakeMarkKind = TakeMarkKind.retake
    script_version: int = Field(ge=1, description="version number of the locked script read")
    beat_slug: str = Field(min_length=1, max_length=64)
    line_no: int = Field(ge=1)
    marked_at: datetime
    offset_ms: int = Field(ge=0)

    @field_validator("marked_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("marked_at needs a timezone (send an ISO 8601 timestamp with Z)")
        return value


class TakeMarkRead(SQLModel):
    id: int
    project_id: int
    script_version_id: int
    script_version: int
    kind: str
    beat_slug: str
    line_no: int
    marked_at: datetime
    offset_ms: int
    created_at: datetime


# --- Camera take markers (issue #50: the prompter drives the DJI camera over Bluetooth) ---------


class CameraTake(SQLModel, table=True):
    """One camera recording (or, in continuous mode, one beat of it) started from the prompter.

    `started_at` is the phone's wall clock when the camera acknowledged the record command, so the
    rough cut (#11) can join camera files to script lines by their start time (see `match_take`).
    """

    __table_args__ = (UniqueConstraint("client_id", name="uq_cameratake_client_id"),)

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    script_version_id: int = Field(foreign_key="scriptversion.id", index=True)
    client_id: str = Field(max_length=36)
    beat_slug: str = Field(max_length=64)
    line_from: int
    line_to: int
    retake: bool = False
    continuous: bool = False
    camera: str = Field(default="", max_length=64)
    started_at: datetime = Field(index=True)
    stopped_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)


class CameraTakeCreate(SQLModel):
    client_id: str = Field(min_length=1, max_length=36, description="client UUID; replays dedupe")
    script_version: int = Field(ge=1, description="version number of the locked script read")
    beat_slug: str = Field(min_length=1, max_length=64)
    line_from: int = Field(ge=1)
    line_to: int = Field(ge=1)
    retake: bool = False
    continuous: bool = False
    camera: str = Field(default="", max_length=64)
    started_at: datetime
    stopped_at: datetime | None = None

    @field_validator("started_at", "stopped_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps need a timezone (send an ISO 8601 timestamp with Z)")
        return value

    @model_validator(mode="after")
    def _ordered(self) -> CameraTakeCreate:
        if self.line_to < self.line_from:
            raise ValueError("line_to must be at or after line_from")
        if self.stopped_at is not None and self.stopped_at < self.started_at:
            raise ValueError("stopped_at must be at or after started_at")
        return self


class CameraTakeRead(SQLModel):
    id: int
    project_id: int
    script_version_id: int
    script_version: int
    client_id: str
    beat_slug: str
    line_from: int
    line_to: int
    retake: bool
    continuous: bool
    camera: str
    started_at: datetime
    stopped_at: datetime | None
    created_at: datetime


# --- Render workers and jobs (spike § Runtime topology) ---------------------------------------


class WorkerHeartbeat(SQLModel):
    """What a worker agent reports every few seconds."""

    name: str = Field(min_length=1, max_length=64)
    host: str = Field(default="", max_length=128)
    gpu_model: str = Field(default="", max_length=128)
    vram_total_mb: int = Field(default=0, ge=0)
    vram_free_mb: int = Field(default=0, ge=0)
    utilization_pct: int = Field(default=0, ge=0, le=100)
    temperature_c: int = 0
    capabilities: list[str] = Field(default_factory=list)
    yielding: bool = False
    busy: bool = False


class Worker(SQLModel, table=True):
    """The latest heartbeat of one render worker (one card, or a CPU-only fallback)."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True, max_length=64)
    host: str = Field(default="", max_length=128)
    gpu_model: str = Field(default="", max_length=128)
    vram_total_mb: int = 0
    vram_free_mb: int = 0
    utilization_pct: int = 0
    temperature_c: int = 0
    capabilities: str = Field(default="", max_length=256)
    yielding: bool = False
    busy: bool = False
    last_heartbeat: datetime = Field(default_factory=_now)


class WorkerRead(SQLModel):
    name: str
    host: str
    gpu_model: str
    vram_total_mb: int
    vram_free_mb: int
    utilization_pct: int
    temperature_c: int
    capabilities: list[str]
    yielding: bool
    busy: bool
    last_heartbeat: datetime


class RenderJob(SQLModel, table=True):
    """A unit of GPU (or CPU fallback) work, pulled by a worker."""

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(max_length=32)
    status: str = Field(default="queued", index=True, max_length=16)
    min_vram_mb: int = 0
    required_caps: str = Field(default="", max_length=256)
    payload: str = "{}"
    worker: str | None = Field(default=None, index=True, max_length=64)
    claimed_at: datetime | None = None
    attempts: int = 0
    outputs: str = "{}"
    created_at: datetime = Field(default_factory=_now)


class RenderJobRead(SQLModel):
    id: int
    kind: str
    status: str
    min_vram_mb: int
    required_caps: list[str]
    payload: dict[str, Any]
    worker: str | None
    attempts: int
    outputs: dict[str, Any]


class JobComplete(SQLModel):
    worker: str = Field(min_length=1, max_length=64)
    ok: bool
    outputs: dict[str, Any] = Field(default_factory=dict)


# --- Assets (spike § Data model; footage stays on the capture PC) -------------------------------


class AssetKind(StrEnum):
    camera = "camera"
    screen = "screen"
    broll = "broll"
    audio = "audio"
    music = "music"


SHA256_HEX = re.compile(r"[0-9a-f]{64}")
VIDEO_KINDS = frozenset({AssetKind.camera, AssetKind.screen, AssetKind.broll})
TRANSCRIBE_KINDS = frozenset({AssetKind.camera, AssetKind.screen})


class Asset(SQLModel, table=True):
    """A source file on the capture PC. Only its metadata and its proxy reach the dev server."""

    __table_args__ = (UniqueConstraint("host", "path", name="uq_asset_host_path"),)

    id: int | None = Field(default=None, primary_key=True)
    host: str = Field(default="", max_length=128)
    path: str = Field(index=True, max_length=1024)
    checksum: str = Field(index=True, unique=True, max_length=64)
    size_bytes: int = 0
    kind: str = Field(max_length=16)
    duration_s: float = 0.0
    proxy_path: str | None = Field(default=None, max_length=1024)
    proxy_job_id: int | None = None
    transcript_id: int | None = None
    transcript_job_id: int | None = None
    created_at: datetime = Field(default_factory=_now)


class AssetManifestEntry(SQLModel):
    """One stable, checksummed file the watcher found."""

    path: str = Field(min_length=1, max_length=1024)
    host: str = Field(default="", max_length=128)
    checksum: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)
    kind: AssetKind
    duration_s: float = Field(default=0.0, ge=0)

    @field_validator("checksum")
    @classmethod
    def _sha256_hex(cls, v: str) -> str:
        if not SHA256_HEX.fullmatch(v):
            raise ValueError("checksum must be a lowercase sha256 hex digest")
        return v


class AssetManifest(SQLModel):
    entries: list[AssetManifestEntry] = Field(default_factory=list, max_length=1000)


class AssetRead(SQLModel):
    id: int
    host: str
    path: str
    checksum: str
    size_bytes: int
    kind: AssetKind
    duration_s: float
    proxy_path: str | None
    proxy_job_id: int | None
    transcript_id: int | None = None
    transcript_job_id: int | None = None
    created_at: datetime


# --- Transcripts (spike § Stack: WhisperX word timestamps, faster-whisper fallback) ------------

MAX_TRANSCRIPT_WORDS = 200_000


class Transcript(SQLModel, table=True):
    """Word-timed speech of one Asset; the words are a JSON array of TranscriptWord."""

    id: int | None = Field(default=None, primary_key=True)
    asset_id: int = Field(index=True)
    engine: str = Field(default="", max_length=32)
    model: str = Field(default="", max_length=64)
    device: str = Field(default="", max_length=16)
    language: str = Field(default="", max_length=16)
    text: str = ""
    words: str = "[]"
    word_count: int = 0
    created_at: datetime = Field(default_factory=_now)


class TranscriptWord(SQLModel):
    """One spoken word: seconds from the start of the asset, and the engine's confidence."""

    word: str = Field(min_length=1, max_length=256)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _end_after_start(self) -> TranscriptWord:
        if self.end < self.start:
            raise ValueError("word end must not be before its start")
        return self


class TranscriptUpload(SQLModel):
    """What the transcribe job writes and the worker uploads."""

    engine: str = Field(min_length=1, max_length=32)
    model: str = Field(default="", max_length=64)
    device: str = Field(default="", max_length=16)
    language: str = Field(default="", max_length=16)
    words: list[TranscriptWord] = Field(default_factory=list, max_length=MAX_TRANSCRIPT_WORDS)


class TranscriptRead(SQLModel):
    id: int
    asset_id: int
    engine: str
    model: str
    device: str
    language: str
    text: str
    words: list[TranscriptWord]
    created_at: datetime


class AssetDetail(AssetRead):
    """The asset view: the asset plus its transcript once the transcribe job is done."""

    transcript: TranscriptRead | None = None
