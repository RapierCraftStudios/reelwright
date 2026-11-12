# SPDX-License-Identifier: AGPL-3.0-or-later
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlmodel import SQLModel

from reelwright.db import models  # noqa: F401
from reelwright.db.session import alembic_config, get_engine, migrate


def test_migrate_creates_project_table(tmp_db: str) -> None:
    migrate()
    engine = get_engine()
    assert {"project", "worker", "renderjob", "asset", "transcript"} <= set(
        inspect(engine).get_table_names()
    )
    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    assert len(heads) == 1, f"multiple migration heads: {heads}"
    with engine.connect() as conn:
        assert MigrationContext.configure(conn).get_current_revision() == heads[0]


def test_migrations_match_models(tmp_db: str) -> None:
    migrate()
    with get_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), SQLModel.metadata)
    assert diff == []
