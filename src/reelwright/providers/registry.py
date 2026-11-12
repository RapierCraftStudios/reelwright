# SPDX-License-Identifier: AGPL-3.0-or-later
"""Provider registry: one entry per external service (spike § Credentials).

A service resolves to its mock unless every one of its credential env vars is set. With credentials
set, it resolves to the real adapter. If no real adapter is registered yet, it raises instead of
falling back to the mock, because a silent mock with real credentials present would fake a publish.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any

from reelwright.providers import mocks


class Service(StrEnum):
    youtube = "youtube"
    hands = "hands"
    alterlab = "alterlab"
    music = "music"
    worker = "worker"
    llm = "llm"


class ProviderNotConfigured(RuntimeError):
    """Credentials are set for a service whose real adapter does not exist yet."""


CREDENTIALS: dict[Service, tuple[str, ...]] = {
    Service.youtube: ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET"),
    Service.hands: ("ALTERLAB_API_KEY",),
    Service.alterlab: ("ALTERLAB_API_KEY",),
    Service.music: ("MUSIC_LIBRARY_API_KEY",),
    Service.worker: ("REELWRIGHT_WORKER_TOKEN",),
    # The LLM runs through SpectraCut's providers layer; until that adapter lands it is mock-only.
    Service.llm: (),
}

MOCKS: dict[Service, Callable[[], Any]] = {
    Service.youtube: mocks.MockYouTube,
    Service.hands: mocks.MockHands,
    Service.alterlab: mocks.MockAlterLab,
    Service.music: mocks.MockMusic,
    Service.worker: mocks.LocalCpuWorker,
    Service.llm: mocks.MockLLM,
}

# Real adapters register here as their issues land: REAL[Service.youtube] = YouTubeClient, ...
REAL: dict[Service, Callable[[Mapping[str, str]], Any]] = {}


def has_credentials(service: Service, env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    names = CREDENTIALS[service]
    return bool(names) and all(env.get(name) for name in names)


def get_provider(service: Service | str, env: Mapping[str, str] | None = None) -> Any:
    """Return the provider for a service: the mock without credentials, else the real adapter."""
    service = Service(service)
    env = os.environ if env is None else env
    if not has_credentials(service, env):
        return MOCKS[service]()
    factory = REAL.get(service)
    if factory is None:
        raise ProviderNotConfigured(
            f"credentials for {service.value} are set but no real adapter is registered; "
            f"unset {', '.join(CREDENTIALS[service])} to use the mock"
        )
    return factory(env)
