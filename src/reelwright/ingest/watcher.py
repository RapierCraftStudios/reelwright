# SPDX-License-Identifier: AGPL-3.0-or-later
"""The footage watcher on the capture PC (spike § Runtime topology).

Polls the DJI import and Recordly output folders. A file is reported only once its size and mtime
have not changed for `stable_s` seconds, so a half-copied file is never ingested. Reported files
are checksummed (sha256) and probed for duration, then posted as a manifest to
`POST /api/assets/manifest`. Only metadata leaves this PC; the footage stays where it is.

    python workers/watcher/watch.py --server https://<dev-server>.<tailnet>.ts.net \\
        --dji D:\\DJI --recordly D:\\Recordly
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from reelwright.ingest.manifest import MEDIA_EXTS, classify_kind
from reelwright.render.worker import TOKEN_ENV, check_server_url

STABLE_S = 5.0
POLL_S = 2.0
CHUNK = 1024 * 1024


@dataclass
class _Seen:
    size: int
    mtime: float
    since: float


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def ffprobe_duration(path: Path) -> float:
    """Container duration in seconds via ffprobe; 0.0 when it can't be read."""
    argv = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=True).stdout
        return float(json.loads(out)["format"]["duration"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return 0.0


def _scan(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.suffix.casefold() in MEDIA_EXTS and p.is_file():
                yield p


class Watcher:
    """Size-stable polling over the capture folders; posts manifests of finished files."""

    def __init__(
        self,
        client: httpx.Client,
        dji_roots: Sequence[Path] = (),
        recordly_roots: Sequence[Path] = (),
        *,
        host: str = "",
        stable_s: float = STABLE_S,
        clock: Callable[[], float] = time.monotonic,
        scan: Callable[[Iterable[Path]], Iterable[Path]] = _scan,
        stat: Callable[[Path], os.stat_result] = os.stat,
        hasher: Callable[[Path], str] = sha256_file,
        prober: Callable[[Path], float] = ffprobe_duration,
    ) -> None:
        self.client = client
        self.dji_roots = list(dji_roots)
        self.recordly_roots = list(recordly_roots)
        self.host = host
        self.stable_s = stable_s
        self.clock = clock
        self.scan = scan
        self.stat = stat
        self.hasher = hasher
        self.prober = prober
        self.seen: dict[Path, _Seen] = {}
        self.reported: dict[Path, tuple[int, float]] = {}

    def stable_files(self) -> list[Path]:
        """Files whose size and mtime have held for stable_s; updates the tracking state."""
        now = self.clock()
        present: set[Path] = set()
        ready: list[Path] = []
        for path in self.scan([*self.dji_roots, *self.recordly_roots]):
            try:
                st = self.stat(path)
            except OSError:
                continue
            present.add(path)
            sig = (st.st_size, st.st_mtime)
            if self.reported.get(path) == sig:
                continue
            prev = self.seen.get(path)
            if prev is None or (prev.size, prev.mtime) != sig:
                self.seen[path] = _Seen(st.st_size, st.st_mtime, now)
                continue
            if st.st_size > 0 and now - prev.since >= self.stable_s:
                ready.append(path)
        for gone in set(self.seen) - present:
            del self.seen[gone]
        return ready

    def entry_for(self, path: Path) -> dict[str, Any]:
        st = self.stat(path)
        return {
            "path": str(path),
            "host": self.host,
            "checksum": self.hasher(path),
            "size_bytes": st.st_size,
            "kind": classify_kind(
                str(path), map(str, self.dji_roots), map(str, self.recordly_roots)
            ).value,
            "duration_s": max(0.0, self.prober(path)),
        }

    def poll(self) -> int:
        """One pass: report every newly stable file. Returns how many were reported."""
        ready = self.stable_files()
        if not ready:
            return 0
        entries: list[dict[str, Any]] = []
        sigs: dict[Path, tuple[int, float]] = {}
        for path in ready:
            try:
                before = self.stat(path)
                entry = self.entry_for(path)
                after = self.stat(path)
            except OSError:
                continue
            if (before.st_size, before.st_mtime) != (after.st_size, after.st_mtime):
                self.seen.pop(path, None)  # it changed while hashing: wait again
                continue
            entries.append(entry)
            sigs[path] = (after.st_size, after.st_mtime)
        if not entries:
            return 0
        resp = self.client.post("/api/assets/manifest", json={"entries": entries})
        resp.raise_for_status()
        for path, sig in sigs.items():
            self.reported[path] = sig
            self.seen.pop(path, None)
        return len(entries)

    def run_forever(self, poll_s: float = POLL_S) -> None:
        while True:
            try:
                self.poll()
            except httpx.HTTPError as e:
                print(f"watcher: {e}", file=sys.stderr)
            time.sleep(poll_s)


def make_client(server: str, token: str | None = None) -> httpx.Client:
    check_server_url(server)
    token = token if token is not None else os.environ.get(TOKEN_ENV)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=server, timeout=30.0, headers=headers)


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="rw-watcher", description=__doc__.split("\n\n")[0])
    ap.add_argument("--server", default=os.environ.get("REELWRIGHT_SERVER"), required=False)
    ap.add_argument("--dji", action="append", default=[], help="DJI import folder (repeatable)")
    ap.add_argument("--recordly", action="append", default=[], help="Recordly output folder")
    ap.add_argument("--host", default=socket.gethostname(), help="Capture PC name")
    ap.add_argument("--stable-s", type=float, default=STABLE_S)
    ap.add_argument("--poll-s", type=float, default=POLL_S)
    args = ap.parse_args(argv)
    if not args.server:
        ap.error("--server (or REELWRIGHT_SERVER) is required")
    if not args.dji and not args.recordly:
        ap.error("give at least one --dji or --recordly folder")
    watcher = Watcher(
        make_client(args.server),
        [Path(p) for p in args.dji],
        [Path(p) for p in args.recordly],
        host=args.host,
        stable_s=args.stable_s,
    )
    watcher.run_forever(args.poll_s)
