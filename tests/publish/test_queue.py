# SPDX-License-Identifier: AGPL-3.0-or-later
"""The approval queue: no adapter call without an approval record matching the payload hash."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlmodel import Session

from reelwright.db.models import Publication, PublicationFields, PublicationStatus, Render
from reelwright.providers.mocks import MockYouTube
from reelwright.publish import queue, youtube

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


class FailingThumbnail(MockYouTube):
    """Uploads fine, then fails on the thumbnail until ``broken`` is cleared."""

    broken: bool = True

    def set_thumbnail(self, video_id: str, path: str) -> None:
        if self.broken:
            raise RuntimeError("thumbnail rejected: image exceeds 2 MB")
        super().set_thumbnail(video_id, path)


def draft(session: Session, render: Render, **fields: Any) -> Publication:
    payload = youtube.build_payload(session, render, PublicationFields(title="My video", **fields))
    return queue.create_draft(session, render.project_id, render.id or 0, payload, NOW)


def test_payload_hash_is_canonical_and_stable() -> None:
    a = {"title": "Ünïcode", "tags": ["x", "y"], "n": 1}
    b = {"n": 1, "tags": ["x", "y"], "title": "Ünïcode"}
    assert queue.payload_hash(a) == queue.payload_hash(b)
    assert queue.payload_hash(a) != queue.payload_hash({**a, "n": 2})
    assert queue.payload_hash({}) == (
        "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    )


def test_no_approval_means_no_adapter_call(session: Session, render: Render) -> None:
    pub = draft(session, render)
    mock = MockYouTube()
    with pytest.raises(queue.NotApproved):
        queue.require_approval(session, pub)
    with pytest.raises(queue.NotApproved):
        youtube.publish(session, pub, mock)
    assert mock.uploads == [] and mock.thumbnails == [] and mock.captions == []


def test_payload_changed_after_approval_is_refused(session: Session, render: Render) -> None:
    pub = draft(session, render)
    mock = MockYouTube()
    queue.approve(session, pub, "yash", pub.payload_hash, NOW, mock)
    assert len(mock.uploads) == 1
    # Someone edits the stored payload behind the queue's back: the hash no longer matches.
    tampered = {**queue.load_payload(pub), "title": "Changed after review"}
    pub.payload = json.dumps(tampered)
    pub.remote_id = None
    with pytest.raises(queue.NotApproved):
        youtube.publish(session, pub, mock)
    # Even with the hash re-recorded, no approval carries the new hash.
    pub.payload_hash = queue.payload_hash(tampered)
    with pytest.raises(queue.NotApproved):
        youtube.publish(session, pub, mock)
    assert len(mock.uploads) == 1


def test_stale_hash_records_nothing_and_uploads_nothing(session: Session, render: Render) -> None:
    pub = draft(session, render)
    mock = MockYouTube()
    with pytest.raises(queue.StaleApproval):
        queue.approve(session, pub, "yash", "0" * 64, NOW, mock)
    assert queue.approvals(session, pub) == [] and mock.uploads == []
    assert pub.status == PublicationStatus.draft


def test_matching_approval_uploads_once_and_records_who_when_hash(
    session: Session, render: Render
) -> None:
    pub = draft(session, render, tags=["python", "ai"])
    mock = MockYouTube()
    shown = pub.payload_hash
    pub = queue.approve(session, pub, "yash", shown, NOW, mock)
    assert [u["id"] for u in mock.uploads] == ["mock-1"]
    assert mock.uploads[0]["title"] == "My video" and mock.uploads[0]["privacy"] == "public"
    assert (pub.status, pub.remote_id) == (PublicationStatus.live, "mock-1")
    [approval] = queue.approvals(session, pub)
    assert approval.approved_by == "yash"
    assert queue.utc(approval.approved_at) == NOW
    assert approval.payload_hash == shown and approval.action == "publish"
    with pytest.raises(queue.QueueConflict):  # a live publication is not approved twice
        queue.approve(session, pub, "yash", shown, NOW, mock)
    assert len(mock.uploads) == 1


def test_scheduled_then_refresh_live(session: Session, render: Render) -> None:
    pub = draft(session, render, publish_at=NOW + timedelta(days=1))
    mock = MockYouTube()
    pub = queue.approve(session, pub, "yash", pub.payload_hash, NOW, mock)
    assert pub.status == PublicationStatus.scheduled
    assert mock.uploads[0]["privacy"] == "private"
    assert mock.uploads[0]["publish_at"] == "2026-11-13T12:00:00+00:00"
    assert queue.approvals(session, pub)[0].action == "schedule"
    assert queue.refresh_live(session, NOW + timedelta(hours=1)) == 0
    assert queue.refresh_live(session, NOW + timedelta(days=1)) == 1
    session.refresh(pub)
    assert pub.status == PublicationStatus.live


def test_provider_error_fails_and_retry_reuses_the_approval(
    session: Session, render: Render
) -> None:
    pub = draft(session, render, thumbnail_path="D:/thumbs/t.png")
    mock = FailingThumbnail()
    with pytest.raises(queue.PublishFailed, match="exceeds 2 MB"):
        queue.approve(session, pub, "yash", pub.payload_hash, NOW, mock)
    session.refresh(pub)
    assert pub.status == PublicationStatus.failed
    assert pub.error == "thumbnail rejected: image exceeds 2 MB"
    assert pub.remote_id == "mock-1"  # the upload itself went through
    with pytest.raises(queue.QueueConflict):  # editing would orphan the uploaded video
        queue.update_draft(session, pub, queue.load_payload(pub), NOW)
    mock.broken = False
    pub = queue.retry(session, pub, mock, NOW)
    assert pub.status == PublicationStatus.live and pub.error is None
    assert len(mock.uploads) == 1  # not uploaded twice
    assert mock.thumbnails == [{"id": "mock-1", "path": "D:/thumbs/t.png"}]
    assert len(queue.approvals(session, pub)) == 1


def test_update_rehashes_and_resets_to_draft(session: Session, render: Render) -> None:
    pub = draft(session, render)
    old = pub.payload_hash
    pub = queue.update_draft(session, pub, {**queue.load_payload(pub), "title": "New"}, NOW)
    assert pub.payload_hash != old and pub.status == PublicationStatus.draft
    with pytest.raises(queue.StaleApproval):
        queue.approve(session, pub, "yash", old, NOW, MockYouTube())


def test_diff_against_the_last_sent_publication(session: Session, render: Render) -> None:
    first = draft(session, render, tags=["a"])
    fresh = {d.field: (d.before, d.after) for d in queue.diff(session, first)}
    assert fresh["title"] == (None, "My video") and fresh["tags"] == (None, ["a"])
    queue.approve(session, first, "yash", first.payload_hash, NOW, MockYouTube())
    second = draft(session, render, tags=["a", "b"], description="More")
    changed = {d.field: (d.before, d.after) for d in queue.diff(session, second)}
    assert changed["tags"] == (["a"], ["a", "b"])
    assert "title" not in changed and "description" in changed
