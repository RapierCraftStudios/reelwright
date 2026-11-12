# SPDX-License-Identifier: AGPL-3.0-or-later
import re
from pathlib import Path

from reelwright.script import CUE_TYPES, CueType, parse, render

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "every-cue.md"
HEADER = "---\nformat: long\nworking_title: Test\n---\n"


def test_fixture_parses_with_every_cue_type() -> None:
    result = parse(FIXTURE.read_text())
    assert result.errors == []
    script = result.script
    assert script.front_matter is not None
    assert script.front_matter.format == "long"
    assert script.front_matter.title_options[0] == "I quit my job to build in public"
    assert script.front_matter.target_length == "8-10 min"
    assert {c.type for c in script.cues} == set(CueType)
    assert set(CUE_TYPES) == {"broll", "anim", "screen", "fact", "sfx", "music", "cut", "short"}
    assert [b.slug for b in script.beats] == ["hook", "setup", "point-1", "cta"]
    assert [b.type for b in script.beats] == ["hook", "setup", "point", "cta"]
    assert script.beats[0].intent == "open loop on the resignation email"


def test_round_trip() -> None:
    first = parse(FIXTURE.read_text())
    rendered = render(first.script)
    second = parse(rendered)
    assert second.errors == []
    assert second.script.content() == first.script.content()
    assert render(second.script) == rendered


def test_unknown_cue_reports_line_and_column() -> None:
    source = HEADER + "## beat: hook | intent: x\nHello there [zoom: 2x] friend\n"
    result = parse(source)
    assert [(e.line, e.col) for e in result.errors] == [(6, 13)]
    assert "unknown cue type 'zoom'" in result.errors[0].message


def test_every_error_is_reported() -> None:
    source = (
        "---\nformat: movie\nworking_title: T\nmood: x\n---\n"
        "stray line\n## beat: hook | intent: x\n[zoom: a]\n## Beat Two\n## beat: hook | intent: y\n"
    )
    errors = parse(source).errors
    lines = [e.line for e in errors]
    assert lines == sorted(lines)
    messages = " | ".join(f"{e.line}:{e.message}" for e in errors)
    assert "2:front matter 'format'" in messages
    assert "4:unknown front matter key 'mood'" in messages
    assert "6:content before the first" in messages
    assert "8:unknown cue type 'zoom'" in messages
    assert "9:malformed beat heading" in messages
    assert "10:duplicate beat 'hook'" in messages


def test_front_matter_is_required_and_yaml_errors_have_lines() -> None:
    assert parse("## beat: a | intent: b\nhi\n").errors[0].message.startswith("missing YAML")
    bad = parse("---\nformat: long\nworking_title: [unclosed\n---\n## beat: a | intent: b\n")
    assert bad.errors and bad.errors[0].line >= 2


def test_comments_are_ignored() -> None:
    source = (
        HEADER + "## beat: a | intent: b\n<!-- [zoom: x] note -->\n<!--\n[nope: x]\n-->\nSpoken.\n"
    )
    result = parse(source)
    assert result.errors == []
    lines = result.script.beats[0].lines
    assert [(line.kind, line.text, line.line_no) for line in lines] == [("spoken", "Spoken.", 10)]


def test_unclosed_comment_is_an_error() -> None:
    errors = parse(HEADER + "## beat: a | intent: b\n<!-- open\nSpoken.\n").errors
    assert [(e.line, e.message) for e in errors] == [(6, "unclosed comment '<!--'")]


def test_cues_standalone_and_attached() -> None:
    source = (
        HEADER + "## beat: a | intent: b\n[broll: desk] [sfx: pop]\nSay this [cut: jump] now.\n"
    )
    lines = parse(source).script.beats[0].lines
    assert lines[0].kind == "cue"
    assert [(c.type, c.payload, c.standalone) for c in lines[0].cues] == [
        ("broll", "desk", True),
        ("sfx", "pop", True),
    ]
    assert lines[1].kind == "spoken"
    assert lines[1].text == "Say this now."
    cue = lines[1].cues[0]
    assert (cue.standalone, cue.at, cue.line, cue.col, cue.end_col) == (False, 8, 7, 10, 21)


def test_adlibs_and_todos() -> None:
    source = (
        HEADER
        + "## beat: a | intent: b\n{wave at camera} Hi.\n[TODO hook line]\nIt costs [TODO price].\n"
    )
    result = parse(source)
    assert result.errors == []
    lines = result.script.beats[0].lines
    assert lines[0].adlibs == ["wave at camera"]
    assert lines[0].text == "{wave at camera} Hi."
    assert lines[1].kind == "todo"
    assert lines[2].kind == "spoken"
    assert [(t.line, t.text) for t in result.script.todos] == [
        (7, "[TODO hook line]"),
        (8, "[TODO price]"),
    ]


def test_each_spoken_line_is_one_unit() -> None:
    source = HEADER + "## beat: a | intent: b\nOne.\n\nTwo.\n   \nThree.\n"
    spoken = parse(source).script.beats[0].spoken
    assert [(line.text, line.line_no) for line in spoken] == [
        ("One.", 6),
        ("Two.", 8),
        ("Three.", 10),
    ]


def test_docs_example_parses() -> None:
    doc = (ROOT / "docs" / "script-format.md").read_text()
    match = re.search(r"```markdown\n(.*?)```", doc, re.DOTALL)
    assert match is not None
    result = parse(match.group(1))
    assert result.errors == []
    assert {c.type for c in result.script.cues} == set(CueType)
