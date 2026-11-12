# SPDX-License-Identifier: AGPL-3.0-or-later
"""Caption lines stay within 42 characters and inside the safe area for 16:9 and 9:16."""

from __future__ import annotations

import re

import pytest

from reelwright.captions import block_rect, cues, style_for, to_ass, with_tokens
from reelwright.captions.build import CaptionWord
from reelwright.captions.style import Aspect, Rect

TEXT = (
    "So today we are building a creator studio that writes scripts, cuts the takes, and "
    "ships captions; it handles supercalifragilisticexpialidocious-sized words and "
    "an-unbroken-token-that-is-definitely-longer-than-sixty-characters-total too, "
    "plus short ones: a I of to"
).split()


def _words() -> list[CaptionWord]:
    return [CaptionWord(t, i * 0.3, i * 0.3 + 0.25) for i, t in enumerate(TEXT)]


ASPECTS: list[Aspect] = ["16:9", "9:16"]


@pytest.mark.parametrize("aspect", ASPECTS)
def test_lines_stay_within_42_chars_and_max_lines(aspect: Aspect) -> None:
    style = style_for(aspect)
    assert style.max_chars <= 42
    track = cues(_words(), style)
    assert track
    for cue in track:
        assert 1 <= len(cue.lines) <= style.max_lines
        for line in cue.lines:
            assert 0 < len(line) <= style.max_chars <= 42
    # Nothing is lost: every character of the text is captioned.
    joined = "".join("".join(line.split()) for c in track for line in c.lines)
    assert joined == "".join(TEXT)


@pytest.mark.parametrize("aspect", ASPECTS)
def test_every_cue_block_lies_inside_the_safe_area(aspect: Aspect) -> None:
    style = style_for(aspect)
    assert style.frame.contains(style.safe)
    for cue in cues(_words(), style):
        assert style.safe.contains(block_rect(style, cue.lines)), cue.lines
    widest = ["x" * style.max_chars] * style.max_lines
    assert style.safe.contains(block_rect(style, widest))


def test_shorts_safe_area_avoids_the_platform_ui() -> None:
    style = style_for("9:16")
    assert (style.width, style.height) == (1080, 1920)
    assert style.safe.bottom <= 1920 * 0.75
    assert style.safe.right <= 1080 * 0.88
    assert style.safe.y >= 1920 * 0.12
    landscape = style_for("16:9")
    assert (landscape.width, landscape.height) == (1920, 1080)
    assert landscape.safe == Rect(96, 54, 1728, 972)


@pytest.mark.parametrize("aspect", ASPECTS)
def test_ass_places_text_inside_the_safe_area(aspect: Aspect) -> None:
    style = style_for(aspect)
    ass = to_ass(cues(_words(), style), style)
    assert f"PlayResX: {style.width}" in ass
    assert f"PlayResY: {style.height}" in ass
    line = next(x for x in ass.splitlines() if x.startswith("Style: Caption,"))
    fields = line.split(",")
    align, margin_l, margin_r, margin_v = (int(f) for f in fields[18:22])
    assert align == 2  # bottom centre
    assert margin_l >= style.safe.x
    assert style.width - margin_r <= style.safe.right
    assert style.height - margin_v <= style.safe.bottom
    assert re.search(r"\\N", ass)  # explicit line breaks; libass wrapping is off
    assert "WrapStyle: 2" in ass


def test_tokens_override_style_and_unsafe_sizes_are_rejected() -> None:
    base = style_for("9:16")
    themed = with_tokens(base, {"font_family": "House Sans", "highlight_colour": "#00E5FF"})
    assert themed.font_family == "House Sans"
    assert "&H00FFE500" in to_ass([], themed)
    with pytest.raises(ValueError, match="font too large"):
        with_tokens(base, {"font_size_px": 200})
    with pytest.raises(ValueError, match="unknown caption token"):
        with_tokens(base, {"kerning": 2})
