# SPDX-License-Identifier: AGPL-3.0-or-later
"""SpectraCut is a pinned git dependency and its edit model imports."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_spectracut_edit_imports() -> None:
    import spectracut.edit as edit

    assert edit.Edit.__name__ == "Edit"


def test_spectracut_is_pinned_to_a_commit() -> None:
    deps = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    spec = next(d for d in deps if d.startswith("spectracut"))
    assert re.fullmatch(
        r"spectracut @ git\+https://github\.com/RapierCraftStudios/spectracut@[0-9a-f]{40}", spec
    ), spec
