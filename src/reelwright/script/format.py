# SPDX-License-Identifier: AGPL-3.0-or-later
"""Script/Beat/Cue models and the markdown renderer (spike § Data model; docs/script-format.md)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from reelwright.db.models import ProjectFormat


class CueType(StrEnum):
    broll = "broll"
    anim = "anim"
    screen = "screen"
    fact = "fact"
    sfx = "sfx"
    music = "music"
    cut = "cut"
    short = "short"


CUE_TYPES: tuple[str, ...] = tuple(t.value for t in CueType)
FRONT_MATTER_KEYS: tuple[str, ...] = (
    "project",
    "format",
    "working_title",
    "title_options",
    "thumbnail_ideas",
    "target_length",
    "status",
)
POSITION_KEYS = frozenset({"line", "col", "end_col", "line_no"})


class FrontMatter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str | None = None
    format: ProjectFormat
    working_title: str = Field(min_length=1)
    title_options: list[str] = Field(default_factory=list)
    thumbnail_ideas: list[str] = Field(default_factory=list)
    target_length: str | None = None
    status: str = "draft"


class Cue(BaseModel):
    """`[type: payload]`, alone on a line (standalone) or attached at `at` chars into the text."""

    type: CueType
    payload: str
    standalone: bool
    at: int = 0
    line: int
    col: int
    end_col: int


class Todo(BaseModel):
    text: str
    line: int
    col: int


class Line(BaseModel):
    """One source line inside a beat. A `spoken` line is one alignment unit."""

    kind: Literal["spoken", "cue", "todo"]
    text: str
    line_no: int
    cues: list[Cue] = Field(default_factory=list)
    adlibs: list[str] = Field(default_factory=list)


class Beat(BaseModel):
    slug: str
    type: str
    intent: str
    index: int
    line: int
    lines: list[Line] = Field(default_factory=list)

    @property
    def cues(self) -> list[Cue]:
        return [cue for line in self.lines for cue in line.cues]

    @property
    def spoken(self) -> list[Line]:
        return [line for line in self.lines if line.kind == "spoken"]


class ScriptError(BaseModel):
    line: int
    col: int = 1
    message: str


class Script(BaseModel):
    front_matter: FrontMatter | None = None
    beats: list[Beat] = Field(default_factory=list)
    todos: list[Todo] = Field(default_factory=list)

    @property
    def cues(self) -> list[Cue]:
        return [cue for beat in self.beats for cue in beat.cues]

    def content(self) -> Any:
        """The model without source positions: equal for scripts that say the same thing."""
        return _strip_positions(self.model_dump(mode="json"))


def _strip_positions(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_positions(v) for k, v in value.items() if k not in POSITION_KEYS}
    if isinstance(value, list):
        return [_strip_positions(v) for v in value]
    return value


def cue_tag(cue: Cue) -> str:
    return f"[{cue.type.value}: {cue.payload}]" if cue.payload else f"[{cue.type.value}:]"


def _render_line(line: Line) -> str:
    if line.kind == "cue":
        return " ".join(cue_tag(c) for c in line.cues)
    if line.kind == "todo":
        return line.text
    out: list[str] = []
    prev = 0
    for cue in sorted(line.cues, key=lambda c: c.at):
        out.extend([line.text[prev : cue.at].strip(), cue_tag(cue)])
        prev = cue.at
    out.append(line.text[prev:].strip())
    return " ".join(part for part in out if part)


def render(script: Script) -> str:
    """Markdown that parses back to the same `Script.content()`."""
    parts: list[str] = []
    if script.front_matter is not None:
        data = script.front_matter.model_dump(mode="json", exclude_none=True)
        parts.append("---\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True) + "---")
    for beat in script.beats:
        parts.append(f"## beat: {beat.slug} | intent: {beat.intent}")
        body = "\n".join(_render_line(line) for line in beat.lines)
        if body:
            parts.append(body)
    return "\n\n".join(parts) + "\n"
