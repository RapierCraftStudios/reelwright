# SPDX-License-Identifier: AGPL-3.0-or-later
"""The approval queue: draft → approved → scheduled → live (or failed), one row per upload.

Every approval stores who approved, when and the sha256 of the exact payload they saw
(spike § Security). ``require_approval`` is the gate the adapters call before any platform call:
no Approval row with the payload's current hash, no call. The client approves by sending back the
hash it displayed, so a payload edited after review can never be approved by an old click.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, col, select

from reelwright.db.models import (
    Approval,
    FieldDiff,
    Publication,
    PublicationStatus,
    Render,
)

DIFF_FIELDS = (
    "title",
    "description",
    "tags",
    "thumbnail_path",
    "privacy",
    "publish_at",
    "paid_promotion",
    "sponsor",
    "affiliate_links",
    "chapters",
    "captions_cues",
)
EDITABLE = frozenset({PublicationStatus.draft, PublicationStatus.failed})
SENT = (PublicationStatus.approved, PublicationStatus.scheduled, PublicationStatus.live)


class NotApproved(RuntimeError):
    """No approval record matches the payload hash: the adapter must not be called."""


class QueueConflict(ValueError):
    """The request does not fit the publication's state (maps to HTTP 409)."""


class StaleApproval(QueueConflict):
    """The approved hash is not the payload's current hash (HTTP 409, nothing recorded)."""


class PublishFailed(RuntimeError):
    """The platform call failed; the message is the platform's error."""


def utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def payload_hash(payload: Mapping[str, Any]) -> str:
    """sha256 of the canonical JSON of a payload (sorted keys, compact separators)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def load_payload(publication: Publication) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(publication.payload)
    return payload


def set_payload(publication: Publication, payload: Mapping[str, Any], now: datetime) -> None:
    publication.payload = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    publication.payload_hash = payload_hash(payload)
    at = payload.get("publish_at")
    publication.publish_at = datetime.fromisoformat(at) if at else None
    publication.updated_at = now


def approvals(session: Session, publication: Publication) -> list[Approval]:
    return list(
        session.exec(
            select(Approval)
            .where(Approval.publication_id == publication.id)
            .order_by(col(Approval.id))
        ).all()
    )


def require_approval(session: Session, publication: Publication) -> Approval:
    """The approval matching the stored payload's hash, else ``NotApproved``."""
    current = payload_hash(load_payload(publication))
    if current != publication.payload_hash:
        raise NotApproved("payload changed since its hash was recorded")
    for approval in reversed(approvals(session, publication)):
        if approval.payload_hash == current:
            return approval
    raise NotApproved(f"publication {publication.id} has no approval for payload {current[:12]}")


def create_draft(
    session: Session, project_id: int, render_id: int, payload: Mapping[str, Any], now: datetime
) -> Publication:
    publication = Publication(
        project_id=project_id,
        render_id=render_id,
        platform=str(payload.get("platform", "youtube")),
        payload="{}",
        payload_hash="",
        created_at=now,
    )
    set_payload(publication, payload, now)
    session.add(publication)
    session.commit()
    session.refresh(publication)
    return publication


def update_draft(
    session: Session, publication: Publication, payload: Mapping[str, Any], now: datetime
) -> Publication:
    """Replace a draft's payload; a failed one goes back to draft (its old approval no longer
    matches the new hash)."""
    if publication.status not in EDITABLE:
        raise QueueConflict(f"a {publication.status} publication can no longer be edited")
    if publication.remote_id:
        raise QueueConflict("the video is already uploaded; retry to finish sending it")
    set_payload(publication, payload, now)
    publication.status = PublicationStatus.draft
    publication.error = None
    session.add(publication)
    session.commit()
    session.refresh(publication)
    return publication


def _ready(session: Session, publication: Publication) -> Render:
    render = session.get(Render, publication.render_id)
    if render is None or not render.output_path:
        raise QueueConflict("the render has no output yet; wait for it to finish")
    return render


def approve(
    session: Session,
    publication: Publication,
    approved_by: str,
    shown_hash: str,
    now: datetime,
    provider: Any,
) -> Publication:
    """Record the approval of the payload the human saw, then send it to the platform."""
    from reelwright.publish import youtube

    if publication.status not in EDITABLE:
        raise QueueConflict(f"a {publication.status} publication was already approved")
    if shown_hash != publication.payload_hash:
        raise StaleApproval("the payload changed since it was shown; review it again")
    _ready(session, publication)
    scheduled = publication.publish_at is not None and utc(publication.publish_at) > now
    session.add(
        Approval(
            publication_id=publication.id or 0,
            approved_by=approved_by,
            approved_at=now,
            payload_hash=shown_hash,
            action="schedule" if scheduled else "publish",
        )
    )
    publication.status = PublicationStatus.approved
    publication.error = None
    publication.updated_at = now
    session.add(publication)
    session.commit()
    session.refresh(publication)
    return _send(session, publication, provider, now, youtube.publish)


def retry(session: Session, publication: Publication, provider: Any, now: datetime) -> Publication:
    """Send a failed publication again under its existing approval."""
    from reelwright.publish import youtube

    if publication.status != PublicationStatus.failed:
        raise QueueConflict(
            f"only a failed publication can be retried (this one is {publication.status})"
        )
    _ready(session, publication)
    return _send(session, publication, provider, now, youtube.publish)


def _send(
    session: Session, publication: Publication, provider: Any, now: datetime, publish: Any
) -> Publication:
    try:
        publish(session, publication, provider)
    except NotApproved:
        raise
    except Exception as e:  # the platform's own error goes to the dashboard
        publication.status = PublicationStatus.failed
        publication.error = (str(e) or type(e).__name__)[:2000]
        publication.updated_at = now
        session.add(publication)
        session.commit()
        session.refresh(publication)
        raise PublishFailed(publication.error) from e
    at = publication.publish_at
    publication.status = (
        PublicationStatus.scheduled if at is not None and utc(at) > now else PublicationStatus.live
    )
    publication.error = None
    publication.updated_at = now
    session.add(publication)
    session.commit()
    session.refresh(publication)
    return publication


def refresh_live(session: Session, now: datetime) -> int:
    """Scheduled publications whose time has come are live (the platform publishes them)."""
    rows = session.exec(
        select(Publication).where(Publication.status == PublicationStatus.scheduled)
    ).all()
    moved = 0
    for row in rows:
        if row.publish_at is not None and utc(row.publish_at) <= now:
            row.status = PublicationStatus.live
            row.updated_at = now
            session.add(row)
            moved += 1
    if moved:
        session.commit()
    return moved


def diff(session: Session, publication: Publication) -> list[FieldDiff]:
    """Changed fields against the last approved payload of the same project and platform."""
    previous = session.exec(
        select(Publication)
        .where(
            Publication.project_id == publication.project_id,
            Publication.platform == publication.platform,
            Publication.id != publication.id,
            col(Publication.status).in_([s.value for s in SENT]),
        )
        .order_by(col(Publication.id).desc())
        .limit(1)
    ).first()
    after = load_payload(publication)
    before = load_payload(previous) if previous is not None else {}
    return [
        FieldDiff(field=name, before=before.get(name), after=after.get(name))
        for name in DIFF_FIELDS
        if before.get(name) != after.get(name)
    ]
