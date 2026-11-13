# SPDX-License-Identifier: AGPL-3.0-or-later
"""Caption styling: frame presets, safe areas, the line-length limit and house-token overrides.

Text size is estimated geometrically (an average glyph advance in em and a line height), not
rasterised; the pixel-level legibility check belongs to the caption QA job. House tokens
(`packages/motion/tokens`) do not exist yet, so the presets carry built-in defaults and
`with_tokens` overrides them once they do.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal

Aspect = Literal["16:9", "9:16"]

MAX_CHARS = 42
"""Hard ceiling on characters per caption line (YouTube and broadcast guidance)."""
MIN_CHARS = 12
"""Fewest characters per line a style may fit before it is rejected as unreadable."""


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    def contains(self, other: Rect) -> bool:
        eps = 1e-6
        return (
            other.x >= self.x - eps
            and other.y >= self.y - eps
            and other.right <= self.right + eps
            and other.bottom <= self.bottom + eps
        )


@dataclass(frozen=True)
class CaptionStyle:
    width: int
    height: int
    safe: Rect
    font_family: str = "Inter"
    font_size_px: int = 64
    line_height: float = 1.25
    """Line pitch in em."""
    char_width_em: float = 0.6
    """Average glyph advance in em; conservative for a bold sans."""
    max_lines: int = 2
    max_gap_s: float = 1.0
    """A silence longer than this between two words starts a new cue."""
    text_colour: str = "#FFFFFF"
    highlight_colour: str = "#FFD400"
    outline_colour: str = "#000000"
    outline_px: int = 4

    @property
    def max_chars(self) -> int:
        fit = math.floor(self.safe.w / (self.font_size_px * self.char_width_em))
        return min(MAX_CHARS, fit)

    @property
    def line_px(self) -> float:
        return self.font_size_px * self.line_height

    @property
    def frame(self) -> Rect:
        return Rect(0, 0, self.width, self.height)

    def validate(self) -> CaptionStyle:
        """Raise ValueError when the text block cannot fit the safe area."""
        if not self.frame.contains(self.safe):
            raise ValueError("safe area lies outside the frame")
        if self.max_lines < 1 or self.font_size_px <= 0:
            raise ValueError("caption style needs at least one line and a positive font size")
        if self.max_chars < MIN_CHARS:
            raise ValueError(
                f"font too large: only {self.max_chars} chars fit the safe width {self.safe.w}"
            )
        if self.max_lines * self.line_px > self.safe.h:
            raise ValueError("font too large: the caption block is taller than the safe area")
        return self


def _inset(width: int, height: int, left: float, top: float, right: float, bottom: float) -> Rect:
    x, y = math.ceil(width * left), math.ceil(height * top)
    return Rect(x, y, width - x - math.ceil(width * right), height - y - math.ceil(height * bottom))


def style_for(aspect: Aspect) -> CaptionStyle:
    """Preset for a frame shape.

    16:9 uses a 5% title-safe inset. 9:16 keeps clear of the Shorts UI: a 6% left inset,
    the top 12% (title and search), the right 12% (action buttons) and the bottom 25%
    (channel, caption text and progress bar).
    """
    if aspect == "16:9":
        return CaptionStyle(1920, 1080, _inset(1920, 1080, 0.05, 0.05, 0.05, 0.05)).validate()
    if aspect == "9:16":
        return CaptionStyle(1080, 1920, _inset(1080, 1920, 0.06, 0.12, 0.12, 0.25)).validate()
    raise ValueError(f"unknown aspect {aspect!r}")


_TOKEN_FIELDS: dict[str, type] = {
    "font_family": str,
    "font_size_px": int,
    "line_height": float,
    "char_width_em": float,
    "text_colour": str,
    "highlight_colour": str,
    "outline_colour": str,
    "outline_px": int,
}


def with_tokens(style: CaptionStyle, tokens: Mapping[str, object]) -> CaptionStyle:
    """Copy of style with house-token overrides; rejects unknown keys and unsafe sizes."""
    changes: dict[str, object] = {}
    for key, value in tokens.items():
        kind = _TOKEN_FIELDS.get(key)
        if kind is None:
            raise ValueError(f"unknown caption token {key!r}")
        if kind is str:
            if not isinstance(value, str):
                raise ValueError(f"caption token {key!r} must be a string")
            changes[key] = value
        else:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"caption token {key!r} must be a number")
            changes[key] = kind(value)
    return replace(style, **changes).validate()  # type: ignore[arg-type]


def block_rect(style: CaptionStyle, lines: list[str]) -> Rect:
    """Estimated rectangle of a cue: centred in the safe area, sitting on its bottom edge."""
    chars = max((len(line) for line in lines), default=0)
    width = chars * style.font_size_px * style.char_width_em
    height = len(lines) * style.line_px
    x = style.safe.x + (style.safe.w - width) / 2
    return Rect(x, style.safe.bottom - height, width, height)
