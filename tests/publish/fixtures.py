# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test data for the publish tests: an edit with beat markers."""

from __future__ import annotations

from spectracut.edit import Clip, Edit, FrameRate, Marker, Track


def beat_edit(markers: list[tuple[str, int]] | None = None, seconds: int = 90) -> Edit:
    """A 30 fps edit of ``seconds`` with one marker per (name, frame)."""
    frames = seconds * 30
    names = markers if markers is not None else [
        ("hook:2", 15), ("hook:3", 300), ("problem:5", 900), ("problem:6", 1200), ("outro:9", 1800),
    ]  # fmt: skip
    return Edit(
        id="e1",
        rate=FrameRate(numerator=30),
        tracks=[
            Track(
                id="v1",
                items=[Clip(id="c1", media_ref="D:/f/a.mp4", source_in=0, source_out=frames)],
            )
        ],
        markers=[Marker(name=n, frame=f) for n, f in names],
    )
