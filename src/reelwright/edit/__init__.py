# SPDX-License-Identifier: AGPL-3.0-or-later
"""Script-aligned best-take rough cut: align, pick takes, cut silences/fillers, build OTIO.

Public names live in the submodules (align, takes, cutter, build) and are
resolved lazily so the package namespace does not re-declare them.
"""

from importlib import import_module
from typing import Any

_SOURCES = {
    "align": ("AlignLine", "Attempt", "align_script", "script_lines"),
    "build": ("CutLine", "build_edit", "edit_to_otio_json"),
    "cutter": ("Span", "cut_take"),
    "takes": ("RetakeMark", "Scores", "Take", "pick_best_takes", "score_attempts"),
}
_LOOKUP = {name: mod for mod, names in _SOURCES.items() for name in names}


def __getattr__(name: str) -> Any:
    mod = _LOOKUP.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f"{__name__}.{mod}"), name)
