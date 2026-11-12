# SPDX-License-Identifier: AGPL-3.0-or-later
"""merge publish and render qa

Revision ID: 1378540ee7d3
Revises: 0009, 0010
Create Date: 2026-11-12 15:08:08.543693
"""

from collections.abc import Sequence

revision: str = "1378540ee7d3"
down_revision: str | None = ("0009", "0010")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
