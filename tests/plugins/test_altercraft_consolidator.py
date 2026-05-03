"""Tests for the Altercraft Consolidator (consolidator.py)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

# ─── Import target ─────────────────────────────────────────────────────────────

import plugins.memory.altercraft.consolidator as con_mod
from plugins.memory.altercraft.consolidator import (
    get_unconsolidated_episodes,
    consolidate_batch,
    run_consolidator,
    _chunk_key,
    _parse_pos,
)


# ─── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    """Redirect HERMES_HOME to tmp_path so world.py / library.py write there."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def world_conn(hermes_home):
    """Open a fresh world DB for persona 'tester'."""
    from plugins.memory.altercraft.world import connect

    conn = connect("tester")
    yield conn
    conn.close()


@pytest.fixture()
def library_conn(hermes_home):
    """Open a fresh library DB for persona 'tester'."""
    from plugins.memory.altercraft.library import open_library

    conn = open_library("tester")
    yield conn
    conn.close()


@pytest.fixture()
def persona_id(world_conn):
    """Return (world_conn, persona_id) for persona 'tester'."""
    from plugins.memory.altercraft.world import get_or_create_persona

    return get_or_create_persona(world_conn, "tester")


def _insert_perceive(
    conn: sqlite3.Connection,
    persona_id: int,
    body: str,
    detail: Optional[str] = None,
    consolidated: int = 0,
    ts: Optional[float] = None,
) -> int:
    """Helper: insert a raw perceive episode. Returns inserted id."""
    ts = ts or time.time()
    cur = conn.execute(
        """INSERT INTO episodes(ts, kind, body, detail, persona_id, consolidated)
           VALUES (?, 'perceive', ?, ?, ?, ?)""",
        (ts, body, detail, persona_id, consolidated),
    )
    conn.commit()
    return cur.lastrowid


# ─── Test 1: get_unconsolidated_episodes returns perceive episodes ──────────────


def test_get_unconsolidated_returns_perceive(world_conn, persona_id):
    """get_unconsolidated_episodes returns episodes with kind='perceive' and consolidated=0."""
    _insert_perceive(world_conn, persona_id, "mc_perceive at (100,64,-200)")
    _insert_perceive(world_conn, persona_id, "mc_perceive at (101,64,-200)")

    rows = get_unconsolidated_episodes(world_conn, persona_id)
    assert len(rows) == 2
    for r in rows:
        assert r["kind"] == "perceive"
        assert r["persona_id"] == persona_id


# ─── Test 2: get_unconsolidated_episodes skips consolidated episodes ────────────


def test_get_unconsolidated_skips_consolidated(world_conn, persona_id):
    """Episodes with consolidated=1 or detail starting '[consolidated]' are excluded."""
    # Unconsolidated — should appear
    _insert_perceive(world_conn, persona_id, "mc_perceive at (200,64,-200)")
    # consolidated flag set
    _insert_perceive(
        world_conn, persona_id, "mc_perceive at (300,64,-200)", consolidated=1
    )
    # detail prefix (legacy mark)
    _insert_perceive(
        world_conn,
        persona_id,
        "mc_perceive at (400,64,-200)",
        detail="[consolidated] was here",
    )

    rows = get_unconsolidated_episodes(world_conn, persona_id)
    assert len(rows) == 1
    assert rows[0]["body"] == "mc_perceive at (200,64,-200)"


# ─── Test 3: consolidate_batch returns correct stats dict keys ─────────────────


def test_consolidate_batch_stat_keys(world_conn, library_conn, persona_id):
    """consolidate_batch always returns dict with processed/consolidated/skipped."""
    result = consolidate_batch(world_conn, library_conn, "tester", persona_id)
    assert isinstance(result, dict)
    assert "processed" in result
    assert "consolidated" in result
    assert "skipped" in result


# ─── Test 4: consolidate_batch marks source episodes consolidated ───────────────


def test_consolidate_batch_marks_episodes(world_conn, library_conn, persona_id):
    """After consolidation, source episodes should have consolidated=1."""
    base_ts = time.time()
    for i in range(4):
        _insert_perceive(
            world_conn,
            persona_id,
            f"mc_perceive at (100,64,-200)",
            ts=base_ts + i,
        )

    stats = consolidate_batch(world_conn, library_conn, "tester", persona_id)
    assert stats["consolidated"] == 4

    # Verify DB rows are marked
    still_raw = world_conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE persona_id=? AND consolidated=0",
        (persona_id,),
    ).fetchone()[0]
    assert still_raw == 0


# ─── Test 5: dry_run does not write to library or mark episodes ─────────────────


def test_consolidate_batch_dry_run(world_conn, library_conn, persona_id):
    """dry_run=True: library stays empty and episodes remain unconsolidated."""
    base_ts = time.time()
    for i in range(3):
        _insert_perceive(
            world_conn,
            persona_id,
            "mc_perceive at (500,64,-500)",
            ts=base_ts + i,
        )

    stats = consolidate_batch(
        world_conn, library_conn, "tester", persona_id, dry_run=True
    )
    # Stats still counts what *would* be consolidated
    assert stats["consolidated"] == 3

    # Library must remain empty
    lib_rows = library_conn.execute(
        "SELECT COUNT(*) FROM library_episodes"
    ).fetchone()[0]
    assert lib_rows == 0

    # World episodes must remain unconsolidated
    raw_count = world_conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE persona_id=? AND consolidated=0",
        (persona_id,),
    ).fetchone()[0]
    assert raw_count == 3


# ─── Test 6: run_consolidator returns stats dict on empty DB ───────────────────


def test_run_consolidator_empty_db(hermes_home):
    """run_consolidator returns a valid stats dict even when DB is empty."""
    stats = run_consolidator("tester")
    assert isinstance(stats, dict)
    assert stats["processed"] == 0
    assert stats["consolidated"] == 0
    assert stats["skipped"] == 0


# ─── Test 7: position grouping — same chunk boundary ──────────────────────────


def test_position_grouping_same_chunk():
    """(100,64,-200) and (104,64,-195) both map to chunk (6,-13)."""
    pos1 = _parse_pos("mc_perceive at (100,64,-200)")
    pos2 = _parse_pos("mc_perceive at (104,64,-195)")
    assert pos1 is not None
    assert pos2 is not None

    ck1 = _chunk_key(pos1[0], pos1[2])
    ck2 = _chunk_key(pos2[0], pos2[2])

    # Both should be in chunk (6, -13)
    assert ck1 == (6, -13), f"Expected (6,-13), got {ck1}"
    assert ck2 == (6, -13), f"Expected (6,-13), got {ck2}"
    assert ck1 == ck2
