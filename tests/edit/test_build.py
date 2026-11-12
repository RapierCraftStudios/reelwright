# SPDX-License-Identifier: AGPL-3.0-or-later
"""The exported OTIO is what Kdenlive reads, and it matches the internal timeline."""

from __future__ import annotations

import importlib
from typing import Any

from spectracut.edit import FPS_30, from_otio

from reelwright.edit import CutLine, Span, build_edit, edit_to_otio_json

otio: Any = importlib.import_module("opentimelineio")

MEDIA = {1: "D:/footage/take-a.mp4", 2: "D:/footage/take-b.mp4"}
LINES = [
    CutLine("hook", 6, "Today we test.", [Span(1, 1.0, 2.5), Span(1, 3.0, 4.0)]),
    CutLine("cta", 9, "Subscribe.", [Span(2, 10.0, 11.5)]),
    CutLine("outro", 12, "Bye.", [Span(2, 20.0, 20.01)]),  # under a frame: dropped
]


def build() -> Any:
    return build_edit("My Video", LINES, MEDIA, FPS_30)


def test_otio_round_trips_to_the_internal_timeline() -> None:
    edit = build()
    timeline = otio.adapters.read_from_string(edit_to_otio_json(edit), "otio_json")
    assert isinstance(timeline, otio.schema.Timeline)
    assert from_otio(timeline) == edit


def test_clips_match_the_spans() -> None:
    edit = build()
    video = next(t for t in edit.tracks if t.kind == "video")
    ranges = [(c.media_ref, c.source_in, c.source_out) for c in video.clips()]
    assert ranges == [(MEDIA[1], 30, 75), (MEDIA[1], 90, 120), (MEDIA[2], 300, 345)]
    assert edit.duration == 45 + 30 + 45
    audio = next(t for t in edit.tracks if t.kind == "audio")
    assert [c.link for c in audio.clips()] == ["v0001", "v0002", "v0003"]
    assert [(m.name, m.frame) for m in edit.markers] == [("hook:6", 0), ("cta:9", 75)]


def test_otio_is_plain_enough_for_kdenlive() -> None:
    timeline = otio.adapters.read_from_string(edit_to_otio_json(build()), "otio_json")
    kinds = [t.kind for t in timeline.tracks]
    assert kinds == [otio.schema.TrackKind.Video, otio.schema.TrackKind.Audio]
    for track in timeline.tracks:
        for item in track:
            assert isinstance(item, otio.schema.Clip)
            assert isinstance(item.media_reference, otio.schema.ExternalReference)
            assert item.media_reference.target_url in MEDIA.values()
            assert list(item.effects) == []
            assert item.source_range.start_time.rate == 30
    assert timeline.duration().to_seconds() == 4.0


def test_edit_id_is_a_slug() -> None:
    assert build().id == "my-video"
