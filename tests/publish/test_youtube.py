# SPDX-License-Identifier: AGPL-3.0-or-later
"""The YouTube adapter: payload contents, disclosures and the calls made on the mock."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session

from reelwright.db.models import AffiliateLink, Asset, PublicationFields, Render, Transcript
from reelwright.providers.mocks import MockYouTube
from reelwright.publish import queue, youtube

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
TITLES = {"hook": "Why this matters", "problem": "The problem", "outro": "Wrap up"}


def build(session: Session, render: Render, **fields: object) -> dict[str, object]:
    return youtube.build_payload(
        session, render, PublicationFields.model_validate({"title": "T", **fields}), TITLES
    )


def add_transcript(session: Session) -> None:
    words = [
        {"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4, "confidence": 1.0}
        for i, w in enumerate(["hello", "and", "welcome", "back"])
    ]
    asset = Asset(path="D:/f/a.mp4", checksum="c" * 64, kind="video")
    session.add(asset)
    session.commit()
    session.refresh(asset)
    row = Transcript(asset_id=asset.id or 0, words=json.dumps(words), word_count=len(words))
    session.add(row)
    session.commit()
    session.refresh(row)
    asset.transcript_id = row.id
    session.add(asset)
    session.commit()


def test_description_has_body_chapters_and_affiliate_block(
    session: Session, render: Render
) -> None:
    link = AffiliateLink(label="Mic", url="https://example.com/mic")
    payload = build(session, render, description="All about it.", affiliate_links=[link])
    assert payload["description"] == (
        "All about it.\n\n"
        "Chapters\n0:00 Why this matters\n0:30 The problem\n1:00 Wrap up\n\n"
        "Affiliate links: I may earn a commission.\nMic: https://example.com/mic"
    )
    assert payload["chapters"] == [
        {"start_s": 0.0, "title": "Why this matters"},
        {"start_s": 30.0, "title": "The problem"},
        {"start_s": 60.0, "title": "Wrap up"},
    ]
    assert payload["media_path"] == "D:\\out\\final.mp4"


@pytest.mark.parametrize(
    "fields",
    [
        {"sponsor": "Acme"},
        {"affiliate_links": [{"label": "Mic", "url": "https://example.com/mic"}]},
    ],
)
def test_sponsor_or_affiliate_forces_paid_promotion(
    session: Session, render: Render, fields: dict[str, object]
) -> None:
    payload = build(session, render, paid_promotion=False, **fields)
    assert payload["paid_promotion"] is True
    description = str(payload["description"])
    assert "Sponsored by Acme" in description or "I may earn a commission" in description
    # Re-deriving the editable fields cannot switch the flag off either.
    again = youtube.payload_fields(payload).model_dump()
    rebuilt = build(session, render, **{**again, "paid_promotion": False})
    assert rebuilt["paid_promotion"] is True


def test_plain_video_is_public_without_paid_promotion(session: Session, render: Render) -> None:
    payload = build(session, render)
    assert (payload["paid_promotion"], payload["privacy"], payload["publish_at"]) == (
        False,
        "public",
        None,
    )


def test_scheduled_payload_is_private_with_utc_publish_at(session: Session, render: Render) -> None:
    at = (NOW + timedelta(days=2)).astimezone(UTC)
    payload = build(session, render, publish_at=at)
    assert payload["privacy"] == "private"
    assert payload["publish_at"] == "2026-11-14T12:00:00+00:00"


def test_captions_srt_comes_from_the_asset_transcripts(session: Session, render: Render) -> None:
    assert build(session, render)["captions_srt"] == ""
    add_transcript(session)
    payload = build(session, render)
    srt = str(payload["captions_srt"])
    assert srt.startswith("1\n00:00:00,000 --> ")
    assert "hello" in srt and "back" in srt
    assert payload["captions_cues"] >= 1  # type: ignore[operator]


def test_publish_sends_video_thumbnail_and_captions(session: Session, render: Render) -> None:
    add_transcript(session)
    payload = build(session, render, thumbnail_path="D:/thumbs/t.png", tags=["ai"])
    pub = queue.create_draft(session, render.project_id, render.id or 0, payload, NOW)
    mock = MockYouTube()
    with pytest.raises(queue.NotApproved):
        youtube.publish(session, pub, mock)
    assert mock.uploads == [] and mock.thumbnails == [] and mock.captions == []

    pub = queue.approve(session, pub, "yash", pub.payload_hash, NOW, mock)
    [upload] = mock.uploads
    assert upload["path"] == "D:\\out\\final.mp4" and upload["tags"] == ["ai"]
    assert upload["description"] == payload["description"]
    assert upload["paid_promotion"] is False
    assert mock.thumbnails == [{"id": "mock-1", "path": "D:/thumbs/t.png"}]
    [caption] = mock.captions
    assert caption["id"] == "mock-1" and caption["language"] == "en"
    assert caption["srt"] == payload["captions_srt"]
    assert pub.remote_id == "mock-1"
