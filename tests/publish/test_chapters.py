# SPDX-License-Identifier: AGPL-3.0-or-later
"""Chapters come from the beat start times in the final edit and follow YouTube's rules."""

from __future__ import annotations

from reelwright.edit.build import edit_to_otio_json
from reelwright.publish.chapters import Chapter, beat_starts, chapters, format_chapters
from reelwright.render.split import parse_otio

from .fixtures import beat_edit


def test_first_marker_per_beat_in_frame_order() -> None:
    assert beat_starts(beat_edit()) == [("hook", 15), ("problem", 900), ("outro", 1800)]


def test_chapters_start_at_zero_and_use_beat_titles() -> None:
    got = chapters(beat_edit(), {"hook": "Why this matters", "outro": "Wrap up"})
    assert got == [
        Chapter(start_s=0.0, title="Why this matters"),  # clamped from frame 15 to 0:00
        Chapter(start_s=30.0, title="Problem"),  # no script title: title-cased slug
        Chapter(start_s=60.0, title="Wrap up"),
    ]


def test_chapters_survive_the_otio_round_trip() -> None:
    edit = parse_otio(edit_to_otio_json(beat_edit()))
    assert [c.start_s for c in chapters(edit)] == [0.0, 30.0, 60.0]


def test_fewer_than_three_beats_gives_no_chapters() -> None:
    assert chapters(beat_edit([("hook:1", 0), ("outro:4", 900)])) == []


def test_a_chapter_shorter_than_ten_seconds_gives_no_chapters() -> None:
    short_middle = beat_edit([("a:1", 0), ("b:2", 900), ("c:3", 1100)])  # b lasts 6.7 s
    assert chapters(short_middle) == []
    short_last = beat_edit([("a:1", 0), ("b:2", 900), ("c:3", 2600)])  # c runs to 90 s: 3.3 s
    assert chapters(short_last) == []


def test_markers_without_a_beat_slug_are_ignored() -> None:
    edit = beat_edit([("note", 0), ("a:1", 0), ("b:2", 900), (":3", 1000), ("c:3", 1800)])
    assert [slug for slug, _ in beat_starts(edit)] == ["a", "b", "c"]


def test_format_chapters() -> None:
    items = [Chapter(0.0, "Intro"), Chapter(75.5, "Middle"), Chapter(600.0, "End")]
    assert format_chapters(items) == "0:00 Intro\n1:15 Middle\n10:00 End"
    long = [Chapter(0.0, "A"), Chapter(3725.0, "B")]
    assert format_chapters(long) == "0:00:00 A\n1:02:05 B"
