# SPDX-License-Identifier: AGPL-3.0-or-later
"""/api/projects CRUD (spike § Route/API map)."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, col, select

from reelwright.db.models import Project, ProjectCreate, ProjectRead
from reelwright.db.session import get_session

router = APIRouter(prefix="/api/projects", tags=["projects"])
SessionDep = Annotated[Session, Depends(get_session)]


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:200] or "project"


def unique_slug(session: Session, title: str) -> str:
    base = slugify(title)
    taken = set(
        session.exec(
            select(Project.slug).where(
                (col(Project.slug) == base) | col(Project.slug).startswith(f"{base}-")
            )
        ).all()
    )
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def list_projects(session: Session) -> list[Project]:
    query = select(Project).order_by(col(Project.created_at).desc(), col(Project.id).desc())
    return list(session.exec(query).all())


@router.get("", response_model=list[ProjectRead])
def get_projects(session: SessionDep) -> list[Project]:
    return list_projects(session)


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(body: ProjectCreate, session: SessionDep) -> Project:
    project = Project.model_validate(body, update={"slug": unique_slug(session, body.title)})
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(project_id: int, session: SessionDep) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project
