# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mock providers: every external service has one, so dev and CI never need credentials.

Mocks only record what they were asked to do, in memory. They never touch the network or disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from spectracut.providers.base import Completion
from spectracut.providers.mock_text import MockTextProvider


@dataclass
class MockYouTube:
    """Records uploads; returns canned analytics."""

    uploads: list[dict[str, Any]] = field(default_factory=list)

    def upload(self, path: str, title: str, **meta: Any) -> str:
        video_id = f"mock-{len(self.uploads) + 1}"
        self.uploads.append({"id": video_id, "path": path, "title": title, **meta})
        return video_id

    def analytics(self, video_id: str) -> dict[str, Any]:
        return {"video_id": video_id, "views": 0, "watch_time_s": 0, "ctr": 0.0, "retention": []}


@dataclass
class MockHands:
    """Records AlterLab Hands browser-task actions (X, Instagram, Facebook, TikTok)."""

    actions: list[dict[str, Any]] = field(default_factory=list)

    def act(self, site: str, action: str, **params: Any) -> dict[str, Any]:
        record = {"site": site, "action": action, **params}
        self.actions.append(record)
        return {"ok": True, "mock": True, **record}


@dataclass
class MockAlterLab:
    """Returns a fixture page for any scrape."""

    requests: list[str] = field(default_factory=list)

    def scrape(self, url: str) -> dict[str, Any]:
        self.requests.append(url)
        return {"url": url, "status": 200, "markdown": f"# Fixture page\n\nMock content for {url}."}


@dataclass
class MockMusic:
    """A licensed-music library stand-in with an empty CC0 catalogue."""

    catalogue: list[dict[str, Any]] = field(default_factory=list)

    def search(self, query: str) -> list[dict[str, Any]]:
        return [t for t in self.catalogue if query.lower() in str(t.get("title", "")).lower()]


@dataclass
class LocalCpuWorker:
    """A CPU-only render worker on the local machine."""

    name: str = "local-cpu"
    capabilities: tuple[str, ...] = ("cpu", "ffmpeg")

    @property
    def kinds(self) -> tuple[str, ...]:
        """Every render job kind: the CPU fallback runs the same kinds as a GPU worker, slower."""
        from reelwright.render.queue import JobKind

        return tuple(JobKind)


@dataclass
class MockLLM:
    """Deterministic, schema-valid structured completions for CI (SpectraCut ``TextProvider``).

    Delegates to SpectraCut's ``MockTextProvider``: no network, no CLI, and it honours the schema's
    ``minItems``/``maxItems``, ``enum`` and ``minLength``.
    """

    name: str = "mock-llm"
    prompts: list[str] = field(default_factory=list)

    def complete(self, system: str, prompt: str, *, schema: dict[str, Any]) -> Completion:
        self.prompts.append(prompt)
        result: Completion = MockTextProvider().complete(system, prompt, schema=schema)
        return result
