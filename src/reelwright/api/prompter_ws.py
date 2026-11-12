# SPDX-License-Identifier: AGPL-3.0-or-later
"""WS /api/prompter/{id}/align: mic audio in, script position out (spike § Route/API map).

Protocol, after the socket opens:
- the client's first frame is text `{"type": "start", "script_version": N, "line_no": L}`;
- binary frames are PCM16 little-endian mono 16 kHz audio;
- text frames `{"type": "seek", "line_no": L}` (manual move) and `{"type": "ping", "t": T}`.
The server sends `{"type": "position", "line_no", "word", "confidence", "server_ms"}` when the
reading position moves, `{"type": "pong", "t": T}` for each ping, and `{"type": "error", "code"}`
before closing on a problem. Close codes: 4400 bad message, 4404 project or version not found,
4409 version not locked, 4422 no spoken lines, 1011 speech engine unavailable.
Audio is held in memory only; nothing is written to disk.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlmodel import Session, select
from starlette.concurrency import run_in_threadpool

from reelwright.db.models import Project, ScriptVersion
from reelwright.db.session import get_engine
from reelwright.prompter.align import Position, ScriptAligner, spoken_lines
from reelwright.prompter.asr import AsrUnavailable, StreamingAsr, make_asr
from reelwright.script import parse

router = APIRouter(prefix="/api/prompter/{project_id}", tags=["prompter"])


class AlignClose(Exception):
    def __init__(self, code: int, error: str) -> None:
        super().__init__(error)
        self.code = code
        self.error = error


def _locked_source(project_id: int, script_version: int) -> str:
    with Session(get_engine()) as session:
        if session.get(Project, project_id) is None:
            raise AlignClose(4404, "project_not_found")
        row = session.exec(
            select(ScriptVersion).where(
                ScriptVersion.project_id == project_id, ScriptVersion.version == script_version
            )
        ).first()
        if row is None:
            raise AlignClose(4404, "version_not_found")
        if row.locked_at is None:
            raise AlignClose(4409, "not_locked")
        return row.source


def _int(message: dict[str, Any], key: str) -> int:
    value = message.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise AlignClose(4400, f"bad_{key}")
    return value


def _message(text: str) -> dict[str, Any]:
    try:
        message = json.loads(text)
    except ValueError as exc:
        raise AlignClose(4400, "bad_message") from exc
    if not isinstance(message, dict):
        raise AlignClose(4400, "bad_message")
    return message


def _seek(aligner: ScriptAligner, asr: StreamingAsr, line_no: int) -> None:
    try:
        aligner.seek(line_no)
    except ValueError as exc:
        raise AlignClose(4400, "bad_line_no") from exc
    asr.reset(aligner.cursor)


def _position(position: Position, started: float) -> dict[str, Any]:
    return {
        "type": "position",
        "line_no": position.line_no,
        "word": position.word,
        "confidence": position.confidence,
        "server_ms": round((time.perf_counter() - started) * 1000, 2),
    }


async def _open(websocket: WebSocket, project_id: int) -> tuple[ScriptAligner, StreamingAsr]:
    start = _message(await websocket.receive_text())
    if start.get("type") != "start":
        raise AlignClose(4400, "expected_start")
    source = _locked_source(project_id, _int(start, "script_version"))
    lines = spoken_lines(parse(source).script)
    if not lines:
        raise AlignClose(4422, "no_spoken_lines")
    aligner = ScriptAligner(lines)
    try:
        asr = await run_in_threadpool(make_asr, aligner.words)
    except AsrUnavailable as exc:
        raise AlignClose(1011, "asr_unavailable") from exc
    line_no = start.get("line_no")
    _seek(aligner, asr, lines[0].line_no if line_no is None else _int(start, "line_no"))
    return aligner, asr


@router.websocket("/align")
async def align(websocket: WebSocket, project_id: int) -> None:
    await websocket.accept()
    try:
        aligner, asr = await _open(websocket, project_id)
        while True:
            frame = await websocket.receive()
            if frame["type"] == "websocket.disconnect":
                return
            data: bytes | None = frame.get("bytes")
            if data is not None:
                started = time.perf_counter()
                words = await run_in_threadpool(asr.feed, data)
                moved = aligner.feed(words)
                if moved is not None:
                    await websocket.send_json(_position(moved, started))
                continue
            message = _message(frame.get("text") or "")
            kind = message.get("type")
            if kind == "ping":
                await websocket.send_json({"type": "pong", "t": message.get("t")})
            elif kind == "seek":
                _seek(aligner, asr, _int(message, "line_no"))
            else:
                raise AlignClose(4400, "bad_message")
    except AlignClose as close:
        await websocket.send_json({"type": "error", "code": close.error})
        await websocket.close(code=close.code)
    except WebSocketDisconnect:
        return
