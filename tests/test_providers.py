# SPDX-License-Identifier: AGPL-3.0-or-later
"""The registry hands out mocks when no credential is set."""

from __future__ import annotations

import pytest

from reelwright.providers import mocks
from reelwright.providers.registry import (
    CREDENTIALS,
    MOCKS,
    ProviderNotConfigured,
    Service,
    get_provider,
)

ALL_CREDENTIAL_VARS = sorted({name for names in CREDENTIALS.values() for name in names})

EXPECTED = {
    Service.youtube: mocks.MockYouTube,
    Service.hands: mocks.MockHands,
    Service.alterlab: mocks.MockAlterLab,
    Service.music: mocks.MockMusic,
    Service.worker: mocks.LocalCpuWorker,
    Service.llm: mocks.MockLLM,
}


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ALL_CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)


def test_every_service_has_a_mock_and_a_credential_entry() -> None:
    assert set(MOCKS) == set(Service) == set(CREDENTIALS) == set(EXPECTED)


@pytest.mark.parametrize("service", list(Service))
def test_mock_when_no_credential(no_credentials: None, service: Service) -> None:
    assert isinstance(get_provider(service), EXPECTED[service])


@pytest.mark.parametrize("service", list(Service))
def test_mock_when_credentials_are_empty_strings(service: Service) -> None:
    env = dict.fromkeys(ALL_CREDENTIAL_VARS, "")
    assert isinstance(get_provider(service, env=env), EXPECTED[service])


def test_partial_credentials_still_use_the_mock() -> None:
    provider = get_provider(Service.youtube, env={"YOUTUBE_CLIENT_ID": "id-only"})
    assert isinstance(provider, mocks.MockYouTube)


@pytest.mark.parametrize("service", [s for s in Service if CREDENTIALS[s]])
def test_credentials_without_real_adapter_raise(service: Service) -> None:
    env = dict.fromkeys(CREDENTIALS[service], "set")
    with pytest.raises(ProviderNotConfigured):
        get_provider(service, env=env)


def test_accepts_service_name_string(no_credentials: None) -> None:
    assert isinstance(get_provider("youtube"), mocks.MockYouTube)


def test_llm_mock_is_a_spectracut_text_provider(no_credentials: None) -> None:
    from spectracut.providers.base import Completion, TextProvider

    provider = get_provider(Service.llm)
    assert isinstance(provider, TextProvider)
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "string"}, "minItems": 2}},
        "required": ["items"],
    }
    completion = provider.complete("system", "prompt", schema=schema)
    assert isinstance(completion, Completion)
    assert isinstance(completion.output, dict) and len(completion.output["items"]) == 2
    assert provider.prompts == ["prompt"]
