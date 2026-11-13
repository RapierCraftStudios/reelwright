# SPDX-License-Identifier: AGPL-3.0-or-later
"""Caption timings follow the edit: cuts, speed and transitions move words onto record time."""

from __future__ import annotations

import re

import pytest
from spectracut.edit import FPS_24, Clip, Edit, SpeedRamp, Track, Transition

from reelwright.captions import cues, edit_words, style_for, to_ass, to_srt, to_vtt
from reelwright.captions.build import CaptionWord
from reelwright.db.models import TranscriptWord


def _w(text: str, start: float, end: float) -> TranscriptWord:
    return TranscriptWord(word=text, start=start, end=end, confidence=1.0)


# One word every 0.5 s for 10 s of source media, each 0.4 s long.
WORDS = [_w(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(20)]


def _edit(*items: Clip | Transition, kind: str = "audio") -> Edit:
    return Edit(id="e1", rate=FPS_24, tracks=[Track(id="t1", kind=kind, items=list(items))])


def _cut_edit() -> Edit:
    # Keep 0-2 s and 4-8 s of a.mov: the 2 s in between are cut out.
    return _edit(
        Clip(id="c1", media_ref="a.mov", source_in=0, source_out=48),
        Clip(id="c2", media_ref="a.mov", source_in=96, source_out=192),
    )


def _times(words: list[CaptionWord]) -> dict[str, tuple[float, float]]:
    return {w.text: (round(w.start, 6), round(w.end, 6)) for w in words}


def test_cut_shifts_later_words_by_the_removed_duration() -> None:
    got = _times(edit_words(_cut_edit(), {"a.mov": WORDS}))
    assert got["w0"] == (0.0, 0.4)
    assert got["w3"] == (1.5, 1.9)
    # w8 starts at 4.0 s in the source; 2 s were cut before it.
    assert got["w8"] == (2.0, 2.4)
    assert got["w15"] == (5.5, 5.9)
    for i in range(4, 8):  # 2.0-3.9 s was cut
        assert f"w{i}" not in got
    for i in range(16, 20):  # after 8 s is not in the edit
        assert f"w{i}" not in got
    starts = [w.start for w in edit_words(_cut_edit(), {"a.mov": WORDS})]
    assert starts == sorted(starts)


def test_reordered_clips_follow_the_record_order() -> None:
    edit = _edit(
        Clip(id="c2", media_ref="a.mov", source_in=96, source_out=144),
        Clip(id="c1", media_ref="a.mov", source_in=0, source_out=48),
    )
    got = _times(edit_words(edit, {"a.mov": WORDS}))
    assert got["w8"] == (0.0, 0.4)
    assert got["w0"] == (2.0, 2.4)


def test_word_straddling_a_cut_is_clamped_or_dropped_by_midpoint() -> None:
    words = [_w("kept", 1.7, 2.1), _w("gone", 1.9, 2.5), _w("tail", 3.9, 4.3)]
    got = _times(edit_words(_cut_edit(), {"a.mov": words}))
    assert got["kept"] == (1.7, 2.0)  # midpoint 1.9 in c1, end clamped to the cut at 2.0
    assert "gone" not in got  # midpoint 2.2 lies in the removed section
    assert got["tail"] == (2.0, 2.3)  # midpoint 4.1 in c2, start clamped to c2's in point


def test_speed_scales_offsets_and_the_covered_media() -> None:
    edit = _edit(Clip(id="c1", media_ref="a.mov", source_in=0, source_out=48, speed=2.0))
    got = _times(edit_words(edit, {"a.mov": WORDS}))
    assert got["w2"] == (0.5, 0.7)
    assert got["w6"] == (1.5, 1.7)  # 3.0 s of media plays at 1.5 s
    assert "w8" not in got  # 2 s at 2x covers 4 s of media


def test_transition_does_not_shift_record_time() -> None:
    edit = _edit(
        Clip(id="c1", media_ref="a.mov", source_in=0, source_out=48),
        Transition(in_offset=6, out_offset=6),
        Clip(id="c2", media_ref="a.mov", source_in=96, source_out=192),
    )
    assert _times(edit_words(edit, {"a.mov": WORDS})) == _times(
        edit_words(_cut_edit(), {"a.mov": WORDS})
    )


def test_video_track_is_used_without_audio_and_other_media_is_ignored() -> None:
    edit = _edit(
        Clip(id="c1", media_ref="b.mov", source_in=0, source_out=24),
        Clip(id="c2", media_ref="a.mov", source_in=0, source_out=24),
        kind="video",
    )
    got = _times(edit_words(edit, {"a.mov": WORDS}))
    assert got == {"w0": (1.0, 1.4), "w1": (1.5, 1.9)}


def test_speed_ramps_are_rejected() -> None:
    ramp = SpeedRamp(start=0, end=10, speed_from=1.0, speed_to=2.0)
    edit = _edit(Clip(id="c1", media_ref="a.mov", source_in=0, source_out=48, ramps=[ramp]))
    with pytest.raises(ValueError, match="ramps"):
        edit_words(edit, {"a.mov": WORDS})


def test_srt_and_vtt_carry_the_edited_times() -> None:
    style = style_for("16:9")
    words = edit_words(_cut_edit(), {"a.mov": WORDS})
    track = cues(words, style)
    srt = to_srt(track)
    assert srt.startswith("1\n00:00:00,000 --> ")
    assert re.search(r" --> 00:00:05,900\n[^\n]*w15\n", srt)
    # The part after the cut, captioned alone, starts where the cut put it.
    after = cues([w for w in words if w.start >= 2.0], style)
    assert to_srt(after).startswith("1\n00:00:02,000 --> 00:00:05,900\nw8 w9 w10")
    assert to_vtt(after).startswith("WEBVTT\n\n00:00:02.000 --> 00:00:05.900\nw8 w9")
    numbers = [int(n) for n in re.findall(r"^(\d+)$", srt, flags=re.M)]
    assert numbers == list(range(1, len(track) + 1))


def test_cues_break_at_long_pauses_and_never_overlap() -> None:
    words = [CaptionWord("a", 0.0, 0.3), CaptionWord("b", 0.4, 0.6), CaptionWord("c", 3.0, 3.2)]
    track = cues(words, style_for("16:9"))
    assert [c.lines for c in track] == [["a b"], ["c"]]
    assert all(a.end <= b.start for a, b in zip(track, track[1:], strict=False))


def test_ass_karaoke_tags_sum_to_each_cue() -> None:
    style = style_for("9:16")
    track = cues(edit_words(_cut_edit(), {"a.mov": WORDS}), style)
    ass = to_ass(track, style)
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(events) == len(track)
    for cue, event in zip(track, events, strict=True):
        ks = [int(k) for k in re.findall(r"\{\\k(\d+)\}", event)]
        assert len(ks) == len(cue.words)
        assert abs(sum(ks) - round((cue.end - cue.start) * 100)) <= 1
    assert ",0:00:05.90,Caption," in ass
    # w8 is highlighted at 2.0 s, right after the cut: the tags before it sum to 200 cs.
    first = events[0].split(",", 9)[9]
    before_w8 = first.split("w8")[0].rsplit("{", 1)[0]
    assert sum(int(k) for k in re.findall(r"\{\\k(\d+)\}", before_w8)) == 200
