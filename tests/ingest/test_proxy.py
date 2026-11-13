# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from reelwright.ingest import proxy
from reelwright.ingest.proxy import MB, DiskBudgetExceeded, ProxyBudget, check_disk, guarded_write


class FakeDisk:
    """free = start - bytes written under the proxy dir so far (minus any extra drain)."""

    def __init__(self, root: Path, start: int, drain_per_call: int = 0) -> None:
        self.root, self.start, self.drain, self.calls = root, start, drain_per_call, 0

    def __call__(self, _: Path) -> Any:
        self.calls += 1
        written = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        return SimpleNamespace(free=self.start - written - self.drain * self.calls)


def budget(tmp_path: Path, free: int, budget_b: int = 100, floor: int = 10, drain: int = 0):  # type: ignore[no-untyped-def]
    d = tmp_path / "proxies"
    d.mkdir(exist_ok=True)
    return ProxyBudget(d, budget_b, floor, FakeDisk(d, free, drain))


def test_write_under_budget_lands(tmp_path: Path) -> None:
    b = budget(tmp_path, free=1000)
    dest = guarded_write([b"a" * 30, b"b" * 30], "asset-1.proxy.mp4", b)
    assert dest == (b.proxy_dir / "asset-1.proxy.mp4").resolve()
    assert dest.read_bytes() == b"a" * 30 + b"b" * 30
    assert [p.name for p in b.proxy_dir.iterdir()] == ["asset-1.proxy.mp4"]


def test_over_budget_raises_and_leaves_nothing(tmp_path: Path) -> None:
    b = budget(tmp_path, free=1000, budget_b=50)
    with pytest.raises(DiskBudgetExceeded, match="budget"):
        guarded_write([b"x" * 30, b"y" * 30], "asset-2.proxy.mp4", b)
    assert list(b.proxy_dir.iterdir()) == []


def test_crossing_free_disk_floor_mid_write_aborts_and_deletes_partial(tmp_path: Path) -> None:
    b = budget(tmp_path, free=60, budget_b=10_000, floor=10)
    with pytest.raises(DiskBudgetExceeded, match="floor"):
        guarded_write([b"x" * 20, b"y" * 20, b"z" * 20], "asset-3.proxy.mp4", b)
    assert list(b.proxy_dir.iterdir()) == []


def test_floor_already_crossed_refuses_before_writing(tmp_path: Path) -> None:
    b = budget(tmp_path, free=5, floor=10)
    with pytest.raises(DiskBudgetExceeded):
        guarded_write(iter([b"x"]), "asset-4.proxy.mp4", b)
    with pytest.raises(DiskBudgetExceeded):
        check_disk(b, 0)
    assert list(b.proxy_dir.iterdir()) == []


def test_replacing_a_proxy_does_not_count_the_old_one(tmp_path: Path) -> None:
    b = budget(tmp_path, free=1000, budget_b=50)
    guarded_write([b"a" * 40], "asset-5.proxy.mp4", b)
    guarded_write([b"b" * 40], "asset-5.proxy.mp4", b)
    assert (b.proxy_dir / "asset-5.proxy.mp4").read_bytes() == b"b" * 40


@pytest.mark.parametrize("name", ["../escape.mp4", "/etc/passwd", "", "x.mp4.part"])
def test_destination_stays_inside_proxy_dir(tmp_path: Path, name: str) -> None:
    b = budget(tmp_path, free=1000)
    with pytest.raises(ValueError):
        guarded_write([b"x"], name, b)
    assert not (tmp_path / "escape.mp4").exists()


def test_budget_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("REELWRIGHT_PROXY_DIR", str(tmp_path))
    monkeypatch.setenv("REELWRIGHT_PROXY_BUDGET_MB", "7")
    monkeypatch.delenv("REELWRIGHT_FREE_DISK_FLOOR_MB", raising=False)
    b = ProxyBudget.from_env()
    assert (b.proxy_dir, b.budget_bytes, b.floor_bytes) == (tmp_path, 7 * MB, 5120 * MB)


def _manifest(client: TestClient) -> int:
    entry = {
        "path": "D:\\DJI\\DJI_0002.MP4",
        "host": "mrdubey",
        "checksum": hashlib.sha256(b"two").hexdigest(),
        "size_bytes": 10,
        "kind": "camera",
        "duration_s": 1.5,
    }
    r = client.post("/api/assets/manifest", json={"entries": [entry]})
    return int(r.json()[0]["id"])


def test_put_proxy_lands_and_sets_proxy_path(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REELWRIGHT_PROXY_DIR", str(tmp_path / "p"))
    monkeypatch.setenv("REELWRIGHT_FREE_DISK_FLOOR_MB", "0")
    asset_id = _manifest(client)
    r = client.put(f"/api/assets/{asset_id}/proxy", content=b"proxy-bytes")
    assert r.status_code == 200, r.text
    path = Path(r.json()["proxy_path"])
    assert path.read_bytes() == b"proxy-bytes" and path.parent == (tmp_path / "p").resolve()
    assert client.get("/api/assets").json()[0]["proxy_path"] == str(path)
    assert client.put("/api/assets/999/proxy", content=b"x").status_code == 404


def test_put_proxy_is_507_below_the_free_disk_floor(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REELWRIGHT_PROXY_DIR", str(tmp_path / "p"))
    low_disk = ProxyBudget(tmp_path / "p", 10 * MB, 2 * MB, lambda _: SimpleNamespace(free=MB))
    monkeypatch.setattr(proxy.ProxyBudget, "from_env", lambda *a, **k: low_disk)
    asset_id = _manifest(client)
    r = client.put(f"/api/assets/{asset_id}/proxy", content=b"x" * 1024)
    assert r.status_code == 507
    assert "floor" in r.json()["detail"]
    assert list((tmp_path / "p").iterdir()) == []
    assert client.get("/api/assets").json()[0]["proxy_path"] is None
