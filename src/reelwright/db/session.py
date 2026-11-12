# SPDX-License-Identifier: AGPL-3.0-or-later
"""Database URL, engine, migrations and the per-request session."""

from __future__ import annotations

import os
from collections.abc import Iterator
from functools import cache
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlmodel import Session, create_engine

DEFAULT_DB_URL = "sqlite:///./data/reelwright.sqlite"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def database_url() -> str:
    """The database URL from REELWRIGHT_DB_URL, creating a SQLite file's parent directory."""
    url = os.environ.get("REELWRIGHT_DB_URL") or DEFAULT_DB_URL
    prefix = "sqlite:///"
    if url.startswith(prefix) and url != prefix + ":memory:":
        Path(url.removeprefix(prefix)).parent.mkdir(parents=True, exist_ok=True)
    return url


@cache
def _engine_for(url: str) -> Engine:
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args)


def get_engine() -> Engine:
    return _engine_for(database_url())


def alembic_config(url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url or database_url())
    return cfg


def migrate() -> None:
    """Apply every Alembic migration up to head."""
    command.upgrade(alembic_config(), "head")


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
