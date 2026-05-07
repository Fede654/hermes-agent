"""Ensure credential refresh syncs the context compressor.

Regression test for: compressor keeps stale api_key after Kimi OAuth refresh,
causing 401 on context-compression summary generation.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

import run_agent


def _minimal_agent():
    """Return an AIAgent with a compressor, skipping heavy init."""
    agent = run_agent.AIAgent.__new__(run_agent.AIAgent)
    agent.model = "kimi-k2.6"
    agent.provider = "kimi-coding"
    agent.base_url = "https://api.kimi.com/coding/v1"
    agent.api_key = "OLD_TOKEN"
    agent.api_mode = "chat_completions"
    agent._client_kwargs = {
        "api_key": "OLD_TOKEN",
        "base_url": "https://api.kimi.com/coding/v1",
    }
    agent._primary_runtime = {
        "compressor_api_key": "OLD_TOKEN",
        "compressor_base_url": "https://api.kimi.com/coding/v1",
        "compressor_provider": "kimi-coding",
    }
    # Stub out client replacement
    agent._replace_primary_openai_client = lambda reason: True
    # Attach a real compressor
    from agent.context_compressor import ContextCompressor
    agent.context_compressor = ContextCompressor(
        model=agent.model,
        quiet_mode=True,
        base_url=agent.base_url,
        api_key=agent.api_key,
        provider=agent.provider,
    )
    return agent


def test_kimi_refresh_syncs_compressor_and_primary_runtime(monkeypatch):
    """After a successful Kimi credential refresh, the compressor must see the new key."""
    agent = _minimal_agent()

    def _fake_resolve(*, force_refresh=False, allow_api_key_fallback=False):
        return {
            "api_key": "NEW_TOKEN",
            "base_url": "https://api.kimi.com/coding/v1",
            "source": "kimi-cli-oauth-refresh",
        }

    monkeypatch.setattr(
        "hermes_cli.auth.resolve_kimi_coding_runtime_credentials",
        _fake_resolve,
    )
    monkeypatch.setattr(
        "hermes_cli.auth.kimi_coding_default_headers",
        lambda: {},
    )

    result = agent._try_refresh_kimi_client_credentials(force=True)

    assert result is True
    assert agent.api_key == "NEW_TOKEN"
    assert agent.context_compressor.api_key == "NEW_TOKEN"
    assert agent._primary_runtime["compressor_api_key"] == "NEW_TOKEN"


def test_kimi_refresh_no_compressor_does_not_crash(monkeypatch):
    """If the agent has no compressor attached, refresh should still succeed."""
    agent = _minimal_agent()
    del agent.context_compressor

    def _fake_resolve(*, force_refresh=False, allow_api_key_fallback=False):
        return {
            "api_key": "NEW_TOKEN",
            "base_url": "https://api.kimi.com/coding/v1",
            "source": "kimi-cli-oauth-refresh",
        }

    monkeypatch.setattr(
        "hermes_cli.auth.resolve_kimi_coding_runtime_credentials",
        _fake_resolve,
    )
    monkeypatch.setattr(
        "hermes_cli.auth.kimi_coding_default_headers",
        lambda: {},
    )

    result = agent._try_refresh_kimi_client_credentials(force=True)
    assert result is True
    assert agent.api_key == "NEW_TOKEN"
