# SPDX-License-Identifier: AGPL-3.0-or-later
"""Watcher manifests in, Asset rows and proxy jobs out.

A manifest is metadata only (path, checksum, size, kind, duration): the footage stays on the
capture PC. Assets are keyed by checksum, so re-posting a manifest is idempotent.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePath, PureWindowsPath

from sqlmodel import Session, select

from reelwright.db.models import VIDEO_KINDS, Asset, AssetKind, AssetManifest, AssetRead
from reelwright.ingest import proxy

AUDIO_EXTS = frozenset({".wav", ".mp3", ".m4a", ".aac", ".flac"})
VIDEO_EXTS = frozenset({".mp4", ".mov", ".mkv", ".avi", ".lrf"})
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS


def _pure(path: str) -> PurePath:
    return PureWindowsPath(path) if ("\\" in path or ":" in path[:3]) else PurePath(path)


def _under(path: PurePath, roots: Iterable[str]) -> bool:
    folded = str(path).replace("\\", "/").casefold()
    for root in roots:
        r = str(_pure(root)).replace("\\", "/").casefold().rstrip("/")
        if r and (folded == r or folded.startswith(r + "/")):
            return True
    return False


def classify_kind(
    path: str, dji_roots: Iterable[str] = (), recordly_roots: Iterable[str] = ()
) -> AssetKind:
    """DJI imports are camera footage, Recordly output is screen; else audio or broll by type."""
    p = _pure(path)
    if _under(p, dji_roots):
        return AssetKind.camera
    if _under(p, recordly_roots):
        return AssetKind.screen
    if p.suffix.casefold() in AUDIO_EXTS:
        return AssetKind.audio
    return AssetKind.broll


def ingest_manifest(session: Session, manifest: AssetManifest) -> list[Asset]:
    """Upsert one Asset per entry and queue a proxy for video without one."""
    out: list[Asset] = []
    for entry in manifest.entries:
        asset = session.exec(select(Asset).where(Asset.checksum == entry.checksum)).first()
        same_path = session.exec(
            select(Asset).where(Asset.host == entry.host, Asset.path == entry.path)
        ).first()
        if asset is None and same_path is not None:
            # The file at this path was replaced: new content, so a new proxy.
            asset = same_path
            asset.proxy_path = None
            asset.proxy_job_id = None
        elif asset is not None and same_path is not None and same_path.id != asset.id:
            session.delete(same_path)
            session.flush()
        if asset is None:
            asset = Asset(path=entry.path, checksum=entry.checksum, kind=entry.kind.value)
        asset.host = entry.host
        asset.path = entry.path
        asset.checksum = entry.checksum
        asset.size_bytes = entry.size_bytes
        asset.kind = entry.kind.value
        asset.duration_s = entry.duration_s
        session.add(asset)
        session.commit()
        session.refresh(asset)
        if AssetKind(asset.kind) in VIDEO_KINDS and not asset.proxy_path and not asset.proxy_job_id:
            proxy.enqueue_proxy(session, asset)
        out.append(asset)
    return out


def asset_to_read(asset: Asset) -> AssetRead:
    assert asset.id is not None
    return AssetRead(
        id=asset.id,
        host=asset.host,
        path=asset.path,
        checksum=asset.checksum,
        size_bytes=asset.size_bytes,
        kind=AssetKind(asset.kind),
        duration_s=asset.duration_s,
        proxy_path=asset.proxy_path,
        proxy_job_id=asset.proxy_job_id,
        created_at=asset.created_at,
    )
