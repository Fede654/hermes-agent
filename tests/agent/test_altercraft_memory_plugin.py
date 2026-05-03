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
        "altercraft_graph_query_near",
        "altercraft_consolidate_batch",
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


# ─── Scene-graph MVP tests (Phase 9 / Appendix A) ──────────────────────


class TestSceneGraphSchema:
    """Verify that initialize() sets up the SQL world DB cleanly."""

    def test_initialize_creates_world_db(self, plugin, tmp_hermes):
        plugin.initialize("session", agent_identity="altercraft-clio")
        from plugins.memory.altercraft.world import world_db_path
        path = world_db_path("clio")
        assert path.exists(), f"world DB not created at {path}"

    def test_world_db_has_schema_version(self, plugin, tmp_hermes):
        plugin.initialize("session", agent_identity="altercraft-clio")
        import sqlite3
        from plugins.memory.altercraft.world import world_db_path
        conn = sqlite3.connect(str(world_db_path("clio")))
        try:
            row = conn.execute(
                "SELECT value FROM world_meta WHERE key = 'schema_version'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "1"

    def test_world_db_has_required_tables(self, plugin, tmp_hermes):
        plugin.initialize("session", agent_identity="altercraft-clio")
        import sqlite3
        from plugins.memory.altercraft.world import world_db_path
        conn = sqlite3.connect(str(world_db_path("clio")))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type IN ('table', 'virtual')"
                ).fetchall()
            }
        finally:
            conn.close()
        # All core tables should be present.
        assert {"world_meta", "personas", "nodes", "edges",
                "episodes", "episode_nodes", "nodes_spatial"}.issubset(tables)


class TestMigration:
    """Idempotent flat-JSON → SQL migration."""

    def test_migration_imports_existing_locations(self, plugin, tmp_hermes):
        # Pre-seed flat JSON
        from agent.altercraft_memory import save_memory
        save_memory("clio", "locations", {
            "cabin": {"x": 100, "y": 64, "z": -50, "notes": "by the river"},
            "spawn": {"x": 0, "y": 64, "z": 0},
        })
        # Now initialize — migration should run
        plugin.initialize("session", agent_identity="altercraft-clio")

        # Verify both locations made it to nodes table
        import sqlite3
        from plugins.memory.altercraft.world import world_db_path
        conn = sqlite3.connect(str(world_db_path("clio")))
        try:
            rows = conn.execute(
                "SELECT uri, name, pos_x, pos_y, pos_z FROM nodes WHERE type='place' ORDER BY name"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 2
        names = {r[1] for r in rows}
        assert names == {"cabin", "spawn"}

    def test_migration_imports_events(self, plugin, tmp_hermes):
        from agent.altercraft_memory import append_event
        append_event("clio", {"type": "discovery", "description": "found a cave"})
        append_event("clio", {"type": "death", "description": "killed by skeleton"})
        plugin.initialize("session", agent_identity="altercraft-clio")

        import sqlite3
        from plugins.memory.altercraft.world import world_db_path
        conn = sqlite3.connect(str(world_db_path("clio")))
        try:
            rows = conn.execute(
                "SELECT kind, body FROM episodes ORDER BY kind"
            ).fetchall()
        finally:
            conn.close()
        kinds = sorted(r[0] for r in rows)
        assert kinds == ["death", "discovery"]

    def test_migration_is_idempotent(self, plugin, tmp_hermes):
        """Re-initializing on a populated DB does not duplicate rows."""
        from agent.altercraft_memory import append_event, save_memory
        save_memory("clio", "locations", {"x": {"x": 1, "y": 1, "z": 1}})
        append_event("clio", {"type": "discovery", "description": "ye"})

        plugin.initialize("s1", agent_identity="altercraft-clio")
        plugin.initialize("s2", agent_identity="altercraft-clio")

        import sqlite3
        from plugins.memory.altercraft.world import world_db_path
        conn = sqlite3.connect(str(world_db_path("clio")))
        try:
            n_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            n_eps = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        finally:
            conn.close()
        assert n_nodes == 1
        assert n_eps == 1


class TestDualWrite:
    """Existing tools should mirror writes into the SQL store."""

    def test_remember_location_dual_writes(self, plugin, tmp_hermes):
        plugin.initialize("session", agent_identity="altercraft-clio")
        plugin.handle_tool_call("altercraft_remember_location", {
            "name": "outpost", "x": 50, "y": 70, "z": 100, "notes": "north scout",
        })
        import sqlite3
        from plugins.memory.altercraft.world import world_db_path
        conn = sqlite3.connect(str(world_db_path("clio")))
        try:
            row = conn.execute(
                "SELECT name, pos_x, pos_y, pos_z, attrs FROM nodes WHERE uri='place:outpost'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "outpost"
        assert row[1] == 50.0
        assert row[2] == 70.0
        assert row[3] == 100.0
        assert "north scout" in row[4]

    def test_record_event_dual_writes(self, plugin, tmp_hermes):
        plugin.initialize("session", agent_identity="altercraft-clio")
        plugin.handle_tool_call("altercraft_record_event", {
            "type": "build",
            "description": "placed first chest",
            "details": {"x": 10, "y": 64, "z": 10},
        })
        import sqlite3
        from plugins.memory.altercraft.world import world_db_path
        conn = sqlite3.connect(str(world_db_path("clio")))
        try:
            rows = conn.execute(
                "SELECT kind, body, pos_x FROM episodes WHERE kind='build'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0][1] == "placed first chest"
        assert rows[0][2] == 10.0


class TestGraphQueryNear:
    """The new spatial query — the round-trip Appendix A asks for."""

    def test_query_near_finds_recent_writes(self, plugin, tmp_hermes):
        plugin.initialize("session", agent_identity="altercraft-clio")
        plugin.handle_tool_call("altercraft_remember_location", {
            "name": "cabin", "x": 100, "y": 64, "z": -50,
        })
        plugin.handle_tool_call("altercraft_remember_location", {
            "name": "tower", "x": 1000, "y": 64, "z": -50,  # far away
        })
        out = plugin.handle_tool_call("altercraft_graph_query_near", {
            "x": 105, "y": 64, "z": -45, "radius": 20,
        })
        parsed = json.loads(out)
        assert parsed["ok"] is True
        names = [n["name"] for n in parsed["nodes"]]
        assert "cabin" in names
        assert "tower" not in names

    def test_query_near_sorts_by_distance(self, plugin, tmp_hermes):
        plugin.initialize("session", agent_identity="altercraft-clio")
        plugin.handle_tool_call("altercraft_remember_location", {
            "name": "near", "x": 5, "y": 64, "z": 0,
        })
        plugin.handle_tool_call("altercraft_remember_location", {
            "name": "mid", "x": 30, "y": 64, "z": 0,
        })
        plugin.handle_tool_call("altercraft_remember_location", {
            "name": "far", "x": 60, "y": 64, "z": 0,
        })
        out = plugin.handle_tool_call("altercraft_graph_query_near", {
            "x": 0, "y": 64, "z": 0, "radius": 100,
        })
        parsed = json.loads(out)
        names = [n["name"] for n in parsed["nodes"]]
        assert names == ["near", "mid", "far"]

    def test_query_near_filters_types(self, plugin, tmp_hermes):
        plugin.initialize("session", agent_identity="altercraft-clio")
        plugin.handle_tool_call("altercraft_remember_location", {
            "name": "spawn", "x": 0, "y": 64, "z": 0,
        })
        # players have no spatial position by default — they shouldn't
        # show up in nearby queries.
        from plugins.memory.altercraft.world import connect, upsert_node
        conn = connect("clio")
        try:
            upsert_node(conn, uri="player:fede", type="player", name="fede")
            conn.commit()
        finally:
            conn.close()
        out = plugin.handle_tool_call("altercraft_graph_query_near", {
            "x": 0, "y": 64, "z": 0, "radius": 50, "types": ["place"],
        })
        parsed = json.loads(out)
        types = {n["type"] for n in parsed["nodes"]}
        assert types == {"place"}

    def test_query_near_rejects_missing_coords(self, plugin, tmp_hermes):
        plugin.initialize("session", agent_identity="altercraft-clio")
        out = plugin.handle_tool_call("altercraft_graph_query_near", {})
        parsed = json.loads(out)
        assert parsed["ok"] is False
        assert "x, y, z" in parsed["error"]

    def test_query_near_returns_distance(self, plugin, tmp_hermes):
        plugin.initialize("session", agent_identity="altercraft-clio")
        plugin.handle_tool_call("altercraft_remember_location", {
            "name": "p", "x": 3, "y": 4, "z": 0,
        })
        out = plugin.handle_tool_call("altercraft_graph_query_near", {
            "x": 0, "y": 0, "z": 0, "radius": 10,
        })
        parsed = json.loads(out)
        assert len(parsed["nodes"]) == 1
        # distance should be 5 (3-4-5 triangle)
        assert abs(parsed["nodes"][0]["distance"] - 5.0) < 1e-6

    def test_query_near_strips_embedding_blob(self, plugin, tmp_hermes):
        plugin.initialize("session", agent_identity="altercraft-clio")
        plugin.handle_tool_call("altercraft_remember_location", {
            "name": "p", "x": 0, "y": 0, "z": 0,
        })
        out = plugin.handle_tool_call("altercraft_graph_query_near", {
            "x": 0, "y": 0, "z": 0, "radius": 5,
        })
        parsed = json.loads(out)
        assert "embedding" not in parsed["nodes"][0]
        assert "mood_history" not in parsed["nodes"][0]


class TestPersonaIsolationSQL:
    """Same isolation guarantee as the JSON store, but for SQL."""

    def test_two_personas_get_different_dbs(self, tmp_hermes):
        from plugins.memory import load_memory_provider
        from plugins.memory.altercraft.world import world_db_path

        p1 = load_memory_provider("altercraft")
        p1.initialize("s", agent_identity="altercraft-clio")
        p1.handle_tool_call("altercraft_remember_location", {
            "name": "x", "x": 0, "y": 0, "z": 0,
        })

        p2 = load_memory_provider("altercraft")
        p2.initialize("s", agent_identity="altercraft-erato")

        # Query from erato's plugin — should see no nodes
        out = p2.handle_tool_call("altercraft_graph_query_near", {
            "x": 0, "y": 0, "z": 0, "radius": 10,
        })
        import json as _json
        parsed = _json.loads(out)
        assert parsed["ok"] is True
        assert parsed["nodes"] == []

        # And the DB files are separate
        assert world_db_path("clio") != world_db_path("erato")
        assert world_db_path("clio").exists()
        assert world_db_path("erato").exists()


# ─── Narrative context injection tests ─────────────────────────────────


class TestInjectNarrativeContext:
    """Tests for the narrative_context block injected by _inject_spatial_context."""

    def _make_messages(self):
        return [{"role": "user", "content": "hello"}]

    def test_inject_spatial_context_with_empty_library(self, plugin, tmp_hermes, monkeypatch):
        """When library has no episodes, only the spatial block is injected (if nodes present)."""
        import plugins.memory.altercraft.library as _lib
        plugin.initialize("session", agent_identity="altercraft-clio")
        plugin._last_position = (100.0, 64.0, -50.0)
        # Place a node near the position so spatial block fires
        plugin.handle_tool_call("altercraft_remember_location", {
            "name": "cabin", "x": 100, "y": 64, "z": -50,
        })

        monkeypatch.setattr(_lib, "get_recent_library_episodes", lambda conn, persona, kind=None, limit=10: [])

        result = plugin._inject_spatial_context(
            session_id="s", model="model", platform="platform", is_first_turn=True
        )
        assert result is not None
        content = result["context"]
        assert "<spatial_context>" in content
        assert "<narrative_context>" not in content

    def test_inject_narrative_context_with_library_episodes(self, plugin, tmp_hermes, monkeypatch):
        """When library has episodes, the narrative block appears in injected content."""
        import plugins.memory.altercraft.library as _lib
        plugin.initialize("session", agent_identity="altercraft-clio")
        plugin._last_position = None  # no position — only narrative

        fake_episodes = [
            {"kind": "adventure", "summary": "slew the dragon", "tags": "combat,dragon"},
            {"kind": "construction", "summary": "built a castle", "tags": "build"},
        ]
        monkeypatch.setattr(_lib, "get_recent_library_episodes", lambda conn, persona, kind=None, limit=10: fake_episodes)

        result = plugin._inject_spatial_context(
            session_id="s", model="model", platform="platform", is_first_turn=True
        )
        assert result is not None
        content = result["context"]
        assert "<narrative_context>" in content
        assert "slew the dragon" in content
        assert "built a castle" in content
        assert "<spatial_context>" not in content

    def test_inject_returns_none_when_no_position_and_empty_library(
        self, plugin, tmp_hermes, monkeypatch
    ):
        """Returns None when neither spatial nodes nor narrative episodes are available."""
        import plugins.memory.altercraft.library as _lib
        plugin.initialize("session", agent_identity="altercraft-clio")
        plugin._last_position = None  # no position

        monkeypatch.setattr(_lib, "get_recent_library_episodes", lambda conn, persona, kind=None, limit=10: [])

        result = plugin._inject_spatial_context(
            session_id="s", model="model", platform="platform", is_first_turn=True
        )
        assert result is None


# ─── Consolidate-batch tool tests ───────────────────────────────────────────


class TestActionResultHook:
    """_on_action_result persists construction/adventure episodes via mc_action_result."""

    def _call_hook(self, plugin, tool_name: str, result_data: dict):
        return plugin._on_action_result(
            tool_name=tool_name,
            args={},
            result=json.dumps(result_data),
            task_id="t1",
            session_id="s1",
            tool_call_id="tc1",
            duration_ms=100,
        )

    def test_ignores_wrong_tool_name(self, plugin, tmp_hermes):
        plugin.initialize("session", agent_identity="altercraft-clio")
        # Any tool_name other than "mc_action_result" must be a no-op
        result = self._call_hook(plugin, "mc_perceive", {"ok": True, "label": "place block"})
        assert result is None

    def test_ignores_failed_action(self, plugin, tmp_hermes):
        plugin.initialize("session", agent_identity="altercraft-clio")
        result = self._call_hook(plugin, "mc_action_result", {"ok": False, "label": "place block"})
        assert result is None

    def test_ignores_unknown_label(self, plugin, tmp_hermes):
        plugin.initialize("session", agent_identity="altercraft-clio")
        result = self._call_hook(plugin, "mc_action_result", {"ok": True, "label": "chat with villager"})
        assert result is None

    def test_construction_episode_persisted(self, plugin, tmp_hermes):
        plugin.initialize("session", agent_identity="altercraft-clio")
        self._call_hook(plugin, "mc_action_result", {
            "ok": True, "label": "place oak_log", "goal": "build cabin", "duration_ms": 200,
        })
        import sqlite3
        from plugins.memory.altercraft.world import world_db_path
        conn = sqlite3.connect(str(world_db_path("clio")))
        try:
            rows = conn.execute(
                "SELECT kind, body FROM episodes WHERE kind='construction'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert "place oak_log" in rows[0][1]

    def test_adventure_episode_persisted(self, plugin, tmp_hermes):
        plugin.initialize("session", agent_identity="altercraft-clio")
        self._call_hook(plugin, "mc_action_result", {
            "ok": True, "label": "mine iron_ore", "goal": "gather resources", "duration_ms": 500,
        })
        import sqlite3
        from plugins.memory.altercraft.world import world_db_path
        conn = sqlite3.connect(str(world_db_path("clio")))
        try:
            rows = conn.execute(
                "SELECT kind, body FROM episodes WHERE kind='adventure'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert "mine iron_ore" in rows[0][1]

    def test_returns_none_always(self, plugin, tmp_hermes):
        """Hook must return None (no result mutation)."""
        plugin.initialize("session", agent_identity="altercraft-clio")
        result = self._call_hook(plugin, "mc_action_result", {
            "ok": True, "label": "craft pickaxe",
        })
        assert result is None


class TestConsolidateBatch:
    def test_consolidate_batch_in_tool_registry(self, plugin):
        names = {s["name"] for s in plugin.get_tool_schemas()}
        assert "altercraft_consolidate_batch" in names

    def test_consolidate_batch_returns_consolidated_string(self, plugin, tmp_hermes, monkeypatch):
        from plugins.memory.altercraft import consolidator as _con

        monkeypatch.setattr(
            _con,
            "run_consolidator",
            lambda persona, dry_run=False: {"consolidated": 3, "skipped": 1, "dry_run": dry_run},
        )
        plugin.initialize("session", agent_identity="altercraft-clio")
        out = plugin.handle_tool_call("altercraft_consolidate_batch", {})
        assert "consolidated" in out.lower()
        assert "3" in out
