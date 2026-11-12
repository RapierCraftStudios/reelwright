# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migrations form one chain: two branches both off 0004 made `migrate()` raise MultipleHeads."""

from __future__ import annotations

from alembic.script import ScriptDirectory

from reelwright.db.session import alembic_config


def test_migrations_have_a_single_head() -> None:
    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"


def test_transcripts_and_take_mark_branches_are_merged() -> None:
    script = ScriptDirectory.from_config(alembic_config())
    head = script.get_current_head()
    assert head is not None
    ancestors = {r.revision for r in script.walk_revisions("base", head)}
    assert {"0005", "0006"} <= ancestors
