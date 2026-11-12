# SPDX-License-Identifier: AGPL-3.0-or-later
"""`rw serve` boots a real server process that answers /healthz."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_rw_serve_boots_and_answers_healthz(tmp_path: Path) -> None:
    rw = Path(sys.executable).parent / "rw"
    assert rw.exists(), "the `rw` console script is not installed in the venv"
    port = _free_port()
    env = {
        **os.environ,
        "REELWRIGHT_DB_URL": f"sqlite:///{tmp_path / 'serve.sqlite'}",
        "REELWRIGHT_WEB_DIST": str(tmp_path / "no-web"),
    }
    log = (tmp_path / "serve.log").open("w")
    proc = subprocess.Popen(
        [str(rw), "serve", "--port", str(port)], env=env, stdout=log, stderr=subprocess.STDOUT
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            assert proc.poll() is None, (tmp_path / "serve.log").read_text()
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1)
                break
            except httpx.TransportError:
                assert time.monotonic() < deadline, "rw serve did not answer within 30s"
                time.sleep(0.2)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        log.close()
