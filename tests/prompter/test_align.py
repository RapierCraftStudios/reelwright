# SPDX-License-Identifier: AGPL-3.0-or-later
"""ScriptAligner against recorded ASR word streams (tests/prompter/fixtures, issue #7)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reelwright.prompter.align import Position, ScriptAligner, ScriptLine, normalize, spoken_lines
from reelwright.script import parse

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


def _aligner(lines: list[str]) -> ScriptAligner:
    # Line numbers as a script would give them: spaced out, not equal to the index.
    return ScriptAligner([ScriptLine(10 + 2 * i, text) for i, text in enumerate(lines)])


def _replay(name: str) -> list[Position | None]:
    """Replay a fixture word by word, asserting every checkpoint; return each word's result."""
    data = _fixture(name)
    aligner = _aligner(data["lines"])
    aligner.seek(10 + 2 * data["start"])
    results: list[Position | None] = []
    for step in data["steps"]:
        before = aligner.position
        for word in step["say"].split():
            results.append(aligner.feed([word]))
            if step.get("held"):
                assert aligner.position == before, f"moved on ad-lib word {word!r}"
        assert aligner.position is not None, step
        assert aligner.position.line_no == 10 + 2 * step["line"], step
    return results


def test_keeps_position_through_a_ten_word_adlib() -> None:
    data = _fixture("adlib")
    assert len(data["steps"][1]["say"].split()) == 10
    _replay("adlib")


def test_recovers_on_a_repeated_line() -> None:
    _replay("repeat")


def test_identical_lines_prefer_the_one_at_or_after_the_cursor() -> None:
    _replay("duplicate")


def test_tracks_through_misheard_words() -> None:
    _replay("mishear")


@pytest.mark.parametrize("name", ["adlib", "repeat", "duplicate", "mishear"])
def test_replay_is_deterministic(name: str) -> None:
    assert _replay(name) == _replay(name)


def test_adlib_resumes_on_the_word_after_it() -> None:
    aligner = _aligner(_fixture("adlib")["lines"])
    aligner.seek(10)
    aligner.feed("three weeks ago i deleted every editing app".split())
    aligner.feed("honestly that was terrifying my whole career lived in those".split())
    moved = aligner.feed(["on", "my", "laptop"])
    assert moved == Position(10, 10, 0.75)


def test_seek_reseeds_the_cursor() -> None:
    aligner = _aligner(_fixture("repeat")["lines"])
    aligner.seek(16)
    assert aligner.feed(["today"]) == Position(16, 0, 1.0)
    aligner.seek(12)
    assert aligner.feed(["not", "because"]) == Position(12, 1, 1.0)
    with pytest.raises(ValueError):
        aligner.seek(11)


def test_spoken_lines_skip_cues_todos_and_blank_lines() -> None:
    source = (
        "---\nformat: long\nworking_title: T\n---\n"
        "## beat: hook | intent: grab\n[music: low pulse]\nHi there {wave}. [broll: desk]\n"
        "[TODO punchline]\n"
        "## beat: cta | intent: subscribe\nSee you next time.\n"
    )
    lines = spoken_lines(parse(source).script)
    assert [line.line_no for line in lines] == [7, 10]
    aligner = ScriptAligner(lines)
    assert aligner.words == ["hi", "there", "see", "you", "next", "time"]


def test_normalize_drops_punctuation_case_and_adlib_prompts() -> None:
    assert normalize("Today I'm {smile} going — to SHOW you!") == [
        "today",
        "im",
        "going",
        "to",
        "show",
        "you",
    ]


def test_no_spoken_lines_is_an_error() -> None:
    with pytest.raises(ValueError):
        ScriptAligner([])
