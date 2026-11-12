# SPDX-License-Identifier: AGPL-3.0-or-later
"""Word-timed caption tracks from the final edit.

`edit_words` moves transcript words (seconds from the start of their source media) onto the
edit's record time, so cuts shift them. `cues` packs them into readable lines, and the writers
emit SRT and WebVTT for YouTube upload and an ASS script with per-word karaoke highlight for
burn-in (ffmpeg's `ass` filter, in the render graph). Nothing here renders or uploads.

Edit times follow OpenTimelineIO semantics (spectracut.edit): a clip occupies
`source_out - source_in` frames on its track and plays its media from `source_in` at `speed`,
so it covers media frames `[source_in, source_in + duration * speed)`. Caption times are
relative to the start of the rendered file, so the edit's `start_frame` is ignored.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from reelwright.captions.style import CaptionStyle
from reelwright.db.models import TranscriptWord

MIN_CUE_S = 0.04
"""Shortest cue written, so zero-length words still show for a frame or so."""


@dataclass(frozen=True)
class CaptionWord:
    """One word on the edit's record time, in seconds from the start of the output."""

    text: str
    start: float
    end: float


@dataclass
class Cue:
    start: float
    end: float
    lines: list[str] = field(default_factory=list)
    words: list[CaptionWord] = field(default_factory=list)
    line_sizes: list[int] = field(default_factory=list)
    """Number of words on each line."""


def _caption_tracks(edit: Any) -> list[Any]:
    audio = [t for t in edit.tracks if t.kind == "audio"]
    return audio or [t for t in edit.tracks if t.kind == "video"]


def edit_words(
    edit: Any, words_by_media: Mapping[str, Sequence[TranscriptWord]]
) -> list[CaptionWord]:
    """Words of the edit's clips (keyed by clip media_ref) on the edit's record time.

    A word belongs to a clip when its midpoint falls inside the media the clip plays; its
    edges are clamped to that range, so words trimmed at a cut keep only their kept part.
    Words cut out of the edit are dropped. Speed ramps and nested edits are not supported.
    """
    fps = edit.rate.fps
    seen: set[tuple[str, float]] = set()
    out: list[CaptionWord] = []
    for track in _caption_tracks(edit):
        for clip in track.clips():
            words = words_by_media.get(clip.media_ref) if clip.media_ref else None
            if clip.nested is not None:
                raise ValueError(f"clip {clip.id}: nested edits are not supported for captions")
            if not words:
                continue
            if clip.ramps:
                raise ValueError(f"clip {clip.id}: speed ramps are not supported for captions")
            speed = Fraction(clip.speed)
            if speed <= 0:
                raise ValueError(f"clip {clip.id}: speed must be positive for captions")
            rec0 = Fraction(track.record_start(clip.id))
            src_in = Fraction(clip.source_in)
            src_out = src_in + clip.duration * speed
            for w in words:
                s, e = Fraction(w.start) * fps, Fraction(w.end) * fps
                mid = (s + e) / 2
                if not src_in <= mid < src_out:
                    continue
                rs = rec0 + (max(s, src_in) - src_in) / speed
                re = rec0 + (min(e, src_out) - src_in) / speed
                word = CaptionWord(w.word, float(rs / fps), float(re / fps))
                key = (word.text, round(word.start, 6))
                if key in seen:
                    continue
                seen.add(key)
                out.append(word)
    out.sort(key=lambda w: (w.start, w.end))
    return out


def _split_long(word: CaptionWord, limit: int) -> list[CaptionWord]:
    if len(word.text) <= limit:
        return [word]
    pieces = [word.text[i : i + limit] for i in range(0, len(word.text), limit)]
    step = (word.end - word.start) / len(pieces)
    return [
        CaptionWord(p, word.start + n * step, word.start + (n + 1) * step)
        for n, p in enumerate(pieces)
    ]


def cues(words: Iterable[CaptionWord], style: CaptionStyle) -> list[Cue]:
    """Pack words into cues of at most style.max_lines lines of at most style.max_chars."""
    limit = style.max_chars
    out: list[Cue] = []
    cur: Cue | None = None
    for original in words:
        for word in _split_long(original, limit):
            if cur is not None and word.start - cur.words[-1].end > style.max_gap_s:
                cur = None
            if cur is not None:
                last = cur.lines[-1]
                if len(last) + 1 + len(word.text) <= limit:
                    cur.lines[-1] = f"{last} {word.text}"
                    cur.line_sizes[-1] += 1
                elif len(cur.lines) < style.max_lines:
                    cur.lines.append(word.text)
                    cur.line_sizes.append(1)
                else:
                    cur = None
            if cur is None:
                cur = Cue(word.start, word.end, [word.text], [], [1])
                out.append(cur)
            cur.words.append(word)
            cur.end = max(cur.end, word.end)
    for n, cue in enumerate(out):
        end = max(cue.end, cue.start + MIN_CUE_S)
        if n + 1 < len(out):
            end = min(end, out[n + 1].start)
        cue.end = max(end, cue.start)
    return out


def _ms(t: float) -> int:
    return max(round(t * 1000), 0)


def _clock(t: float, sep: str) -> str:
    ms = _ms(t)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(items: Sequence[Cue]) -> str:
    blocks = [
        f"{n}\n{_clock(c.start, ',')} --> {_clock(c.end, ',')}\n" + "\n".join(c.lines)
        for n, c in enumerate(items, start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def to_vtt(items: Sequence[Cue]) -> str:
    blocks = [
        f"{_clock(c.start, '.')} --> {_clock(c.end, '.')}\n" + "\n".join(c.lines) for c in items
    ]
    return "WEBVTT\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")


def _ass_colour(hex_rgb: str) -> str:
    rgb = hex_rgb.lstrip("#")
    if len(rgb) != 6:
        raise ValueError(f"colour must be #RRGGBB, got {hex_rgb!r}")
    r, g, b = rgb[0:2], rgb[2:4], rgb[4:6]
    return f"&H00{b}{g}{r}".upper()


def _cs(t: float) -> int:
    return max(round(t * 100), 0)


def _ass_clock(cs: int) -> str:
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def _karaoke(cue: Cue) -> str:
    """Cue text with a {\\k} per word; tags sum to the cue's length in centiseconds."""
    marks = [_cs(w.start) for w in cue.words[1:]] + [_cs(cue.end)]
    at = _cs(cue.start)
    words = iter(cue.words)
    marks_iter = iter(marks)
    rendered: list[str] = []
    for size in cue.line_sizes:
        parts: list[str] = []
        for _ in range(size):
            word = next(words)
            mark = max(next(marks_iter), at)
            parts.append(f"{{\\k{mark - at}}}{_ass_escape(word.text)}")
            at = mark
        rendered.append(" ".join(parts))
    return "\\N".join(rendered)


def to_ass(items: Sequence[Cue], style: CaptionStyle) -> str:
    """ASS script for burn-in: text sits inside the safe area and fills word by word.

    Words not yet spoken use the text colour (SecondaryColour); each turns the highlight
    colour (PrimaryColour) as it is spoken.
    """
    margin_l = round(style.safe.x)
    margin_r = round(style.width - style.safe.right)
    margin_v = round(style.height - style.safe.bottom)
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {style.width}",
        f"PlayResY: {style.height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Caption,{style.font_family},{style.font_size_px},"
        f"{_ass_colour(style.highlight_colour)},{_ass_colour(style.text_colour)},"
        f"{_ass_colour(style.outline_colour)},&H80000000,-1,0,0,0,100,100,0,0,1,"
        f"{style.outline_px},0,2,{margin_l},{margin_r},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events = [
        f"Dialogue: 0,{_ass_clock(_cs(c.start))},{_ass_clock(max(_cs(c.end), _cs(c.start)))},"
        f"Caption,,0,0,0,,{_karaoke(c)}"
        for c in items
    ]
    return "\n".join(header + events) + "\n"
