"""World database access — connection helpers + world identification.

Each world (a specific Minecraft server, e.g. `altercraft`) gets its own
SQLite file at::

    ~/.hermes/profiles/<persona>/memory/world-<world_name>.db

The persona scope mirrors `altercraft_memory.py`'s flat-JSON convention.
World identity is read from world_meta on first access; if missing, the
caller seeds it (typically migrate.py during initial migration, or the
plugin's initialize() if the persona starts fresh).

Spec:
    Alter-infra:docs/superpowers/specs/2026-04-29-scene-graph-narrative-memory.md
    §4 (World, persona, scope), §5 (Schema), Appendix B (Naming).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Schema file lives next to this module; loaded once on first connect.
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Default world name when no profile-specific override is set.
DEFAULT_WORLD = "altercraft"


# ─── Path helpers ──────────────────────────────────────────────────────


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _profile_dir(persona: str) -> Path:
    """Directory where world DBs live for a given persona scope.

    `persona` here is the directory key — for `altercraft-clio` profiles
    it's "clio" (post-prefix-strip); for unprefixed profiles it's the
    full profile name. This matches the AltercraftMemoryProvider
    convention so the new SQL store sits side-by-side with the existing
    flat JSON.
    """
    return _hermes_home() / "profiles" / f"altercraft-{persona}" / "memory"


def world_db_path(persona: str, world_name: str = DEFAULT_WORLD) -> Path:
    """Resolve the world DB path for a (persona, world) pair."""
    return _profile_dir(persona) / f"world-{world_name}.db"


# ─── Connection ────────────────────────────────────────────────────────


def connect(persona: str, world_name: str = DEFAULT_WORLD) -> sqlite3.Connection:
    """Open (and lazily create+migrate) the world DB for (persona, world).

    Idempotent: subsequent calls reuse the existing schema. On first
    create, executes schema.sql and seeds world_meta.
    """
    db_path = world_db_path(persona, world_name)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not db_path.exists()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # Foreign keys must be enabled per-connection.
    conn.execute("PRAGMA foreign_keys = ON")

    if is_new:
        _bootstrap(conn, world_name)
        logger.info("scene-graph world DB created: %s", db_path)
    else:
        # Verify schema version; future migrations apply here.
        _ensure_schema(conn, world_name)

    return conn


def _bootstrap(conn: sqlite3.Connection, world_name: str) -> None:
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema_sql)
    now = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO world_meta(key, value) VALUES (?, ?)",
        ("world_name", world_name),
    )
    conn.execute(
        "INSERT OR REPLACE INTO world_meta(key, value) VALUES (?, ?)",
        ("created_at", str(now)),
    )
    conn.commit()


def _ensure_schema(conn: sqlite3.Connection, world_name: str) -> None:
    """No-op on schema version 1; future versions add migrations here."""
    row = conn.execute(
        "SELECT value FROM world_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        # DB exists but isn't ours — refuse to mutate.
        raise RuntimeError(
            "world DB has no schema_version; refusing to migrate an unknown DB"
        )
    # Only one version exists. Add `if int(row[0]) < N: migrate_to_N(conn)` here.


# ─── Persona resolution ────────────────────────────────────────────────


def get_or_create_persona(
    conn: sqlite3.Connection,
    name: str,
    *,
    pos: Optional[Tuple[float, float, float]] = None,
) -> int:
    """Look up a persona by in-game username; create on first observation.

    Returns the persona id.
    """
    row = conn.execute(
        "SELECT id FROM personas WHERE name = ?", (name,)
    ).fetchone()
    if row is not None:
        return int(row["id"])
    bx, by, bz = pos or (None, None, None)
    cur = conn.execute(
        "INSERT INTO personas(name, birth_x, birth_y, birth_z, birth_ts) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, bx, by, bz, time.time()),
    )
    return int(cur.lastrowid)


# ─── Node helpers ──────────────────────────────────────────────────────


def upsert_node(
    conn: sqlite3.Connection,
    *,
    uri: str,
    type: str,
    name: Optional[str] = None,
    pos: Optional[Tuple[float, float, float]] = None,
    bbox: Optional[Tuple[Tuple[float, float, float], Tuple[float, float, float]]] = None,
    attrs: Optional[Dict[str, Any]] = None,
    observed_by: Optional[int] = None,
    salience: float = 0.5,
    pinned: bool = False,
) -> int:
    """Insert or update a node by URI. Returns the node id.

    Updates touch `last_seen` and merge `attrs`; do not overwrite name/
    type/observer once set unless explicit.
    """
    now = time.time()
    pos_x, pos_y, pos_z = pos or (None, None, None)
    if bbox is not None:
        (bxmin, bymin, bzmin), (bxmax, bymax, bzmax) = bbox
    else:
        bxmin = bymin = bzmin = bxmax = bymax = bzmax = None
    attrs_json = json.dumps(attrs or {}, sort_keys=True, default=str)

    row = conn.execute("SELECT id, attrs FROM nodes WHERE uri = ?", (uri,)).fetchone()
    if row is not None:
        node_id = int(row["id"])
        # Merge attrs: new keys win
        try:
            old_attrs = json.loads(row["attrs"] or "{}")
        except Exception:
            old_attrs = {}
        merged = {**old_attrs, **(attrs or {})}
        merged_json = json.dumps(merged, sort_keys=True, default=str)
        conn.execute(
            "UPDATE nodes SET "
            "name = COALESCE(?, name), "
            "pos_x = COALESCE(?, pos_x), pos_y = COALESCE(?, pos_y), pos_z = COALESCE(?, pos_z), "
            "bbox_min_x = COALESCE(?, bbox_min_x), bbox_min_y = COALESCE(?, bbox_min_y), bbox_min_z = COALESCE(?, bbox_min_z), "
            "bbox_max_x = COALESCE(?, bbox_max_x), bbox_max_y = COALESCE(?, bbox_max_y), bbox_max_z = COALESCE(?, bbox_max_z), "
            "attrs = ?, last_seen = ?, "
            "salience = MAX(salience, ?), pinned = MAX(pinned, ?) "
            "WHERE id = ?",
            (
                name,
                pos_x, pos_y, pos_z,
                bxmin, bymin, bzmin,
                bxmax, bymax, bzmax,
                merged_json, now,
                salience, 1 if pinned else 0,
                node_id,
            ),
        )
        return node_id
    cur = conn.execute(
        "INSERT INTO nodes("
        "uri, type, name, pos_x, pos_y, pos_z, "
        "bbox_min_x, bbox_min_y, bbox_min_z, bbox_max_x, bbox_max_y, bbox_max_z, "
        "attrs, first_seen, last_seen, observed_by, salience, pinned"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uri, type, name, pos_x, pos_y, pos_z,
            bxmin, bymin, bzmin, bxmax, bymax, bzmax,
            attrs_json, now, now, observed_by, salience, 1 if pinned else 0,
        ),
    )
    return int(cur.lastrowid)


# ─── Spatial queries ───────────────────────────────────────────────────


def query_near(
    conn: sqlite3.Connection,
    x: float,
    y: float,
    z: float,
    radius: float,
    *,
    types: Optional[Iterable[str]] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return nodes whose bbox overlaps the cube of side 2*radius around
    (x,y,z). Sorted by distance to centroid, ascending.

    Uses the R*Tree virtual table for O(log n) prefilter, then filters
    by exact distance in Python.
    """
    rmin_x, rmax_x = x - radius, x + radius
    rmin_y, rmax_y = y - radius, y + radius
    rmin_z, rmax_z = z - radius, z + radius
    sql = """
        SELECT n.* FROM nodes n
        JOIN nodes_spatial s ON s.id = n.id
        WHERE s.max_x >= ? AND s.min_x <= ?
          AND s.max_y >= ? AND s.min_y <= ?
          AND s.max_z >= ? AND s.min_z <= ?
    """
    params: List[Any] = [rmin_x, rmax_x, rmin_y, rmax_y, rmin_z, rmax_z]
    if types:
        type_list = list(types)
        placeholders = ",".join("?" for _ in type_list)
        sql += f" AND n.type IN ({placeholders})"
        params.extend(type_list)
    rows = conn.execute(sql, params).fetchall()

    def _distance(row: sqlite3.Row) -> float:
        nx, ny, nz = row["pos_x"], row["pos_y"], row["pos_z"]
        if nx is None or ny is None or nz is None:
            return float("inf")
        return ((nx - x) ** 2 + (ny - y) ** 2 + (nz - z) ** 2) ** 0.5

    sorted_rows = sorted(rows, key=_distance)
    out: List[Dict[str, Any]] = []
    for row in sorted_rows[:limit]:
        d = dict(row)
        # Decode attrs JSON for caller convenience.
        try:
            d["attrs"] = json.loads(d.get("attrs") or "{}")
        except Exception:
            d["attrs"] = {}
        d["distance"] = _distance(row)
        out.append(d)
    return out
