"""Library DB — FTS5-indexed long-term episode summaries.

Each persona gets a ``library-<persona>.db`` SQLite file that lives
alongside the world DB in the same memory directory::

    ~/.hermes/profiles/altercraft-<persona>/memory/library-<persona>.db

The library stores consolidated episode summaries and exposes FTS5
full-text search for long-range recall.

Spec:
    Alter-infra:docs/superpowers/specs/2026-04-29-scene-graph-narrative-memory.md
    §6 (Library), Appendix B (Naming).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─── Path helpers ──────────────────────────────────────────────────────


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _profile_dir(persona: str) -> Path:
    return _hermes_home() / "profiles" / f"altercraft-{persona}" / "memory"


def library_path(persona: str) -> Path:
    """Return path to library-<persona>.db (sibling of world.db)."""
    return _profile_dir(persona) / f"library-{persona}.db"


# ─── Connection ────────────────────────────────────────────────────────


def open_library(persona: str) -> sqlite3.Connection:
    """Open (and initialize if needed) the library DB. Returns connection."""
    try:
        path = library_path(persona)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        library_schema(conn)
        return conn
    except Exception:
        logger.warning("open_library failed for persona=%s", persona, exc_info=True)
        raise


def library_schema(conn: sqlite3.Connection) -> None:
    """Create tables if not exist."""
    try:
        conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
                episode_id UNINDEXED,
                world,
                persona,
                kind,
                summary,
                tags
            );

            CREATE TABLE IF NOT EXISTS library_episodes (
                id INTEGER PRIMARY KEY,
                episode_id TEXT NOT NULL,
                world TEXT NOT NULL,
                persona TEXT NOT NULL,
                kind TEXT NOT NULL,
                summary TEXT NOT NULL,
                tags TEXT,
                ts_created REAL NOT NULL,
                ts_updated REAL NOT NULL
            );

            CREATE TRIGGER IF NOT EXISTS library_episodes_ai
              AFTER INSERT ON library_episodes BEGIN
                INSERT INTO episodes_fts(episode_id, world, persona, kind, summary, tags)
                VALUES (new.episode_id, new.world, new.persona, new.kind, new.summary, new.tags);
              END;
        """)
        conn.commit()
    except Exception:
        logger.warning("library_schema failed", exc_info=True)


# ─── Write ─────────────────────────────────────────────────────────────


def upsert_library_episode(
    conn: sqlite3.Connection,
    episode_id: str,
    world: str,
    persona: str,
    kind: str,
    summary: str,
    tags: list[str] = None,
) -> None:
    """Insert or update a library episode + keep FTS in sync."""
    try:
        tags_str = ",".join(tags) if tags else None
        now = time.time()

        # Check if it exists already
        row = conn.execute(
            "SELECT ts_created FROM library_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()

        if row is not None:
            ts_created = row["ts_created"]
            # Remove old FTS entry before replacing
            conn.execute(
                "DELETE FROM episodes_fts WHERE episode_id = ?", (episode_id,)
            )
            conn.execute(
                "DELETE FROM library_episodes WHERE episode_id = ?", (episode_id,)
            )
        else:
            ts_created = now

        conn.execute(
            """INSERT INTO library_episodes
               (episode_id, world, persona, kind, summary, tags, ts_created, ts_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (episode_id, world, persona, kind, summary, tags_str, ts_created, now),
        )
        conn.commit()
    except Exception:
        logger.warning(
            "upsert_library_episode failed episode_id=%s", episode_id, exc_info=True
        )


# ─── Read ──────────────────────────────────────────────────────────────


def search_library(
    conn: sqlite3.Connection,
    query: str,
    persona: str,
    limit: int = 10,
) -> list[dict]:
    """FTS5 full-text search. Returns list of {episode_id, kind, summary, tags, rank}."""
    try:
        rows = conn.execute(
            """SELECT le.episode_id, le.kind, le.summary, le.tags, fts.rank
               FROM episodes_fts fts
               JOIN library_episodes le ON fts.episode_id = le.episode_id
               WHERE episodes_fts MATCH ? AND le.persona = ?
               ORDER BY rank
               LIMIT ?""",
            (query, persona, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.warning("search_library failed query=%r", query, exc_info=True)
        return []


def get_recent_library_episodes(
    conn: sqlite3.Connection,
    persona: str,
    kind: str = None,
    limit: int = 10,
) -> list[dict]:
    """Return most recently updated episodes, optionally filtered by kind."""
    try:
        if kind is not None:
            rows = conn.execute(
                """SELECT * FROM library_episodes
                   WHERE persona = ? AND kind = ?
                   ORDER BY ts_updated DESC
                   LIMIT ?""",
                (persona, kind, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM library_episodes
                   WHERE persona = ?
                   ORDER BY ts_updated DESC
                   LIMIT ?""",
                (persona, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.warning(
            "get_recent_library_episodes failed persona=%s", persona, exc_info=True
        )
        return []
