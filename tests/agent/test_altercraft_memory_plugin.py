"""Tests for the altercraft memory plugin (plugins/memory/altercraft/).

Round-trips writes/reads through the plugin tool surface against a tmp
HERMES_HOME so the tests never touch a real persona's data.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def tmp_hermes(tmp_path, monkeypatch):
    """Point HERMES_HOME at a tmp path so altercraft_memory writes there."""
    home = tmp_path / "hermes-home"
    (home / "profiles" / "altercraft-clio" / "memory").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def plugin():
    """Load the altercraft memory plugin once per test."""
    from plugins.memory import load_memory_provider
    p = load_memory_provider("altercraft")
    assert p is not None, "altercraft plugin failed to load"
    return p


def test_provider_loads_and_advertises_correctly(plugin):
    assert plugin.name == "altercraft"
    assert plugin.is_available() is True
    schemas = plugin.get_tool_schemas()
    names = {s["name"] for s in schemas}
    assert names == {
        "altercraft_recall_summary",
        "altercraft_remember_location",
        "altercraft_recall_locations",
        "altercraft_record_event",
        "altercraft_recall_events",
    }


def test_initialize_with_altercraft_prefix_sets_persona(plugin):
    plugin.initialize("session-1", agent_identity="altercraft-clio")
    assert plugin._persona == "clio"


def test_initialize_without_prefix_uses_full_name(plugin):
    plugin.initialize("session-1", agent_identity="mindcraft-andy")
    assert plugin._persona == "mindcraft-andy"


def test_initialize_with_no_identity_falls_back(plugin):
    plugin.initialize("session-1", agent_identity="")
    assert plugin._persona == "default"


def test_uninitialized_tool_call_returns_error(plugin):
    # Fresh instance has no _persona set.
    out = plugin.handle_tool_call("altercraft_recall_summary", {})
    parsed = json.loads(out)
    assert parsed["ok"] is False
    assert "not initialized" in parsed["error"].lower()


def test_remember_and_recall_location_roundtrip(plugin, tmp_hermes):
    plugin.initialize("session", agent_identity="altercraft-clio")

    # Write a location
    out = plugin.handle_tool_call("altercraft_remember_location", {
        "name": "cabin",
        "x": 100, "y": 64, "z": -50,
        "notes": "by the river",
    })
    assert json.loads(out)["ok"] is True

    # Read it back
    out = plugin.handle_tool_call("altercraft_recall_locations", {})
    parsed = json.loads(out)
    assert parsed["ok"] is True
    assert "cabin" in parsed["locations"]
    assert parsed["locations"]["cabin"]["x"] == 100
    assert parsed["locations"]["cabin"]["notes"] == "by the river"

    # File on disk
    locs_file = tmp_hermes / "profiles" / "altercraft-clio" / "memory" / "locations.json"
    assert locs_file.exists()
    saved = json.loads(locs_file.read_text())
    assert saved["cabin"]["x"] == 100


def test_remember_location_rejects_missing_name(plugin, tmp_hermes):
    plugin.initialize("session", agent_identity="altercraft-clio")
    out = plugin.handle_tool_call("altercraft_remember_location", {
        "x": 0, "y": 0, "z": 0,
    })
    parsed = json.loads(out)
    assert parsed["ok"] is False
    assert "name" in parsed["error"]


def test_record_and_recall_events_roundtrip(plugin, tmp_hermes):
    plugin.initialize("session", agent_identity="altercraft-clio")

    plugin.handle_tool_call("altercraft_record_event", {
        "type": "discovery",
        "description": "found a goblin camp",
        "details": {"x": 50, "y": 64, "z": 100},
    })
    plugin.handle_tool_call("altercraft_record_event", {
        "type": "death",
        "description": "killed by a creeper",
    })

    out = plugin.handle_tool_call("altercraft_recall_events", {"n": 5})
    parsed = json.loads(out)
    assert parsed["ok"] is True
    assert len(parsed["events"]) == 2
    types = [e["type"] for e in parsed["events"]]
    assert types == ["discovery", "death"]
    # ts auto-stamped
    for ev in parsed["events"]:
        assert "ts" in ev
    # details merged
    assert parsed["events"][0]["x"] == 50


def test_recall_events_default_limit_is_10(plugin, tmp_hermes):
    plugin.initialize("session", agent_identity="altercraft-clio")
    for i in range(15):
        plugin.handle_tool_call("altercraft_record_event", {
            "type": "tick", "description": f"event {i}",
        })

    out = plugin.handle_tool_call("altercraft_recall_events", {})
    parsed = json.loads(out)
    assert len(parsed["events"]) == 10
    # newest last
    assert parsed["events"][-1]["description"] == "event 14"


def test_recall_summary_includes_locations_and_events(plugin, tmp_hermes):
    plugin.initialize("session", agent_identity="altercraft-clio")
    plugin.handle_tool_call("altercraft_remember_location", {
        "name": "spawn", "x": 0, "y": 64, "z": 0,
    })
    plugin.handle_tool_call("altercraft_record_event", {
        "type": "build", "description": "placed first chest",
    })

    out = plugin.handle_tool_call("altercraft_recall_summary", {})
    parsed = json.loads(out)
    assert parsed["ok"] is True
    summary = parsed["summary"]
    assert "spawn" in summary
    assert "placed first chest" in summary or "[build]" in summary


def test_unknown_tool_returns_error(plugin, tmp_hermes):
    plugin.initialize("session", agent_identity="altercraft-clio")
    out = plugin.handle_tool_call("altercraft_does_not_exist", {})
    parsed = json.loads(out)
    assert parsed["ok"] is False
    assert "unknown tool" in parsed["error"].lower()


def test_system_prompt_block_includes_summary(plugin, tmp_hermes):
    plugin.initialize("session", agent_identity="altercraft-clio")
    plugin.handle_tool_call("altercraft_remember_location", {
        "name": "spawn", "x": 0, "y": 64, "z": 0,
    })
    block = plugin.system_prompt_block()
    assert "AlterCraft world memory" in block
    assert "spawn" in block


def test_system_prompt_block_empty_when_no_memory(plugin, tmp_hermes):
    plugin.initialize("session", agent_identity="altercraft-clio")
    # Brand-new persona, no writes yet
    block = plugin.system_prompt_block()
    # summarize_memory returns empty string when nothing's there; the
    # block guard returns "" in that case.
    assert block == ""


def test_persona_isolation_two_personas_dont_share(tmp_hermes):
    """Each persona's memory is isolated by directory."""
    from plugins.memory import load_memory_provider

    p1 = load_memory_provider("altercraft")
    p1.initialize("s", agent_identity="altercraft-clio")
    p1.handle_tool_call("altercraft_remember_location", {
        "name": "clio-place", "x": 1, "y": 1, "z": 1,
    })

    p2 = load_memory_provider("altercraft")
    p2.initialize("s", agent_identity="altercraft-erato")
    out = p2.handle_tool_call("altercraft_recall_locations", {})
    parsed = json.loads(out)
    assert parsed["ok"] is True
    # erato persona should not see clio's locations
    assert "clio-place" not in parsed["locations"]
