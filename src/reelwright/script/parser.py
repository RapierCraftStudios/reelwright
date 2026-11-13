# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parse script markdown into Script/Beat/Cue, collecting every error with its line and column."""

from __future__ import annotations

import re
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from reelwright.script.format import (
    CUE_TYPES,
    Beat,
    Cue,
    CueType,
    FrontMatter,
    Line,
    Script,
    ScriptError,
    Todo,
)

COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
BEAT_RE = re.compile(
    r"^##\s+beat:\s*(?P<slug>[a-z0-9][a-z0-9-]*)\s*\|\s*intent:\s*(?P<intent>\S.*?)\s*$"
)
TAG_RE = re.compile(
    r"(?P<todo>\[TODO\b[^\]]*\])"
    r"|(?P<cue>\[(?P<type>[A-Za-z][\w-]*)[ \t]*:[ \t]*(?P<payload>[^\]\[]*?)[ \t]*\])"
)
MALFORMED_BEAT = "malformed beat heading; expected '## beat: <slug> | intent: <text>'"
ADLIB_RE = re.compile(r"\{([^{}]*)\}")


class ParseResult(BaseModel):
    script: Script
    errors: list[ScriptError]

    @property
    def ok(self) -> bool:
        return not self.errors


def _squash(text: str) -> str:
    return " ".join(text.split())


def _strip_comments(source: str, errors: list[ScriptError]) -> str:
    def blank(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group())

    stripped = COMMENT_RE.sub(blank, source)
    start = stripped.find("<!--")
    if start != -1:
        line = stripped.count("\n", 0, start) + 1
        col = start - (stripped.rfind("\n", 0, start) + 1) + 1
        errors.append(ScriptError(line=line, col=col, message="unclosed comment '<!--'"))
        stripped = stripped[:start] + re.sub(r"[^\n]", " ", stripped[start:])
    return stripped


def _front_matter(lines: list[str], errors: list[ScriptError]) -> tuple[FrontMatter | None, int]:
    """Returns the front matter and the index of the first body line."""
    if not lines or lines[0].strip() != "---":
        errors.append(ScriptError(line=1, message="missing YAML front matter ('---' on line 1)"))
        return None, 0
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        errors.append(ScriptError(line=1, message="front matter is not closed with '---'"))
        return None, len(lines)
    try:
        data: Any = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark
        line = mark.line + 2 if mark is not None else 1
        errors.append(ScriptError(line=line, message=f"front matter: {exc.problem}"))
        return None, end + 1
    if not isinstance(data, dict):
        errors.append(ScriptError(line=2, message="front matter must be a mapping of keys"))
        return None, end + 1
    if isinstance(data.get("target_length"), int | float):
        data["target_length"] = str(data["target_length"])

    def key_line(key: object) -> int:
        for i in range(1, end):
            if lines[i].split(":", 1)[0].strip() == key:
                return i + 1
        return 1

    try:
        return FrontMatter.model_validate(data), end + 1
    except ValidationError as exc:
        for err in exc.errors():
            key = err["loc"][0] if err["loc"] else ""
            if err["type"] == "extra_forbidden":
                message = f"unknown front matter key '{key}'"
            elif err["type"] == "missing":
                message = f"front matter is missing '{key}'"
            else:
                message = f"front matter '{key}': {err['msg']}"
            errors.append(ScriptError(line=key_line(key), message=message))
        return None, end + 1


def _parse_line(raw: str, line_no: int, errors: list[ScriptError], todos: list[Todo]) -> Line:
    parts: list[str] = []
    cues: list[Cue] = []
    has_todo = False
    pos = 0
    for match in TAG_RE.finditer(raw):
        col = match.start() + 1
        if match.group("todo"):
            has_todo = True
            todos.append(Todo(text=match.group("todo"), line=line_no, col=col))
            continue
        parts.append(raw[pos : match.start()])
        pos = match.end()
        kind = match.group("type")
        if kind not in CUE_TYPES:
            errors.append(
                ScriptError(
                    line=line_no,
                    col=col,
                    message=f"unknown cue type '{kind}' (expected one of: {', '.join(CUE_TYPES)})",
                )
            )
            continue
        cues.append(
            Cue(
                type=CueType(kind),
                payload=match.group("payload"),
                standalone=False,
                at=len(_squash(" ".join(parts))),
                line=line_no,
                col=col,
                end_col=match.end() + 1,
            )
        )
    parts.append(raw[pos:])
    text = _squash(" ".join(parts))
    if not text and cues:
        for cue in cues:
            cue.standalone = True
            cue.at = 0
        return Line(kind="cue", text="", line_no=line_no, cues=cues)
    if has_todo and TAG_RE.fullmatch(text) and text.startswith("[TODO"):
        return Line(kind="todo", text=text, line_no=line_no, cues=cues)
    adlibs = [m.group(1).strip() for m in ADLIB_RE.finditer(text)]
    return Line(kind="spoken", text=text, line_no=line_no, cues=cues, adlibs=adlibs)


def parse(source: str) -> ParseResult:
    """Parse script markdown. Errors are collected, not raised; the script is best effort."""
    errors: list[ScriptError] = []
    todos: list[Todo] = []
    lines = _strip_comments(source.replace("\r\n", "\n"), errors).split("\n")
    front_matter, start = _front_matter(lines, errors)
    beats: list[Beat] = []
    seen: dict[str, int] = {}
    for index in range(start, len(lines)):
        line_no = index + 1
        raw = lines[index].rstrip()
        if not raw.strip():
            continue
        if raw.lstrip().startswith("## ") or raw.strip() == "##":
            match = BEAT_RE.match(raw.strip())
            if match is None:
                errors.append(
                    ScriptError(
                        line=line_no,
                        message=MALFORMED_BEAT,
                    )
                )
                continue
            slug = match.group("slug")
            if slug in seen:
                errors.append(
                    ScriptError(
                        line=line_no,
                        message=f"duplicate beat '{slug}' (first on line {seen[slug]})",
                    )
                )
            seen.setdefault(slug, line_no)
            beats.append(
                Beat(
                    slug=slug,
                    type=slug.split("-")[0],
                    intent=match.group("intent"),
                    index=len(beats),
                    line=line_no,
                )
            )
            continue
        parsed = _parse_line(raw, line_no, errors, todos)
        if not beats:
            errors.append(
                ScriptError(line=line_no, message="content before the first '## beat:' heading")
            )
            continue
        beats[-1].lines.append(parsed)
    if not beats and not errors:
        errors.append(ScriptError(line=max(start, 1), message="script has no beats"))
    errors.sort(key=lambda e: (e.line, e.col))
    script = Script(front_matter=front_matter, beats=beats, todos=todos)
    return ParseResult(script=script, errors=errors)
