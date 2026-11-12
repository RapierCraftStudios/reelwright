# SPDX-License-Identifier: AGPL-3.0-or-later
"""/api/assets: watcher manifests, proxy uploads and the asset list (spike § Route/API map).

Manifest, proxy and transcript uploads use the worker token (same rule as /api/workers). Proxy
uploads go through the disk budget and free-disk floor, transcripts through the floor; a write
that would cross either answers 507. GET /api/assets/{id} is the asset view, with its transcript.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError
from sqlmodel import Session, col, select

from reelwright.api.workers import WorkerAuth
from reelwright.db.models import (
    Asset,
    AssetDetail,
    AssetManifest,
    AssetRead,
    Transcript,
    TranscriptUpload,
)
from reelwright.db.session import get_session
from reelwright.ingest import manifest as manifest_mod
from reelwright.ingest import proxy, transcript

router = APIRouter(prefix="/api/assets", tags=["assets"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("", response_model=list[AssetRead])
def list_assets(session: SessionDep) -> list[AssetRead]:
    rows = session.exec(select(Asset).order_by(col(Asset.id))).all()
    return [manifest_mod.asset_to_read(a) for a in rows]


@router.post(
    "/manifest",
    response_model=list[AssetRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=WorkerAuth,
)
def post_manifest(body: AssetManifest, session: SessionDep) -> list[AssetRead]:
    """The watcher reports stable, checksummed footage; proxies are queued for video."""
    return [manifest_mod.asset_to_read(a) for a in manifest_mod.ingest_manifest(session, body)]


def _sync_chunks(stream: AsyncIterator[bytes]) -> Iterator[bytes]:
    """Pull the request body from the event loop while guarded_write runs in a worker thread."""

    async def next_chunk() -> bytes | None:
        try:
            return await stream.__anext__()
        except StopAsyncIteration:
            return None

    while (chunk := anyio.from_thread.run(next_chunk)) is not None:
        yield chunk


@router.put("/{asset_id}/proxy", response_model=AssetRead, dependencies=WorkerAuth)
async def put_proxy(asset_id: int, request: Request, session: SessionDep) -> AssetRead:
    """A worker uploads the proxy it encoded; written under the budget, aborted below the floor."""
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    budget = proxy.ProxyBudget.from_env()
    chunks = _sync_chunks(request.stream().__aiter__())
    try:
        dest = await anyio.to_thread.run_sync(
            proxy.guarded_write, chunks, proxy.proxy_name(asset_id), budget
        )
    except proxy.DiskBudgetExceeded as e:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE, detail=str(e)
        ) from None
    asset.proxy_path = str(dest)
    session.add(asset)
    session.commit()
    session.refresh(asset)
    return manifest_mod.asset_to_read(asset)


def _detail(session: Session, asset: Asset) -> AssetDetail:
    row = session.get(Transcript, asset.transcript_id) if asset.transcript_id else None
    return AssetDetail(
        **manifest_mod.asset_to_read(asset).model_dump(),
        transcript=transcript.transcript_to_read(row) if row is not None else None,
    )


@router.get("/{asset_id}", response_model=AssetDetail)
def get_asset(asset_id: int, session: SessionDep) -> AssetDetail:
    """The asset view: metadata, proxy, and the word-timed transcript once it is done."""
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return _detail(session, asset)


@router.put("/{asset_id}/transcript", response_model=AssetDetail, dependencies=WorkerAuth)
async def put_transcript(asset_id: int, request: Request, session: SessionDep) -> AssetDetail:
    """A worker uploads the transcript JSON its transcribe job wrote; checked against the floor."""
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > transcript.MAX_TRANSCRIPT_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"transcript over {transcript.MAX_TRANSCRIPT_BYTES} bytes",
            )
    try:
        upload = TranscriptUpload.model_validate_json(bytes(body))
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=e.errors(include_url=False, include_context=False, include_input=False),
        ) from None
    try:
        transcript.store_transcript(session, asset, upload, len(body))
    except proxy.DiskBudgetExceeded as e:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE, detail=str(e)
        ) from None
    return _detail(session, asset)
