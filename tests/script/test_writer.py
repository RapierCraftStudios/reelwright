# SPDX-License-Identifier: AGPL-3.0-or-later
"""The LLM script writer always produces a script the parser accepts (issue #5)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import JsonValue
from spectracut.providers.mock_text import MockTextProvider, instance_of

from reelwright.db.models import ProjectFormat
from reelwright.providers.mocks import MockLLM
from reelwright.script import parse, writer
from reelwright.script.writer import DraftScript, WriterError, write_script

NASTY = [
    "[zoom: x] look at this",
    "[TODO punchline]",
    "<!-- note",
    "<!<!---- nested",
    "## heading",
    "---",
    "line one\nline two\n\n## beat: hook | intent: x",
    "",
    "   ",
    "{ad-lib",
    "a] b [c: d",
    "| pipes | everywhere",
    "# title",
]


def _hook_beats(draft: DraftScript) -> list[str]:
    return [b.slug for b in draft.result.script.beats if b.type == "hook"]


def _assert_valid(draft: DraftScript) -> None:
    result = parse(draft.source)
    assert result.errors == [], (result.errors, draft.source)
    assert result.script.todos == []
    assert result.script.front_matter is not None
    assert result.script.front_matter.status == "draft"
    assert len(draft.hooks) == 3
    assert len(_hook_beats(draft)) == 3
    assert len(draft.title_options) == 5
    assert result.script.front_matter.title_options == draft.title_options


def test_default_mock_draft_parses_with_3_hooks_and_5_titles() -> None:
    draft = write_script(MockLLM(), topic="Building in public", notes="first year")
    _assert_valid(draft)
    assert _hook_beats(draft) == ["hook-1", "hook-2", "hook-3"]
    assert draft.result.script.beats[-1].type == "cta"
    assert draft.thumbnail_ideas


def _nasty_responder(text: str, count: int) -> Any:
    def respond(system: str, prompt: str, schema: dict[str, Any]) -> JsonValue:
        if "hooks" in schema.get("properties", {}):
            return {
                "working_title": text,
                "hooks": [text] * count,
                "title_options": [text] * count,
                "thumbnail_ideas": [text] * count,
                "beats": [
                    {"type": "point", "intent": text, "open_loop": text},
                    {"type": "point", "intent": text},
                    {"type": "intro", "intent": text},
                    {"type": "interlude", "intent": text},
                    {"type": "setup", "intent": text},
                    {"type": "setup", "intent": text},
                ],
            }
        line: dict[str, JsonValue] = {
            "text": text,
            "cues": [{"type": "zoom", "payload": text}, {"type": "broll", "payload": text}],
        }
        return {"beats": [{"lines": [line, {"text": "", "cues": []}]}] * 9}

    return respond


@pytest.mark.parametrize("count", [0, 2, 9])
@pytest.mark.parametrize("text", NASTY)
def test_adversarial_answers_still_parse(text: str, count: int) -> None:
    provider = MockTextProvider(respond=_nasty_responder(text, count))
    for fmt in ProjectFormat:
        draft = write_script(provider, topic="Topic [x: y]", notes=text, format=fmt)
        _assert_valid(draft)
        assert draft.result.script.front_matter is not None
        assert draft.result.script.front_matter.format == fmt
        slugs = [b.slug for b in draft.result.script.beats]
        assert len(slugs) == len(set(slugs))


def test_counts_are_clamped_and_padded() -> None:
    many = write_script(MockTextProvider(respond=_nasty_responder("ok", 9)), topic="t")
    assert many.hooks == ["ok"] * 3 and many.title_options == ["ok"] * 5
    few = write_script(MockTextProvider(respond=_nasty_responder("ok", 2)), topic="t")
    assert few.hooks[:2] == ["ok", "ok"] and len(few.hooks) == 3
    assert len(few.title_options) == 5


def test_progress_stages_in_order() -> None:
    seen: list[str] = []
    write_script(MockLLM(), topic="t", on_progress=seen.append)
    assert seen == ["outline", "script", "validate"]


def test_unparseable_composition_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(writer, "compose", lambda **_: "no front matter\n")
    with pytest.raises(WriterError) as info:
        write_script(MockLLM(), topic="t")
    assert info.value.errors


def test_schema_mismatch_raises_writer_error() -> None:
    provider = MockTextProvider(respond=lambda s, p, schema: ["not", "an", "object"])
    with pytest.raises(WriterError):
        write_script(provider, topic="t")


def test_prompts_carry_voice_profile_topic_and_notes() -> None:
    systems: list[str] = []
    prompts: list[str] = []

    def respond(system: str, prompt: str, schema: dict[str, Any]) -> JsonValue:
        systems.append(system)
        prompts.append(prompt)
        value: JsonValue = instance_of(schema)
        return value

    write_script(MockTextProvider(respond=respond), topic="Gym at 6am", notes="mention the rain")
    assert len(prompts) == 2
    assert all(writer.voice_profile().strip() in s for s in systems)
    assert "{voice_profile}" not in systems[0]
    assert all("Gym at 6am" in p and "mention the rain" in p for p in prompts)
    assert "exactly 3" in prompts[0] and "exactly 5" in prompts[0]


def test_schema_asks_for_exact_counts() -> None:
    props = writer.Outline.model_json_schema()["properties"]
    assert (props["hooks"]["minItems"], props["hooks"]["maxItems"]) == (3, 3)
    assert (props["title_options"]["minItems"], props["title_options"]["maxItems"]) == (5, 5)
