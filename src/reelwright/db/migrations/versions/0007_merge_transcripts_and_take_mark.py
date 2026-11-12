# SPDX-License-Identifier: AGPL-3.0-or-later
"""merge transcripts and take mark

Revision ID: 0007
Revises: 0005, 0006
Create Date: 2026-11-12 14:07:11.165839
"""

from collections.abc import Sequence

revision: str = "0007"
down_revision: str | Sequence[str] | None = ("0005", "0006")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
