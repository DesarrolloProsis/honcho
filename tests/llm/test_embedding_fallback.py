"""Same-model embedding fallback (ConfiguredEmbeddingFallbackSettings).

A backup embedding endpoint is only safe if it serves the same model as the
primary: vectors from another model live in a different space and would
silently corrupt similarity search against the existing index. These tests pin
the config guard, the credential resolution, and the client wrapper's rule of
falling back on provider failures only, never on input errors.
"""

from __future__ import annotations

import logging
import re
from typing import Any, final

import pytest
from pydantic import ValidationError

from src.config import (
    ConfiguredEmbeddingModelSettings,
    resolve_embedding_model_config,
)
from src.embedding_client import EmbeddingClient, EmbeddingTokenLimitError

# The pattern windows/health_check.py alerts on (fallback-active).
FALLBACK_LOG = re.compile(r"switching from .* to backup", re.IGNORECASE)


def _configured(**fallback: Any) -> ConfiguredEmbeddingModelSettings:
    return ConfiguredEmbeddingModelSettings.model_validate(
        {
            "model": "text-embedding-3-small",
            "transport": "openai",
            "overrides": {"api_key": "primary-key", "base_url": "https://primary/v1"},
            "fallback": fallback or None,
        }
    )


def test_fallback_serving_another_model_is_refused():
    with pytest.raises(ValidationError, match="same model"):
        _configured(model="text-embedding-3-large")


def test_fallback_with_same_model_resolves_with_its_own_endpoint_and_key(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("TEST_BACKUP_EMBED_KEY", "backup-key")
    resolved = resolve_embedding_model_config(
        _configured(
            model="text-embedding-3-small",
            overrides={
                "base_url": "https://backup/v1",
                "api_key_env": "TEST_BACKUP_EMBED_KEY",
            },
        )
    )
    assert resolved.api_key == "primary-key"
    assert resolved.fallback is not None
    assert resolved.fallback.model == "text-embedding-3-small"
    assert resolved.fallback.base_url == "https://backup/v1"
    assert resolved.fallback.api_key == "backup-key"


def test_no_fallback_configured_resolves_to_none():
    configured = ConfiguredEmbeddingModelSettings.model_validate(
        {"model": "text-embedding-3-small", "overrides": {"api_key": "k"}}
    )
    assert resolve_embedding_model_config(configured).fallback is None


@final
class _Fake:
    """Stand-in for _EmbeddingClient. Every vector it returns is [marker]."""

    def __init__(self, marker: float, error: Exception | None = None) -> None:
        self.transport: str = "openai"
        self.model: str = "text-embedding-3-small"
        self.marker: float = marker
        self.error: Exception | None = error
        self.calls: list[str] = []

    def _check(self, what: str) -> None:
        self.calls.append(what)
        if self.error is not None:
            raise self.error

    async def embed(self, query: str) -> list[float]:
        del query
        self._check("embed")
        return [self.marker]

    async def simple_batch_embed(
        self, texts: list[str], *, on_oversize: str
    ) -> list[list[float]]:
        del texts, on_oversize
        self._check("simple_batch_embed")
        return [[self.marker]]

    async def batch_embed(
        self, id_resource_dict: dict[str, str]
    ) -> dict[str, list[list[float]]]:
        del id_resource_dict
        self._check("batch_embed")
        return {"id": [[self.marker]]}


PRIMARY, BACKUP = 1.0, 2.0


def _wire(
    monkeypatch: pytest.MonkeyPatch, primary: _Fake, fallback: _Fake | None
) -> EmbeddingClient:
    # Patch the singleton INSTANCE, not the class. Another test in this
    # directory monkeypatches `_get_client` on the instance, and pytest undoes
    # that by setting the saved bound method back as an instance attribute,
    # which would shadow a class-level patch here and let these tests reach the
    # real client (and the network).
    wrapper = EmbeddingClient()
    monkeypatch.setattr(wrapper, "_get_client", lambda: primary)
    monkeypatch.setattr(wrapper, "_fallback_instance", fallback)
    return wrapper


async def _call(client: EmbeddingClient, method: str) -> float:
    """Invoke `method` and return the marker of whichever fake served it."""
    if method == "embed":
        return (await client.embed("q"))[0]
    if method == "simple_batch_embed":
        return (await client.simple_batch_embed(["a"]))[0][0]
    return (await client.batch_embed({"id": "a"}))["id"][0][0]


@pytest.mark.asyncio
async def test_healthy_primary_never_touches_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    primary, backup = _Fake(PRIMARY), _Fake(BACKUP)
    client = _wire(monkeypatch, primary, backup)
    assert await _call(client, "embed") == PRIMARY
    assert backup.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["embed", "simple_batch_embed", "batch_embed"])
async def test_provider_failure_switches_to_backup_and_logs_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, method: str
):
    primary = _Fake(PRIMARY, error=RuntimeError("401 PermissionDenied"))
    backup = _Fake(BACKUP)
    client = _wire(monkeypatch, primary, backup)
    with caplog.at_level(logging.WARNING, logger="src.embedding_client"):
        served_by = await _call(client, method)
    assert served_by == BACKUP
    assert primary.calls == [method] and backup.calls == [method]
    assert any(FALLBACK_LOG.search(r.getMessage()) for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [EmbeddingTokenLimitError("too long"), ValueError("dimension mismatch")],
)
async def test_input_errors_do_not_fall_back(
    monkeypatch: pytest.MonkeyPatch, error: Exception
):
    primary, backup = _Fake(PRIMARY, error=error), _Fake(BACKUP)
    client = _wire(monkeypatch, primary, backup)
    with pytest.raises(type(error)):
        await client.embed("q")
    assert backup.calls == []


@pytest.mark.asyncio
async def test_without_a_fallback_the_provider_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
):
    client = _wire(monkeypatch, _Fake(PRIMARY, error=RuntimeError("down")), None)
    with pytest.raises(RuntimeError, match="down"):
        await client.embed("q")
