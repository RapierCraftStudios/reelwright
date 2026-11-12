# SPDX-License-Identifier: AGPL-3.0-or-later
"""SQLModel tables and API schemas (spike § Data model)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

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
