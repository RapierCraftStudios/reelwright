# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
import pytest

from reelwright.ingest import watcher as watcher_mod
from reelwright.ingest.watcher import Watcher, make_client, sha256_file

DJI = Path("/capture/DJI")
REC = Path("/capture/Recordly")
CLIP = DJI / "DJI_0001.MP4"


class FakeFs:
    """Files as (size, mtime); the copy grows the size until it finishes."""

    def __init__(self) -> None:
        self.files: dict[Path, tuple[int, float]] = {}

    def scan(self, roots: Iterable[Path]) -> list[Path]:
        roots = list(roots)
        return [p for p in self.files if any(p.is_relative_to(r) for r in roots)]

    def stat(self, path: Path) -> os.stat_result:
        if path not in self.files:
            raise FileNotFoundError(path)
        size, mtime = self.files[path]
        return os.stat_result((0o100644, 0, 0, 1, 0, 0, size, 0, int(mtime), 0))


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def make(
    fs: FakeFs, clock: Clock, posts: list[dict[str, Any]], headers: dict[str, str] | None = None
) -> Watcher:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/assets/manifest"
        posts.append({"body": httpx.Response(200, content=request.content).json(), "req": request})
        return httpx.Response(201, json=[])

    client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
        headers=headers or {},
    )
    return Watcher(
        client,
        [DJI],
        [REC],
        host="mrdubey",
        stable_s=5,
        clock=clock,
        scan=fs.scan,
        stat=fs.stat,
        hasher=lambda p: hashlib.sha256(str(p).encode()).hexdigest(),
        prober=lambda p: 312.5,
    )


def test_half_copied_file_is_never_reported_while_it_grows() -> None:
    fs, clock = FakeFs(), Clock()
    posts: list[dict[str, Any]] = []
    w = make(fs, clock, posts)
    for i in range(30):  # the copy grows every 2 s poll for a minute
        fs.files[CLIP] = ((i + 1) * 100_000_000, float(i))
        assert w.poll() == 0
        clock.t += 2
    assert posts == []


def test_stable_file_reported_once_within_30s_of_copy_finishing() -> None:
    fs, clock = FakeFs(), Clock()
    posts: list[dict[str, Any]] = []
    w = make(fs, clock, posts)
    fs.files[CLIP] = (100, 1.0)
    w.poll()
    clock.t += 2
    fs.files[CLIP] = (734003200, 2.0)  # last write: the copy finishes here
    finished_at = clock.t
    reported_at = None
    while clock.t - finished_at <= 30:
        w.poll()
        if posts:
            reported_at = clock.t
            break
        clock.t += 2  # the default poll interval
    assert reported_at is not None and reported_at - finished_at <= 30
    (entry,) = posts[0]["body"]["entries"]
    assert entry == {
        "path": str(CLIP),
        "host": "mrdubey",
        "checksum": hashlib.sha256(str(CLIP).encode()).hexdigest(),
        "size_bytes": 734003200,
        "kind": "camera",
        "duration_s": 312.5,
    }
    for _ in range(5):  # never reported twice
        clock.t += 2
        assert w.poll() == 0
    assert len(posts) == 1


def test_shrink_or_vanish_resets_the_stability_timer() -> None:
    fs, clock = FakeFs(), Clock()
    posts: list[dict[str, Any]] = []
    w = make(fs, clock, posts)
    clip = REC / "take.mp4"
    fs.files[clip] = (500, 1.0)
    w.poll()
    clock.t += 4
    fs.files[clip] = (200, 1.0)  # truncated and rewritten
    assert w.poll() == 0
    clock.t += 4
    assert w.poll() == 0  # only 4 s stable since the change
    del fs.files[clip]
    clock.t += 2
    assert w.poll() == 0
    fs.files[clip] = (200, 1.0)
    w.poll()
    clock.t += 4
    assert w.poll() == 0
    clock.t += 2
    assert w.poll() == 1
    assert posts[0]["body"]["entries"][0]["kind"] == "screen"


def test_empty_file_is_not_reported() -> None:
    fs, clock = FakeFs(), Clock()
    posts: list[dict[str, Any]] = []
    w = make(fs, clock, posts)
    fs.files[CLIP] = (0, 1.0)
    for _ in range(10):
        w.poll()
        clock.t += 2
    assert posts == []


def test_manifest_carries_metadata_only_and_the_bearer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, clock = FakeFs(), Clock()
    posts: list[dict[str, Any]] = []
    w = make(fs, clock, posts, headers={"Authorization": "Bearer t0k"})
    fs.files[CLIP] = (10, 1.0)
    w.poll()
    clock.t += 6
    assert w.poll() == 1
    req: httpx.Request = posts[0]["req"]
    assert req.headers["Authorization"] == "Bearer t0k"
    assert len(req.content) < 1024  # metadata, not footage
    assert set(posts[0]["body"]["entries"][0]) == {
        "path",
        "host",
        "checksum",
        "size_bytes",
        "kind",
        "duration_s",
    }

    monkeypatch.setenv("REELWRIGHT_WORKER_TOKEN", "from-env")
    assert (
        make_client("https://devbox.example.ts.net").headers["Authorization"] == "Bearer from-env"
    )
    with pytest.raises(ValueError):
        make_client("http://devbox.example.ts.net")


def test_sha256_file_and_ffprobe_fallback(tmp_path: Path) -> None:
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x" * (3 * 1024 * 1024 + 7))
    assert sha256_file(f) == hashlib.sha256(f.read_bytes()).hexdigest()
    assert watcher_mod.ffprobe_duration(tmp_path / "missing.mp4") == 0.0


def test_real_scan_finds_media_only(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "DJI_1.MP4").write_bytes(b"v")
    (tmp_path / "notes.txt").write_text("n")
    found = list(watcher_mod._scan([tmp_path, tmp_path / "missing"]))
    assert found == [tmp_path / "sub" / "DJI_1.MP4"]
