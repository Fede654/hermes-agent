-- AlterCraft scene-graph schema (MVP)
--
-- Spec: Alter-infra:docs/superpowers/specs/2026-04-29-scene-graph-narrative-memory.md
--
-- This file is the source of truth for the world database; the plugin's
-- world.py loads it via executescript(). Triggers keep the R*Tree spatial
-- index in sync with nodes.bbox columns.
--
-- Schema versioning lives in world_meta(key='schema_version'). Migrations
-- bump the version and run additive DDL. Never destructive without explicit
-- user opt-in.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ────────────────────────────────────────────────────────────────────────
-- World identity (one row per key; world is a single identity per DB file)
-- ────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS world_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ────────────────────────────────────────────────────────────────────────
-- Personas — bots/identities active in this world
-- ────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS personas (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    birth_x REAL,
    birth_y REAL,
    birth_z REAL,
    birth_ts REAL,
    death_count INTEGER NOT NULL DEFAULT 0,
    total_blocks_placed INTEGER NOT NULL DEFAULT 0,
    total_distance REAL NOT NULL DEFAULT 0
);

-- ────────────────────────────────────────────────────────────────────────
-- Nodes — typed entities in the graph
-- ────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY,
    uri TEXT UNIQUE NOT NULL,
    type TEXT NOT NULL,
    name TEXT,
    pos_x REAL,
    pos_y REAL,
    pos_z REAL,
    bbox_min_x REAL,
    bbox_min_y REAL,
    bbox_min_z REAL,
    bbox_max_x REAL,
    bbox_max_y REAL,
    bbox_max_z REAL,
    attrs TEXT,                    -- JSON
    embedding BLOB,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    observed_by INTEGER REFERENCES personas(id),
    confidence REAL NOT NULL DEFAULT 1.0,
    salience REAL NOT NULL DEFAULT 0.5,
    pinned INTEGER NOT NULL DEFAULT 0,
    mood_history TEXT              -- JSON: {persona_id: [{ts, valence, intensity, reason, source}]}
);
CREATE INDEX IF NOT EXISTS nodes_type ON nodes(type);
CREATE INDEX IF NOT EXISTS nodes_pinned ON nodes(pinned, type);
CREATE INDEX IF NOT EXISTS nodes_last_seen ON nodes(last_seen);

-- R*Tree spatial index. Synced via triggers below.
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_spatial USING rtree(
    id,
    min_x, max_x,
    min_y, max_y,
    min_z, max_z
);

-- A node may have a centroid only (no bbox); for those we use a 1-block
-- bbox centered on the position so spatial queries still find them.
CREATE TRIGGER IF NOT EXISTS nodes_spatial_insert AFTER INSERT ON nodes
WHEN new.pos_x IS NOT NULL
BEGIN
    INSERT INTO nodes_spatial(id, min_x, max_x, min_y, max_y, min_z, max_z) VALUES (
        new.id,
        COALESCE(new.bbox_min_x, new.pos_x),
        COALESCE(new.bbox_max_x, new.pos_x),
        COALESCE(new.bbox_min_y, new.pos_y),
        COALESCE(new.bbox_max_y, new.pos_y),
        COALESCE(new.bbox_min_z, new.pos_z),
        COALESCE(new.bbox_max_z, new.pos_z)
    );
END;

CREATE TRIGGER IF NOT EXISTS nodes_spatial_update AFTER UPDATE OF
    pos_x, pos_y, pos_z,
    bbox_min_x, bbox_min_y, bbox_min_z,
    bbox_max_x, bbox_max_y, bbox_max_z
ON nodes
WHEN new.pos_x IS NOT NULL
BEGIN
    DELETE FROM nodes_spatial WHERE id = new.id;
    INSERT INTO nodes_spatial(id, min_x, max_x, min_y, max_y, min_z, max_z) VALUES (
        new.id,
        COALESCE(new.bbox_min_x, new.pos_x),
        COALESCE(new.bbox_max_x, new.pos_x),
        COALESCE(new.bbox_min_y, new.pos_y),
        COALESCE(new.bbox_max_y, new.pos_y),
        COALESCE(new.bbox_min_z, new.pos_z),
        COALESCE(new.bbox_max_z, new.pos_z)
    );
END;

CREATE TRIGGER IF NOT EXISTS nodes_spatial_delete AFTER DELETE ON nodes
BEGIN
    DELETE FROM nodes_spatial WHERE id = old.id;
END;

-- ────────────────────────────────────────────────────────────────────────
-- Edges — typed, time-versioned relations
-- ────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY,
    from_node INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    to_node INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    attrs TEXT,                    -- JSON
    created_at REAL NOT NULL,
    invalidated_at REAL,           -- NULL = currently valid
    source TEXT,                   -- 'observation' | 'inference' | 'told_by:<n>' | 'consolidator'
    confidence REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS edges_from ON edges(from_node, type, invalidated_at);
CREATE INDEX IF NOT EXISTS edges_to ON edges(to_node, type, invalidated_at);
CREATE INDEX IF NOT EXISTS edges_temporal ON edges(created_at, invalidated_at);

-- ────────────────────────────────────────────────────────────────────────
-- Episodes — events anchored in space and time
-- ────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    body TEXT,
    detail TEXT,                   -- JSON
    embedding BLOB,
    persona_id INTEGER NOT NULL REFERENCES personas(id),
    pos_x REAL,
    pos_y REAL,
    pos_z REAL,
    consolidated INTEGER NOT NULL DEFAULT 0,
    salience REAL NOT NULL DEFAULT 0.5
);
CREATE INDEX IF NOT EXISTS episodes_ts ON episodes(ts);
CREATE INDEX IF NOT EXISTS episodes_kind ON episodes(kind, ts);
CREATE INDEX IF NOT EXISTS episodes_persona_ts ON episodes(persona_id, ts);
CREATE INDEX IF NOT EXISTS episodes_unconsolidated ON episodes(consolidated, salience, ts);

-- Episode ↔ node anchors (many-to-many with role)
CREATE TABLE IF NOT EXISTS episode_nodes (
    episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    PRIMARY KEY (episode_id, node_id, role)
);
CREATE INDEX IF NOT EXISTS episode_nodes_node ON episode_nodes(node_id, role);

-- ────────────────────────────────────────────────────────────────────────
-- Schema version
-- ────────────────────────────────────────────────────────────────────────
INSERT OR REPLACE INTO world_meta(key, value)
VALUES ('schema_version', '1');
