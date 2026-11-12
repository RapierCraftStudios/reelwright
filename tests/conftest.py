# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reelwright.api.app import create_app


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point REELWRIGHT_DB_URL at a fresh SQLite file and keep the web bundle out of the way."""
    url = f"sqlite:///{tmp_path / 'rw.sqlite'}"
    monkeypatch.setenv("REELWRIGHT_DB_URL", url)
    monkeypatch.setenv("REELWRIGHT_WEB_DIST", str(tmp_path / "no-web"))
    return url


@pytest.fixture
def client(tmp_db: str) -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c
