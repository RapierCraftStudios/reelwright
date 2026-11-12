# SPDX-License-Identifier: AGPL-3.0-or-later
"""LLM script writer: topic and notes → outline → retention-structured script with cues.

Runs through SpectraCut's providers layer (spike § Stack): any ``TextProvider`` that answers
``complete(system, prompt, *, schema)`` with JSON. The model never writes markdown. It returns
structured JSON, and the writer assigns slugs, sanitises every string and composes the markdown
itself, then re-parses it, so the output always parses with the script parser.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Generator
from importlib import resources
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from spectracut.providers.base import TextProvider

from reelwright.db.models import ProjectFormat
from reelwright.script.format import CUE_TYPES, ScriptError
from reelwright.script.parser import ParseResult, parse

HOOK_COUNT = 3
TITLE_COUNT = 5
MAX_THUMBNAILS = 3
BEAT_TYPES: tuple[str, ...] = ("setup", "point", "payoff", "interlude", "cta")
NUMBERED_TYPES = frozenset({"point", "interlude"})
STAGES: tuple[str, ...] = ("outline", "script", "validate")

Progress = Callable[[str], None]


class WriterError(RuntimeError):
    """The model's answer could not be turned into a script that parses."""

    def __init__(self, message: str, errors: list[ScriptError] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


# --- What the model is asked for. Counts and enums go into the JSON schema through
# json_schema_extra only, so a model that ignores them is clamped below instead of rejected.


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class OutlineBeat(_Lenient):
    type: str = Field(json_schema_extra={"enum": list(BEAT_TYPES)})
    intent: str
    open_loop: str = ""


class Outline(_Lenient):
    working_title: str
    hooks: list[str] = Field(json_schema_extra={"minItems": HOOK_COUNT, "maxItems": HOOK_COUNT})
    title_options: list[str] = Field(
        json_schema_extra={"minItems": TITLE_COUNT, "maxItems": TITLE_COUNT}
    )
    thumbnail_ideas: list[str] = Field(
        json_schema_extra={"minItems": 1, "maxItems": MAX_THUMBNAILS}
    )
    beats: list[OutlineBeat] = Field(json_schema_extra={"minItems": 3})


class CueSuggestion(_Lenient):
    type: str = Field(json_schema_extra={"enum": list(CUE_TYPES)})
    payload: str = ""


class ScriptLine(_Lenient):
    text: str = ""
    cues: list[CueSuggestion] = Field(default_factory=list)


class ScriptBeat(_Lenient):
    lines: list[ScriptLine] = Field(default_factory=list)


class ScriptBody(_Lenient):
    beats: list[ScriptBeat]


class DraftScript(BaseModel):
    """The generated draft: markdown source plus what the acceptance criteria count."""

    source: str
    hooks: list[str]
    title_options: list[str]
    thumbnail_ideas: list[str]
    result: ParseResult


# --- Sanitising: nothing the model says may become markdown syntax the parser reacts to.

_LEADING_JUNK = re.compile(r"^[\s#|>-]+")


def clean(text: object, fallback: str = "") -> str:
    """One line of plain text: no tags, comments, headings or front-matter fences."""
    value = str(text) if text is not None else ""
    value = value.replace("[", "(").replace("]", ")")
    while "<!--" in value:
        value = value.replace("<!--", "")
    value = value.replace("-->", "")
    value = " ".join(value.split())
    value = _LEADING_JUNK.sub("", value).strip()
    return value or fallback


def _fit(items: list[str], count: int, pad: Callable[[int], str]) -> list[str]:
    cleaned = [c for c in (clean(i) for i in items) if c][:count]
    while len(cleaned) < count:
        cleaned.append(pad(len(cleaned) + 1))
    return cleaned


def _slugs(types: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    slugs: list[str] = []
    for kind in types:
        seen[kind] = seen.get(kind, 0) + 1
        n = seen[kind]
        slugs.append(f"{kind}-{n}" if kind in NUMBERED_TYPES or n > 1 else kind)
    return slugs


# --- Prompts


def _resource(package: str, name: str) -> str:
    return resources.files(package).joinpath(name).read_text(encoding="utf-8")


def _fill(template: str, **values: str) -> str:
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def voice_profile() -> str:
    return _resource("reelwright.script", "voice_profile.md")


def system_prompt() -> str:
    return _fill(_resource("reelwright.script.prompts", "system.md"), voice_profile=voice_profile())


def outline_prompt(topic: str, notes: str, fmt: ProjectFormat) -> str:
    template = _resource("reelwright.script.prompts", "outline.md")
    return _fill(template, topic=topic, notes=notes or "(none)", format=fmt.value)


def script_prompt(topic: str, notes: str, outline: Outline) -> str:
    template = _resource("reelwright.script.prompts", "script.md")
    outline_json = json.dumps(outline.model_dump(mode="json"), indent=2, ensure_ascii=False)
    return _fill(template, topic=topic, notes=notes or "(none)", outline_json=outline_json)


def _ask[M: BaseModel](
    provider: TextProvider, system: str, prompt: str, model: type[M], stage: str
) -> M:
    completion = provider.complete(system, prompt, schema=model.model_json_schema())
    try:
        return model.model_validate(completion.output)
    except ValidationError as exc:
        raise WriterError(f"{stage}: the model's answer does not match the schema: {exc}") from exc


# --- Composition


def _beat_types(outline: Outline) -> list[OutlineBeat]:
    beats = [b for b in outline.beats if b.type in BEAT_TYPES]
    if not beats:
        beats = [OutlineBeat(type="setup", intent="why this matters")]
    if beats[-1].type != "cta":
        beats.append(OutlineBeat(type="cta", intent="subscribe and the next video"))
    return beats


def _line(line: ScriptLine) -> str:
    text = clean(line.text)
    tags = [
        f"[{cue.type}: {payload}]" if (payload := clean(cue.payload)) else f"[{cue.type}:]"
        for cue in line.cues
        if cue.type in CUE_TYPES
    ]
    return " ".join([text, *tags]) if text else " ".join(tags)


def compose(
    *,
    fmt: ProjectFormat,
    working_title: str,
    hooks: list[str],
    title_options: list[str],
    thumbnail_ideas: list[str],
    beats: list[OutlineBeat],
    body: ScriptBody,
) -> str:
    """Script markdown in the docs/script-format.md format, from already-clean values."""
    front: dict[str, Any] = {
        "format": fmt.value,
        "working_title": working_title,
        "title_options": title_options,
        "thumbnail_ideas": thumbnail_ideas,
        "status": "draft",
    }
    dumped = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=1_000_000)
    parts = ["---\n" + dumped + "---"]
    for n, hook in enumerate(hooks, start=1):
        parts.append(f"## beat: hook-{n} | intent: hook variant {n} for the first 30 s\n{hook}")
    for i, (slug, beat) in enumerate(zip(_slugs([b.type for b in beats]), beats, strict=True)):
        intent = clean(beat.intent, fallback=beat.type)
        loop = clean(beat.open_loop)
        if loop:
            intent = f"{intent} (open loop: {loop})"
        section = [f"## beat: {slug} | intent: {intent}"]
        lines = body.beats[i].lines if i < len(body.beats) else []
        section.extend(rendered for line in lines if (rendered := _line(line)))
        parts.append("\n".join(section))
    return "\n\n".join(parts) + "\n"


def write_steps(
    provider: TextProvider, *, topic: str, notes: str = "", fmt: ProjectFormat
) -> Generator[str, None, DraftScript]:
    """Yield each stage name before running it; return the draft when done."""
    system = system_prompt()
    yield "outline"
    outline = _ask(provider, system, outline_prompt(topic, notes, fmt), Outline, "outline")
    beats = _beat_types(outline)
    yield "script"
    outline_for_script = outline.model_copy(update={"beats": beats})
    body = _ask(
        provider, system, script_prompt(topic, notes, outline_for_script), ScriptBody, "script"
    )
    yield "validate"
    working_title = clean(outline.working_title, fallback=clean(topic, fallback="Untitled"))
    hooks = _fit(outline.hooks, HOOK_COUNT, lambda n: f"{working_title}: hook variant {n}")
    titles = _fit(outline.title_options, TITLE_COUNT, lambda n: f"{working_title} (option {n})")
    thumbnails = [c for c in (clean(t) for t in outline.thumbnail_ideas) if c][:MAX_THUMBNAILS]
    source = compose(
        fmt=fmt,
        working_title=working_title,
        hooks=hooks,
        title_options=titles,
        thumbnail_ideas=thumbnails,
        beats=beats,
        body=body,
    )
    result = parse(source)
    if result.errors:
        raise WriterError("generated script does not parse", result.errors)
    return DraftScript(
        source=source,
        hooks=hooks,
        title_options=titles,
        thumbnail_ideas=thumbnails,
        result=result,
    )


def write_script(
    provider: TextProvider,
    *,
    topic: str,
    notes: str = "",
    format: ProjectFormat = ProjectFormat.long,
    on_progress: Progress | None = None,
) -> DraftScript:
    """Generate a draft script; ``on_progress`` hears each stage in :data:`STAGES` order."""
    steps = write_steps(provider, topic=topic, notes=notes, fmt=format)
    while True:
        try:
            stage = next(steps)
        except StopIteration as done:
            draft: DraftScript = done.value
            return draft
        if on_progress is not None:
            on_progress(stage)
