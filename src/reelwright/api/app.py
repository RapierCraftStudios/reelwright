# SPDX-License-Identifier: AGPL-3.0-or-later
"""The FastAPI app: health, /api routes and the built web bundle."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from reelwright.api import projects, script
from reelwright.db.session import migrate

REPO_ROOT = Path(__file__).resolve().parents[3]


def web_dist() -> Path:
    """The built web bundle: REELWRIGHT_WEB_DIST, else apps/web/dist in the repo."""
    configured = os.environ.get("REELWRIGHT_WEB_DIST")
    return Path(configured) if configured else REPO_ROOT / "apps" / "web" / "dist"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    migrate()
    yield


def mount_web(app: FastAPI, dist: Path) -> None:
    index = dist / "index.html"
    if not index.is_file():
        return
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    root = dist.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404)
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index)


def create_app() -> FastAPI:
    app = FastAPI(title="Reelwright", lifespan=lifespan)

    @app.get("/healthz", tags=["health"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(projects.router)
    app.include_router(script.router)
    mount_web(app, web_dist())
    return app
