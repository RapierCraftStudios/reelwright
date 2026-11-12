# SPDX-License-Identifier: AGPL-3.0-or-later
"""The YouTube adapter: build the upload payload, then send it only under a matching approval.

The payload is everything that goes to YouTube: title, description (with chapters from the beats
and the affiliate block), tags, captions (SRT built from the edit and its transcripts), thumbnail,
privacy and scheduled time, and the paid-promotion flag. Sponsor or affiliate content always gets
the paid-promotion flag and a disclosure (spike § Guardrails). Until the OAuth client exists (#16)
the provider is ``MockYouTube``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlmodel import Session, col, select

from reelwright.captions import cues, edit_words, style_for, to_srt
from reelwright.db.models import (
    AffiliateLink,
    Asset,
    Publication,
    PublicationFields,
    Render,
    Transcript,
    TranscriptWord,
)
from reelwright.ingest.transcript import transcript_to_read
from reelwright.publish import queue
from reelwright.publish.chapters import chapters, format_chapters
from reelwright.render.split import parse_otio

PLATFORM = "youtube"
AFFILIATE_DISCLOSURE = "Affiliate links: I may earn a commission if you buy through these links."
CAPTION_LANGUAGE = "en"


class YouTubeProvider(Protocol):
    def upload(self, path: str, title: str, **meta: Any) -> str: ...

    def set_thumbnail(self, video_id: str, path: str) -> None: ...

    def insert_caption(self, video_id: str, srt: str, language: str) -> None: ...


def sponsor_disclosure(sponsor: str) -> str:
    return f"Sponsored by {sponsor}. This video includes paid promotion."


def compose_description(
    body: str,
    chapter_block: str,
    sponsor: str | None,
    links: Sequence[AffiliateLink],
) -> str:
    parts = []
    if sponsor:
        parts.append(sponsor_disclosure(sponsor))
    if body.strip():
        parts.append(body.strip())
    if chapter_block:
        parts.append(f"Chapters\n{chapter_block}")
    if links:
        lines = "\n".join(f"{link.label}: {link.url}" for link in links)
        parts.append(f"{AFFILIATE_DISCLOSURE}\n{lines}")
    return "\n\n".join(parts)


def captions_srt(session: Session, edit: Any) -> tuple[str, int]:
    """SRT of the edit's speech from its assets' transcripts, and the cue count ('' and 0
    when no clip has a transcript)."""
    refs = {
        clip.media_ref for track in edit.tracks for clip in track.clips() if clip.media_ref
    }
    if not refs:
        return "", 0
    words: dict[str, list[TranscriptWord]] = {}
    for asset in session.exec(select(Asset).where(col(Asset.path).in_(sorted(refs)))).all():
        row = session.get(Transcript, asset.transcript_id) if asset.transcript_id else None
        if row is not None:
            words[asset.path] = transcript_to_read(row).words
    if not words:
        return "", 0
    items = cues(edit_words(edit, words), style_for("16:9"))
    return (to_srt(items), len(items)) if items else ("", 0)


def build_payload(
    session: Session,
    render: Render,
    fields: PublicationFields,
    beat_titles: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The canonical upload payload for a render; its hash is what an approval signs."""
    edit = parse_otio(render.otio)
    found = chapters(edit, beat_titles)
    srt, n_cues = captions_srt(session, edit)
    publish_at = fields.publish_at.astimezone(UTC).isoformat() if fields.publish_at else None
    links = list(fields.affiliate_links)
    return {
        "platform": PLATFORM,
        "render_id": render.id,
        "media_path": render.output_path or render.target_path,
        "title": fields.title,
        "description_body": fields.description,
        "description": compose_description(
            fields.description, format_chapters(found), fields.sponsor, links
        ),
        "chapters": [{"start_s": c.start_s, "title": c.title} for c in found],
        "tags": list(fields.tags),
        "captions_srt": srt,
        "captions_cues": n_cues,
        "thumbnail_path": fields.thumbnail_path,
        "publish_at": publish_at,
        "privacy": "private" if publish_at else "public",
        # Sponsor and affiliate content always carries the platform's paid-promotion flag.
        "paid_promotion": bool(fields.paid_promotion or fields.sponsor or links),
        "sponsor": fields.sponsor,
        "affiliate_links": [link.model_dump() for link in links],
    }


def payload_fields(payload: Mapping[str, Any]) -> PublicationFields:
    """The editable inputs a payload was built from (to apply a draft update on top)."""
    at = payload.get("publish_at")
    return PublicationFields(
        title=payload["title"],
        description=payload.get("description_body", ""),
        tags=list(payload.get("tags", [])),
        thumbnail_path=payload.get("thumbnail_path"),
        publish_at=datetime.fromisoformat(at) if at else None,
        paid_promotion=bool(payload.get("paid_promotion")) and not (
            payload.get("sponsor") or payload.get("affiliate_links")
        ),
        sponsor=payload.get("sponsor"),
        affiliate_links=[AffiliateLink(**link) for link in payload.get("affiliate_links", [])],
    )


def publish(session: Session, publication: Publication, provider: YouTubeProvider) -> str:
    """Upload under a matching approval: video, then thumbnail, then captions.

    The remote id is stored as soon as the upload returns, so a retry after a thumbnail or caption
    failure does not upload the video twice.
    """
    queue.require_approval(session, publication)  # raises NotApproved: no call at all
    payload = queue.load_payload(publication)
    if not publication.remote_id:
        video_id = provider.upload(
            payload["media_path"],
            payload["title"],
            description=payload["description"],
            tags=payload["tags"],
            privacy=payload["privacy"],
            publish_at=payload["publish_at"],
            paid_promotion=payload["paid_promotion"],
        )
        publication.remote_id = str(video_id)
        session.add(publication)
        session.commit()
        session.refresh(publication)
    video_id = publication.remote_id
    assert video_id is not None
    if payload.get("thumbnail_path"):
        provider.set_thumbnail(video_id, payload["thumbnail_path"])
    if payload.get("captions_srt"):
        provider.insert_caption(video_id, payload["captions_srt"], CAPTION_LANGUAGE)
    return video_id
