"""Tests for the vendored dialogue-handoff plugin.

Verifies:
- Module-level path resolution from env vars (cascade + base derivation)
- post_llm_call writes DIALOGUE-HANDOFF.minecraft.md when platform=minecraft
- pre_llm_call injects <previous_session_context> on first-turn read-back
- Substantive-turn gate skips trivial echoes
- Per-platform separation: minecraft and cli get different files

Avoids the broader Hermes PluginManager harness by importing the plugin
module directly with monkeypatched env. The plugin is small and the
hook entry points (`_on_post_llm_call`, `_on_pre_llm_call`) are public
enough to call.

Spec: Alter-infra:docs/superpowers/specs/2026-04-29-scene-graph-narrative-memory.md §13.3a
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest


# The plugin module path (vendored under plugins/dialogue-handoff).
_PLUGIN_PATH = Path(__file__).parent.parent.parent / "plugins" / "dialogue-handoff" / "__init__.py"


@pytest.fixture
def fresh_plugin(tmp_path, monkeypatch):
    """Reload the plugin module with env pointing at tmp_path.

    The plugin resolves env vars at module import time, so each test
    needs a fresh module instance."""
    base = tmp_path / "agent-memory"
    state = base / "state"
    sessions = tmp_path / "hermes-home" / "sessions"
    state.mkdir(parents=True)
    sessions.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("HERMES_AGENT_MEMORY_BASE", str(base))
    # Drop any leftover module
    for mod_name in list(sys.modules):
        if "dialogue_handoff" in mod_name or mod_name.endswith("dialogue-handoff"):
            del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(
        "tests_dialogue_handoff_module", str(_PLUGIN_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, base


def test_paths_resolved_from_base(fresh_plugin, tmp_path):
    mod, base = fresh_plugin
    assert mod._HANDOFF_PATH == base / "state" / "DIALOGUE-HANDOFF.md"
    assert mod._ALWAYS_CONTEXT_PATH == base / "state" / "ALWAYS-CONTEXT.md"
    assert mod._SESSIONS_DIR == tmp_path / "hermes-home" / "sessions"
    assert mod._CONFIG_OK is True


def test_post_writes_minecraft_handoff_when_platform_kwarg_minecraft(fresh_plugin):
    mod, base = fresh_plugin
    # A clearly substantive turn (combined > 300 chars)
    user = "andy, where did you put the diamonds you mined yesterday near the cabin?" * 4
    asst = "i stashed them in the chest by the fireplace; want me to grab some?" * 4
    mod._on_post_llm_call(
        session_id="s1",
        user_message=user,
        assistant_response=asst,
        conversation_history=[],
        platform="minecraft",
    )
    mc_path = base / "state" / "DIALOGUE-HANDOFF.minecraft.md"
    assert mc_path.exists(), "minecraft handoff file not created"
    text = mc_path.read_text()
    assert "## Recent Exchanges" in text
    assert "USER:" in text
    assert "HERMES:" in text
    assert "platform: minecraft" in text


def test_post_does_not_overwrite_cli_when_platform_minecraft(fresh_plugin):
    mod, base = fresh_plugin
    cli_path = base / "state" / "DIALOGUE-HANDOFF.md"
    mc_path = base / "state" / "DIALOGUE-HANDOFF.minecraft.md"
    # Pre-populate the CLI file with a sentinel
    cli_path.write_text("CLI SENTINEL — do not touch")

    user = "x" * 200
    asst = "y" * 200
    mod._on_post_llm_call(
        session_id="s",
        user_message=user,
        assistant_response=asst,
        conversation_history=[],
        platform="minecraft",
    )
    assert cli_path.read_text() == "CLI SENTINEL — do not touch"
    assert mc_path.exists()


def test_substantive_gate_skips_trivial_turn(fresh_plugin):
    mod, base = fresh_plugin
    mc_path = base / "state" / "DIALOGUE-HANDOFF.minecraft.md"
    # Trivial turn: passes the >=3 chars sanity gate but combined <300
    # chars, so the substantive gate skips the TAIL update. The
    # metadata header IS still written.
    mod._on_post_llm_call(
        session_id="s",
        user_message="hello there",
        assistant_response="hi back",
        conversation_history=[],
        platform="minecraft",
    )
    text = mc_path.read_text()
    assert "substantive: false" in text
    # Tail should be empty (substantive gate kept it from being filled)
    assert "<!-- empty: no substantive turn recorded yet -->" in text


def test_substantive_gate_preserves_existing_tail_under_trivial_turn(fresh_plugin):
    mod, base = fresh_plugin
    mc_path = base / "state" / "DIALOGUE-HANDOFF.minecraft.md"
    # First, a substantive turn (>=300 chars combined)
    big = "x" * 200
    mod._on_post_llm_call(
        session_id="s1", user_message=big, assistant_response=big,
        conversation_history=[], platform="minecraft",
    )
    text_before = mc_path.read_text()
    assert "## Recent Exchanges" in text_before
    # Then a trivial turn — must NOT remove the tail. Use lengths that
    # pass the >=3 chars sanity gate but combined <300.
    mod._on_post_llm_call(
        session_id="s2", user_message="oki doki", assistant_response="cool then",
        conversation_history=[], platform="minecraft",
    )
    text_after = mc_path.read_text()
    # The Recent Exchanges section is still populated (not the empty
    # marker)
    assert "<!-- empty: no substantive turn recorded yet -->" not in text_after
    # The substantive=false flag is set on the latest header
    assert "substantive: false" in text_after


def test_pre_llm_call_injects_previous_session_context(fresh_plugin):
    mod, base = fresh_plugin
    # Write a substantive turn
    user = "where is the cabin you built last week?" * 8
    asst = "the cabin's at -120, 64, 50 by the river — half-collapsed twice while i was figuring out roofs" * 4
    mod._on_post_llm_call(
        session_id="s1", user_message=user, assistant_response=asst,
        conversation_history=[], platform="minecraft",
    )
    # Now simulate a fresh session, first turn
    result = mod._on_pre_llm_call(
        session_id="s2",
        user_message="andy, what were we doing?",
        conversation_history=[],
        is_first_turn=True,
        platform="minecraft",
    )
    assert result is not None
    assert "context" in result
    ctx = result["context"]
    assert "<previous_session_context>" in ctx
    assert "Recent exchanges" in ctx
    assert "cabin" in ctx


def test_pre_llm_call_returns_none_when_not_first_turn(fresh_plugin):
    mod, base = fresh_plugin
    # Even with a populated handoff, mid-session reads return None
    big = "x" * 200
    mod._on_post_llm_call(
        session_id="s1", user_message=big, assistant_response=big,
        conversation_history=[], platform="minecraft",
    )
    result = mod._on_pre_llm_call(
        session_id="s2",
        user_message="continuing question",
        conversation_history=[],
        is_first_turn=False,
        platform="minecraft",
    )
    assert result is None


def test_pre_llm_call_skips_slash_command(fresh_plugin):
    mod, base = fresh_plugin
    big = "x" * 200
    mod._on_post_llm_call(
        session_id="s1", user_message=big, assistant_response=big,
        conversation_history=[], platform="minecraft",
    )
    # Slash commands shouldn't trigger handoff injection
    result = mod._on_pre_llm_call(
        session_id="s2",
        user_message="/reset",
        conversation_history=[],
        is_first_turn=True,
        platform="minecraft",
    )
    assert result is None


def test_per_platform_isolation(fresh_plugin):
    """A minecraft handoff doesn't leak into a telegram session."""
    mod, base = fresh_plugin
    # Write a substantive minecraft turn
    user = "minecraft chat about diamonds" * 8
    asst = "answered about diamonds in chest by fireplace" * 4
    mod._on_post_llm_call(
        session_id="s1", user_message=user, assistant_response=asst,
        conversation_history=[], platform="minecraft",
    )
    # Read from the telegram side — should get nothing
    tg_result = mod._on_pre_llm_call(
        session_id="s2",
        user_message="hello",
        conversation_history=[],
        is_first_turn=True,
        platform="telegram",
    )
    if tg_result is not None:
        # If always_context is empty, plugin returns None; if non-empty,
        # the returned context should NOT mention diamonds
        assert "diamonds" not in tg_result.get("context", "")


def test_always_context_loaded_when_present(fresh_plugin):
    mod, base = fresh_plugin
    ac_path = base / "state" / "ALWAYS-CONTEXT.md"
    ac_path.write_text("you are the bot. respond in lowercase. never break character.")
    result = mod._on_pre_llm_call(
        session_id="s",
        user_message="hi",
        conversation_history=[],
        is_first_turn=True,
        platform="minecraft",
    )
    assert result is not None
    assert "<always_context>" in result["context"]
    assert "respond in lowercase" in result["context"]


def test_register_hooks(fresh_plugin):
    """Plugin's register(ctx) hooks both pre and post into the context."""
    mod, _ = fresh_plugin
    registered = []

    class FakeCtx:
        def register_hook(self, name, fn):
            registered.append((name, fn))

    mod.register(FakeCtx())
    names = sorted(n for n, _ in registered)
    assert names == ["post_llm_call", "pre_llm_call"]


def test_disabled_when_no_env(monkeypatch):
    """With no env set, the plugin disables itself and does not crash."""
    for var in ("HERMES_HOME", "HERMES_AGENT_MEMORY_BASE", "HERMES_HANDOFF_PATH",
                "HERMES_ALWAYS_CONTEXT_PATH", "HERMES_SESSIONS_DIR",
                "HMK_AGENT_MEMORY_BASE", "HMK_HERMES_HOME",
                "HMK_DIALOGUE_HANDOFF_PATH", "HMK_ALWAYS_CONTEXT_PATH",
                "HMK_SESSIONS_DIR", "HMK_BASE_DIR", "AGENT_MEMORY_BASE"):
        monkeypatch.delenv(var, raising=False)
    for mod_name in list(sys.modules):
        if "dialogue_handoff" in mod_name:
            del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(
        "tests_dialogue_handoff_disabled", str(_PLUGIN_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._CONFIG_OK is False
    # Hooks should be no-ops
    assert mod._on_post_llm_call(
        session_id="s", user_message="x" * 200, assistant_response="y" * 200,
        platform="minecraft",
    ) is None
    assert mod._on_pre_llm_call(
        session_id="s", user_message="hi",
        is_first_turn=True, platform="minecraft",
    ) is None
