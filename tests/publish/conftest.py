# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fixtures for the publish tests: an edit with beat markers and an in-memory database."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from reelwright.db.models import Project, Render
from reelwright.edit.build import edit_to_otio_json

from .fixtures import beat_edit


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def render(session: Session) -> Render:
    project = Project(title="Publish", slug="publish")
    session.add(project)
    session.commit()
    session.refresh(project)
    row = Render(
        project_id=project.id or 0,
        status="done",
        otio=edit_to_otio_json(beat_edit()),
        fps_num=30,
        frames=2700,
        target_path="D:\\out\\final.mp4",
        output_path="D:\\out\\final.mp4",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
