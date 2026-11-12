# SPDX-License-Identifier: AGPL-3.0-or-later
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlmodel import SQLModel

from reelwright.db import models  # noqa: F401
from reelwright.db.session import get_engine, migrate


def test_migrate_creates_tables(tmp_db: str) -> None:
    migrate()
    engine = get_engine()
    assert {"project", "scriptversion"} <= set(inspect(engine).get_table_names())
    with engine.connect() as conn:
        assert MigrationContext.configure(conn).get_current_revision() == "0002"


def test_migrations_match_models(tmp_db: str) -> None:
    migrate()
    with get_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), SQLModel.metadata)
    assert diff == []
