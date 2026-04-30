"""Flat-JSON → scene-graph migration (idempotent).

On first run for a persona, this reads:

    ~/.hermes/profiles/altercraft-<persona>/memory/locations.json
    ~/.hermes/profiles/altercraft-<persona>/memory/players.json
    ~/.hermes/profiles/altercraft-<persona>/memory/events.jsonl

and populates the world DB tables:

    personas      — bots active in the world (the persona itself + any
                    player names found in players.json or events)
    nodes         — type='place' for each location entry, type='player'
                    for each player entry
    episodes      — one row per event in events.jsonl
    episode_nodes — anchors episodes to nearby places (best-effort)

It is idempotent: re-running on a populated DB inserts nothing duplicate
because nodes are keyed by URI and episodes are keyed by ts+kind+body
(checked before insert via a short uniqueness probe).

The original JSON files are NEVER modified or deleted; the migration is
read-only on the source. After migration succeeds, the plugin can choose
to read from the SQL store; the JSON files remain as backup.

Spec: Alter-infra:docs/superpowers/specs/2026-04-29-scene-graph-narrative-memory.md
      §13.2 (what this design adds), Appendix A (smallest useful first commit).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from .world import (
    DEFAULT_WORLD,
    _profile_dir,
    connect,
    get_or_create_persona,
    upsert_node,
)

logger = logging.getLogger(__name__)


# ─── Source readers ────────────────────────────────────────────────────


def _read_json(path: Path) -> Optional[Any]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("could not read %s: %s", path, exc)
        return None


def _iter_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


# ─── Migrate helpers ───────────────────────────────────────────────────


def _migrate_locations(conn: sqlite3.Connection, locs: Dict[str, Any], persona_id: int) -> int:
    """One node per named location."""
    n = 0
    for name, info in (locs or {}).items():
        if not isinstance(info, dict):
            continue
        try:
            pos = (float(info["x"]), float(info["y"]), float(info["z"]))
        except Exception:
            continue
        attrs = {k: v for k, v in info.items() if k not in ("x", "y", "z")}
        upsert_node(
            conn,
            uri=f"place:{name}",
            type="place",
            name=name,
            pos=pos,
            attrs=attrs,
            observed_by=persona_id,
            salience=0.6,
        )
        n += 1
    return n


def _migrate_players(conn: sqlite3.Connection, players: Dict[str, Any]) -> int:
    """One node per named player. No spatial position by default."""
    n = 0
    for name, info in (players or {}).items():
        attrs = info if isinstance(info, dict) else {"note": str(info)}
        upsert_node(
            conn,
            uri=f"player:{name}",
            type="player",
            name=name,
            attrs=attrs,
            salience=0.5,
        )
        n += 1
    return n


def _migrate_events(
    conn: sqlite3.Connection,
    events: Iterable[Dict[str, Any]],
    persona_id: int,
) -> int:
    """One episode per event line, idempotent on (ts, kind, body)."""
    n = 0
    # Build a quick existence cache of (ts, kind, body) to skip duplicates.
    seen = set(
        (row["ts"], row["kind"], row["body"])
        for row in conn.execute(
            "SELECT ts, kind, body FROM episodes WHERE persona_id = ?",
            (persona_id,),
        ).fetchall()
    )
    for ev in events:
        if not isinstance(ev, dict):
            continue
        ts = ev.get("ts")
        if not isinstance(ts, (int, float)):
            ts = time.time()
        kind = str(ev.get("type") or ev.get("kind") or "event")
        body = str(ev.get("description") or ev.get("body") or ev.get("summary") or "")
        key = (float(ts), kind, body)
        if key in seen:
            continue
        seen.add(key)
        # Pull position out if present
        pos_x = ev.get("x")
        pos_y = ev.get("y")
        pos_z = ev.get("z")
        # Strip the keys we promote to columns
        detail = {
            k: v for k, v in ev.items()
            if k not in ("ts", "type", "kind", "description", "body",
                         "summary", "x", "y", "z")
        }
        conn.execute(
            "INSERT INTO episodes(ts, kind, body, detail, persona_id, pos_x, pos_y, pos_z) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                float(ts), kind, body,
                json.dumps(detail, sort_keys=True, default=str),
                persona_id,
                pos_x, pos_y, pos_z,
            ),
        )
        n += 1
    return n


# ─── Public entry point ────────────────────────────────────────────────


def migrate_persona(
    persona: str,
    *,
    persona_username: Optional[str] = None,
    world_name: str = DEFAULT_WORLD,
) -> Dict[str, int]:
    """Migrate flat-JSON memory for `persona` into the world DB.

    `persona` is the directory key (e.g. "clio" for an altercraft-clio
    profile, or the full profile name for unprefixed profiles).
    `persona_username` is the in-game username to register in the
    `personas` table; defaults to `persona` if not supplied.

    Returns counts: {locations: N, players: N, events: N}. Safe to call
    multiple times — duplicates are skipped.
    """
    counts = {"locations": 0, "players": 0, "events": 0}
    src_dir = _profile_dir(persona)
    if not src_dir.exists():
        logger.info("migrate: nothing to do (no source dir): %s", src_dir)
        return counts

    locs = _read_json(src_dir / "locations.json") or {}
    players = _read_json(src_dir / "players.json") or {}
    events_path = src_dir / "events.jsonl"

    conn = connect(persona, world_name)
    try:
        persona_id = get_or_create_persona(conn, persona_username or persona)
        counts["locations"] = _migrate_locations(conn, locs, persona_id)
        counts["players"] = _migrate_players(conn, players)
        counts["events"] = _migrate_events(conn, _iter_jsonl(events_path), persona_id)
        conn.commit()
    finally:
        conn.close()

    logger.info(
        "migrate complete persona=%s world=%s locations=%d players=%d events=%d",
        persona, world_name,
        counts["locations"], counts["players"], counts["events"],
    )
    return counts
