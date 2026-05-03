"""Tests for the altercraft Library DB layer (library.py)."""

from __future__ import annotations

import time
import importlib
from pathlib import Path

import pytest

import plugins.memory.altercraft.library as lib_mod
from plugins.memory.altercraft.library import (
    library_path,
    open_library,
    upsert_library_episode,
    search_library,
    get_recent_library_episodes,
)


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    """Redirect HERMES_HOME to tmp_path and reload the module's helper."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Force the module to pick up the env var at runtime (it reads os.environ)
    return tmp_path


@pytest.fixture()
def conn(hermes_home):
    c = open_library("testpersona")
    yield c
    c.close()


# ─── Tests ─────────────────────────────────────────────────────────────


def test_library_path_name_pattern(hermes_home):
    """library_path returns path with correct name pattern."""
    p = library_path("clio")
    assert p.name == "library-clio.db"
    assert "altercraft-clio" in str(p)
    assert "memory" in str(p)


def test_open_library_creates_db_and_tables(hermes_home):
    """open_library creates the DB file and tables."""
    c = open_library("clio")
    try:
        # File must exist
        assert library_path("clio").exists()
        # Both tables must be present
        tables = {
            row[0]
            for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'shadow')"
            ).fetchall()
        }
        assert "library_episodes" in tables
        # FTS virtual table shows up as 'table'
        fts_tables = {
            row[0]
            for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "episodes_fts" in fts_tables
    finally:
        c.close()


def test_upsert_inserts_and_retrieves(conn):
    """upsert_library_episode inserts and retrieves correctly."""
    upsert_library_episode(
        conn,
        episode_id="ep-001",
        world="altercraft",
        persona="testpersona",
        kind="place",
        summary="The iron mine near spawn",
        tags=["iron", "mine"],
    )
    rows = get_recent_library_episodes(conn, persona="testpersona")
    assert len(rows) == 1
    row = rows[0]
    assert row["episode_id"] == "ep-001"
    assert row["kind"] == "place"
    assert row["summary"] == "The iron mine near spawn"
    assert row["tags"] == "iron,mine"


def test_search_library_returns_matching_results(conn):
    """search_library returns matching results for a simple query."""
    upsert_library_episode(
        conn,
        episode_id="ep-002",
        world="altercraft",
        persona="testpersona",
        kind="adventure",
        summary="Fought the ender dragon with allies",
        tags=["dragon", "end"],
    )
    results = search_library(conn, query="dragon", persona="testpersona")
    assert len(results) >= 1
    episode_ids = [r["episode_id"] for r in results]
    assert "ep-002" in episode_ids


def test_search_library_returns_empty_for_no_match(conn):
    """search_library returns empty list for no match."""
    upsert_library_episode(
        conn,
        episode_id="ep-003",
        world="altercraft",
        persona="testpersona",
        kind="construction",
        summary="Built a big castle on the hill",
        tags=["castle"],
    )
    results = search_library(conn, query="unicorn", persona="testpersona")
    assert results == []


def test_get_recent_library_episodes_correct_order(conn):
    """get_recent_library_episodes returns correct (most-recent-first) order."""
    upsert_library_episode(
        conn, "ep-A", "w", "testpersona", "session", "First session", []
    )
    time.sleep(0.01)
    upsert_library_episode(
        conn, "ep-B", "w", "testpersona", "session", "Second session", []
    )
    rows = get_recent_library_episodes(conn, persona="testpersona")
    ids = [r["episode_id"] for r in rows]
    assert ids.index("ep-B") < ids.index("ep-A")


def test_get_recent_library_episodes_filters_by_kind(conn):
    """get_recent_library_episodes filters by kind correctly."""
    upsert_library_episode(
        conn, "ep-place-1", "w", "testpersona", "place", "A mountain", []
    )
    upsert_library_episode(
        conn, "ep-adv-1", "w", "testpersona", "adventure", "A dungeon", []
    )
    places = get_recent_library_episodes(conn, persona="testpersona", kind="place")
    kinds = {r["kind"] for r in places}
    assert kinds == {"place"}
    ids = [r["episode_id"] for r in places]
    assert "ep-adv-1" not in ids


def test_fts_trigger_fires_on_insert(conn):
    """FTS trigger fires: insert via upsert, search finds it."""
    upsert_library_episode(
        conn,
        episode_id="ep-trigger",
        world="altercraft",
        persona="testpersona",
        kind="place",
        summary="Discovered hidden nether fortress",
        tags=["nether", "fortress"],
    )
    results = search_library(conn, query="nether fortress", persona="testpersona")
    assert any(r["episode_id"] == "ep-trigger" for r in results)


def test_upsert_update_keeps_fts_in_sync(conn):
    """Updating an episode via upsert keeps FTS index consistent."""
    upsert_library_episode(
        conn, "ep-upd", "w", "testpersona", "place", "Original summary", []
    )
    # Verify original is findable
    assert search_library(conn, query="Original", persona="testpersona")

    # Update with new summary
    upsert_library_episode(
        conn, "ep-upd", "w", "testpersona", "place", "Completely new summary text", []
    )
    # New text must be found
    assert search_library(conn, query="Completely", persona="testpersona")
    # Old text must not be found for this episode
    old_results = search_library(conn, query="Original", persona="testpersona")
    assert not any(r["episode_id"] == "ep-upd" for r in old_results)
