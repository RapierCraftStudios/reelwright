# SPDX-License-Identifier: AGPL-3.0-or-later
"""SQLModel tables and API schemas (spike § Data model)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

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
