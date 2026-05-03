"""AlterCraft world memory plugin — MemoryProvider for embodied Minecraft personas.

Wraps the existing flat-JSON memory store in `agent/altercraft_memory.py`
(locations / players / events / preferences / strategies) and exposes it
through the standard MemoryProvider interface so AlterCraft personas can
opt in to it via `memory.provider: altercraft` in their profile config.

Activation:
  Set in `~/.hermes/profiles/<profile>/config.yaml`:
    profile:
      memory:
        provider: altercraft

  The plugin keys off `agent_identity` (profile name) at initialize time
  to scope writes to `~/.hermes/profiles/<agent_identity>/memory/`.

  For profiles whose name starts with `altercraft-`, the persona is
  inferred by stripping the prefix (so `altercraft-clio` → persona
  `clio`, files at `~/.hermes/profiles/altercraft-clio/memory/`).
  For other profile names, the plugin uses the profile name verbatim
  (e.g. `mindcraft-andy` → `~/.hermes/profiles/mindcraft-andy/memory/`).

Tools exposed:
  altercraft_recall_summary  — return summarize_memory text
  altercraft_remember_location — write a location to locations.json
  altercraft_recall_locations — read locations.json
  altercraft_record_event    — append an event to events.jsonl
  altercraft_recall_events   — read recent events from events.jsonl
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)


# ─── Tool schemas ──────────────────────────────────────────────────────────

_RECALL_SUMMARY_SCHEMA = {
    "name": "altercraft_recall_summary",
    "description": (
        "Return a compact paragraph summarising AlterCraft world memory "
        "(known players, locations, preferences, strategies, recent events). "
        "Use at the start of a conversation or when context resets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "max_chars": {
                "type": "integer",
                "description": "Soft cap on summary length (default 1200).",
            },
        },
        "required": [],
    },
}

_REMEMBER_LOCATION_SCHEMA = {
    "name": "altercraft_remember_location",
    "description": (
        "Save a named location in AlterCraft world memory. Overwrites if a "
        "location with the same name already exists."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Location handle (e.g. 'cabin', 'spawn', 'goblin-camp')."},
            "x": {"type": "number"},
            "y": {"type": "number"},
            "z": {"type": "number"},
            "notes": {"type": "string", "description": "Optional human-readable notes about the place."},
        },
        "required": ["name", "x", "y", "z"],
    },
}

_RECALL_LOCATIONS_SCHEMA = {
    "name": "altercraft_recall_locations",
    "description": "Return all known AlterCraft locations as a dict of name → record.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

_RECORD_EVENT_SCHEMA = {
    "name": "altercraft_record_event",
    "description": (
        "Append an event to events.jsonl. Use for narrative beats: deaths, "
        "discoveries, conversations, builds, conflicts. Auto-stamps `ts`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "description": "Event kind (e.g. 'death', 'build', 'trade', 'discovery', 'conversation')."},
            "description": {"type": "string", "description": "Short human-readable description."},
            "details": {
                "type": "object",
                "description": "Free-form structured detail (positions, players, items, etc.).",
            },
        },
        "required": ["type", "description"],
    },
}

_RECALL_EVENTS_SCHEMA = {
    "name": "altercraft_recall_events",
    "description": "Return the N most recent events from events.jsonl (newest last).",
    "parameters": {
        "type": "object",
        "properties": {
            "n": {"type": "integer", "description": "Max number of events (default 10)."},
        },
        "required": [],
    },
}

# ─── Scene-graph (MVP) tool schema ────────────────────────────────────────
# Spec: Alter-infra:docs/superpowers/specs/2026-04-29-scene-graph-narrative-memory.md
# Appendix A — first useful commit.

_GRAPH_QUERY_NEAR_SCHEMA = {
    "name": "altercraft_graph_query_near",
    "description": (
        "Spatial query against the AlterCraft scene graph. Returns nodes "
        "(places, players, structures, etc.) whose bbox overlaps a cube "
        "of side 2*radius around (x, y, z), sorted by distance. Use this "
        "when the player asks 'what's around here', or when you need to "
        "know whether you're near a known landmark before acting."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "x": {"type": "number"},
            "y": {"type": "number"},
            "z": {"type": "number"},
            "radius": {"type": "number", "description": "Search radius in blocks (default 50)."},
            "types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional filter (e.g. ['place', 'player', 'construction']).",
            },
            "limit": {"type": "integer", "description": "Max nodes returned (default 20)."},
        },
        "required": ["x", "y", "z"],
    },
}


_CONSOLIDATE_BATCH_SCHEMA = {
    "name": "altercraft_consolidate_batch",
    "description": (
        "Trigger a narrative-consolidation pass over recent AlterCraft episodes. "
        "Reads raw events from the episode store, groups them into thematic clusters, "
        "and writes consolidated library entries. Use after a session ends or when "
        "the event log is getting long. Returns a summary of how many episodes were "
        "consolidated."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "dry_run": {
                "type": "boolean",
                "description": "If true, report what would be consolidated without writing.",
                "default": False,
            },
        },
        "required": [],
    },
}


# ─── Provider ──────────────────────────────────────────────────────────────


class AltercraftMemoryProvider(MemoryProvider):
    """MemoryProvider wrapping agent/altercraft_memory.py JSON store."""

    @property
    def name(self) -> str:
        return "altercraft"

    def __init__(self) -> None:
        self._persona: Optional[str] = None
        self._initialized: bool = False
        self._last_position: Optional[tuple] = None  # (x, y, z)
        self._ctx = None  # set by register()

    # ── Lifecycle ────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Always available — no external deps. Persona scoping happens
        at initialize time via agent_identity."""
        try:
            import agent.altercraft_memory  # noqa: F401
            return True
        except Exception:
            return False

    def initialize(self, session_id: str, **kwargs) -> None:
        agent_identity = kwargs.get("agent_identity") or ""
        # `altercraft-clio` → persona `clio`; anything else stays as-is so
        # the memory dir lands at ~/.hermes/profiles/<agent_identity>/memory/.
        if agent_identity.startswith("altercraft-"):
            self._persona = agent_identity[len("altercraft-"):]
        else:
            self._persona = agent_identity or "default"
        self._initialized = True
        logger.info(
            "altercraft memory provider initialized: persona=%s session=%s",
            self._persona, session_id,
        )
        # MVP scene-graph migration: idempotent flat-JSON → SQL on first
        # run. Subsequent runs find the DB present and migrate-skip
        # duplicate rows.
        try:
            from .migrate import migrate_persona
            counts = migrate_persona(self._persona, persona_username=self._persona)
            if any(counts.values()):
                logger.info(
                    "altercraft scene-graph migration: persona=%s %s",
                    self._persona, counts,
                )
        except Exception as exc:
            logger.warning(
                "altercraft scene-graph migration skipped: %s", exc,
            )

        # Register hooks. During live sessions the ctx passed via register() is a
        # _ProviderCollector whose register_hook is a no-op — hooks must be wired
        # directly into the global plugin manager so invoke_hook() can find them.
        hooks_registered = False
        if self._ctx is not None:
            try:
                from hermes_cli.plugins import _manager as _pm
                _pm._hooks.setdefault("transform_tool_result", []).append(self._on_perceive)
                _pm._hooks.setdefault("transform_tool_result", []).append(self._on_action_result)
                _pm._hooks.setdefault("pre_llm_call", []).append(self._inject_spatial_context)
                hooks_registered = True
                logger.debug("altercraft: hooks registered via global plugin manager")
            except Exception as _he:
                logger.debug("altercraft: global plugin manager unavailable (%s), falling back to ctx", _he)
                self._ctx.register_hook("transform_tool_result", self._on_perceive)
                self._ctx.register_hook("transform_tool_result", self._on_action_result)
                self._ctx.register_hook("pre_llm_call", self._inject_spatial_context)
                hooks_registered = True
        if not hooks_registered:
            logger.warning("altercraft: no hook registration path available")

    def shutdown(self) -> None:
        # Deregister hooks from global plugin manager to avoid stale callbacks.
        try:
            from hermes_cli.plugins import _manager as _pm
            for hook_name, cb in [
                ("transform_tool_result", self._on_perceive),
                ("transform_tool_result", self._on_action_result),
                ("pre_llm_call", self._inject_spatial_context),
            ]:
                lst = _pm._hooks.get(hook_name, [])
                if cb in lst:
                    lst.remove(cb)
        except Exception:
            pass
        self._initialized = False

    # ── Context injection ────────────────────────────────────────────

    def system_prompt_block(self) -> str:
        if not self._persona:
            return ""
        try:
            from agent.altercraft_memory import summarize_memory
            summary = summarize_memory(self._persona, max_chars=1200)
        except Exception as e:
            logger.debug("altercraft system_prompt_block failed: %s", e)
            return ""
        if not summary:
            return ""
        return (
            "## AlterCraft world memory\n\n"
            f"{summary}\n\n"
            "Use the altercraft_* tools to read or update this memory as the world changes."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        # The system prompt block already carries the summary; prefetch
        # only fires when memory has changed mid-session and we want the
        # latest in-context. For now it's a no-op — adding a per-turn
        # summary refetch would be cheap but noisy. Future: emit a delta
        # when the events file has new entries since last turn.
        return ""

    # ── Tool surface ─────────────────────────────────────────────────

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            _RECALL_SUMMARY_SCHEMA,
            _REMEMBER_LOCATION_SCHEMA,
            _RECALL_LOCATIONS_SCHEMA,
            _RECORD_EVENT_SCHEMA,
            _RECALL_EVENTS_SCHEMA,
            _GRAPH_QUERY_NEAR_SCHEMA,
            _CONSOLIDATE_BATCH_SCHEMA,
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._initialized or not self._persona:
            return json.dumps({"ok": False, "error": "altercraft memory not initialized"})

        try:
            from agent.altercraft_memory import (
                append_event,
                load_memory,
                save_memory,
                summarize_memory,
            )
        except Exception as e:
            return json.dumps({"ok": False, "error": f"altercraft_memory module unavailable: {e}"})

        try:
            if tool_name == "altercraft_recall_summary":
                max_chars = int(args.get("max_chars") or 1200)
                summary = summarize_memory(self._persona, max_chars=max_chars)
                return json.dumps({"ok": True, "summary": summary})

            if tool_name == "altercraft_remember_location":
                name = args.get("name")
                if not isinstance(name, str) or not name.strip():
                    return json.dumps({"ok": False, "error": "name required"})
                mem = load_memory(self._persona)
                locs = mem.get("locations") or {}
                locs[name] = {
                    "x": float(args["x"]),
                    "y": float(args["y"]),
                    "z": float(args["z"]),
                    "notes": str(args.get("notes") or ""),
                }
                save_memory(self._persona, "locations", locs)
                # MVP dual-write: keep the SQL store in sync so spatial
                # queries see the new location without re-running the
                # migrator. The flat JSON remains authoritative for the
                # legacy recall paths until V1 flips them.
                self._sql_upsert_place(name, locs[name])
                return json.dumps({"ok": True, "location": locs[name]})

            if tool_name == "altercraft_recall_locations":
                mem = load_memory(self._persona)
                return json.dumps({"ok": True, "locations": mem.get("locations") or {}})

            if tool_name == "altercraft_record_event":
                event = {
                    "type": str(args.get("type") or "event"),
                    "description": str(args.get("description") or ""),
                }
                details = args.get("details")
                if isinstance(details, dict):
                    event.update(details)
                append_event(self._persona, event)
                # Dual-write into episodes table for temporal queries.
                self._sql_record_episode(event)
                return json.dumps({"ok": True, "recorded": event["type"]})

            if tool_name == "altercraft_graph_query_near":
                from .world import connect, query_near
                try:
                    x = float(args["x"]); y = float(args["y"]); z = float(args["z"])
                except (KeyError, TypeError, ValueError):
                    return json.dumps({"ok": False, "error": "x, y, z required (numbers)"})
                radius = float(args.get("radius") or 50)
                types = args.get("types")
                if isinstance(types, str):
                    types = [types]
                limit = int(args.get("limit") or 20)
                conn = connect(self._persona)
                try:
                    nodes = query_near(conn, x, y, z, radius,
                                       types=types if types else None,
                                       limit=limit)
                finally:
                    conn.close()
                # Strip embedding BLOB from output (binary; large; not
                # useful in-prompt). Strip mood_history too — its raw
                # JSON is verbose; future tools surface it explicitly.
                cleaned = []
                for n in nodes:
                    n.pop("embedding", None)
                    n.pop("mood_history", None)
                    cleaned.append(n)
                return json.dumps({"ok": True, "nodes": cleaned})

            if tool_name == "altercraft_consolidate_batch":
                dry_run = bool(args.get("dry_run") or False)
                from .consolidator import run_consolidator
                result = run_consolidator(self._persona, dry_run=dry_run)
                consolidated = result.get("consolidated", 0)
                skipped = result.get("skipped", 0)
                suffix = " (dry run)" if result.get("dry_run") else ""
                return (
                    f"Consolidation complete{suffix}: "
                    f"{consolidated} episode(s) consolidated, {skipped} skipped."
                )

            if tool_name == "altercraft_recall_events":
                n = int(args.get("n") or 10)
                mem = load_memory(self._persona)
                events = (mem.get("events") or [])[-n:]
                return json.dumps({"ok": True, "events": events})

            return json.dumps({"ok": False, "error": f"unknown tool: {tool_name}"})

        except Exception as e:
            logger.exception("altercraft tool %s failed", tool_name)
            return json.dumps({"ok": False, "error": str(e)})

    # ── Lifecycle hooks ───────────────────────────────────────────────

    def _on_perceive(
        self,
        tool_name: str,
        args: dict,
        result: str,
        task_id: str,
        session_id: str,
        tool_call_id: str,
        duration_ms: int,
    ) -> Optional[str]:
        """transform_tool_result: persist mc_perceive data to scene graph."""
        if tool_name != "mc_perceive":
            return None
        if not self._persona:
            return None
        try:
            data = json.loads(result)
        except Exception:
            return None
        try:
            pos = (data.get("status") or {}).get("position") or {}
            bx = pos.get("x")
            by = pos.get("y")
            bz = pos.get("z")
            if bx is None or by is None or bz is None:
                return None
            bx, by, bz = float(bx), float(by), float(bz)
            self._last_position = (bx, by, bz)

            from .world import connect, get_or_create_persona, upsert_node
            conn = connect(self._persona)
            try:
                pid = get_or_create_persona(conn, self._persona)
                status = data.get("status") or {}
                upsert_node(
                    conn,
                    uri="bot_position",
                    type="bot",
                    name="Bot position",
                    pos=(bx, by, bz),
                    attrs={"health": status.get("health"), "food": status.get("food")},
                    observed_by=pid,
                    salience=0.8,
                )
                # entities can be at data.nearby.entities or data.status.nearbyEntities
                nearby = data.get("nearby") or {}
                nearby_entities = nearby.get("entities") or status.get("nearbyEntities") or []
                for ent in nearby_entities[:8]:
                    ename = str(ent.get("name") or "unknown")
                    etype = "mob" if ename.lower() not in ("player",) else "player"
                    dist = float(ent.get("distance") or 0)
                    upsert_node(
                        conn,
                        uri=f"entity_{ename}_{round(bx)}_{round(bz)}",
                        type=etype,
                        name=ename,
                        pos=(bx, by + dist * 0.1, bz + dist),
                        attrs={"distance": dist},
                        observed_by=pid,
                        salience=0.4,
                    )
                import time as _time
                conn.execute(
                    "INSERT INTO episodes(ts, kind, body, detail, persona_id, pos_x, pos_y, pos_z) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _time.time(),
                        "perceive",
                        f"mc_perceive at ({round(bx)},{round(by)},{round(bz)})",
                        result[:1000],
                        pid,
                        bx, by, bz,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("altercraft _on_perceive hook failed: %s", exc)
        return None

    # ─── Action-result auto-detection ────────────────────────────────────

    _CONSTRUCTION_LABELS = ("place", "build", "craft", "smelt")
    _ADVENTURE_LABELS = ("mine", "kill", "hunt", "explore", "collect")

    def _on_action_result(
        self,
        tool_name: str,
        args: dict,
        result: str,
        task_id: str,
        session_id: str,
        tool_call_id: str,
        duration_ms: int,
    ) -> Optional[str]:
        """transform_tool_result: persist construction/adventure episodes from mc_action_result."""
        if tool_name != "mc_action_result":
            return None
        if not self._persona:
            return None
        try:
            data = json.loads(result)
        except Exception:
            return None

        if not data.get("ok"):
            return None

        label = str(data.get("label") or "").lower()
        if not label:
            return None

        kind: Optional[str] = None
        if any(kw in label for kw in self._CONSTRUCTION_LABELS):
            kind = "construction"
        elif any(kw in label for kw in self._ADVENTURE_LABELS):
            kind = "adventure"

        if kind is None:
            return None

        try:
            from .world import connect, get_or_create_persona
            import time as _time

            conn = connect(self._persona)
            try:
                pid = get_or_create_persona(conn, self._persona)
                detail = {
                    "label": data.get("label"),
                    "goal": data.get("goal"),
                    "duration_ms": data.get("duration_ms"),
                }
                lp = self._last_position  # (x, y, z) or None
                conn.execute(
                    "INSERT INTO episodes(ts, kind, body, detail, persona_id, pos_x, pos_y, pos_z) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _time.time(),
                        kind,
                        str(data.get("label") or ""),
                        json.dumps(detail, sort_keys=True, default=str),
                        pid,
                        lp[0] if lp else None,
                        lp[1] if lp else None,
                        lp[2] if lp else None,
                    ),
                )
                conn.commit()
                logger.debug(
                    "altercraft _on_action_result: inserted %s episode label=%s",
                    kind, data.get("label"),
                )
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("altercraft _on_action_result hook failed: %s", exc)
        return None

    def _inject_spatial_context(
        self,
        session_id: str,
        model: str,
        platform: str,
        is_first_turn: bool = True,
        **kwargs,
    ) -> Optional[dict]:
        """pre_llm_call: return spatial + narrative context on the first turn.

        The framework appends the returned {"context": str} value to the user
        message. Never mutates the messages list directly.
        """
        if not is_first_turn:
            return None
        if not self._persona:
            return None

        spatial_block: Optional[str] = None
        narrative_block: Optional[str] = None

        # ── Spatial block ────────────────────────────────────────────
        if self._last_position is not None:
            try:
                x, y, z = self._last_position
                from .world import connect, query_near
                conn = connect(self._persona)
                try:
                    nodes = query_near(conn, x, y, z, radius=50, limit=12)
                finally:
                    conn.close()
                if nodes:
                    lines = [
                        f"## Nearby (last known position: x={round(x)} y={round(y)} z={round(z)})"
                    ]
                    for n in nodes:
                        nx = round(n.get("pos_x") or 0)
                        ny = round(n.get("pos_y") or 0)
                        nz = round(n.get("pos_z") or 0)
                        lines.append(
                            f'- {n.get("type","?")} "{n.get("name","?")}" at ({nx},{ny},{nz})'
                        )
                    spatial_block = "<spatial_context>\n" + "\n".join(lines) + "\n</spatial_context>\n"
            except Exception as exc:
                logger.warning("altercraft _inject_spatial_context (spatial) failed: %s", exc)

        # ── Narrative block ──────────────────────────────────────────
        try:
            from .library import open_library, get_recent_library_episodes
            lib_conn = open_library(self._persona)
            try:
                episodes = get_recent_library_episodes(lib_conn, self._persona, limit=5)
            finally:
                lib_conn.close()
            if episodes:
                lines = ["## Recent Episodes"]
                for ep in episodes:
                    kind = ep.get("kind", "?")
                    summary = ep.get("summary", "")
                    tags = ep.get("tags", "")
                    lines.append(f"- {kind}: {summary} [{tags}]")
                narrative_block = "<narrative_context>\n" + "\n".join(lines) + "\n</narrative_context>\n"
        except Exception as exc:
            logger.warning("altercraft _inject_spatial_context (narrative) failed: %s", exc)

        # ── Combine ──────────────────────────────────────────────────
        if spatial_block and narrative_block:
            combined = spatial_block + "\n" + narrative_block
        elif spatial_block:
            combined = spatial_block
        elif narrative_block:
            combined = narrative_block
        else:
            return None

        return {"context": combined}

    # ── SQL dual-write helpers (MVP) ─────────────────────────────────

    def _sql_upsert_place(self, name: str, info: Dict[str, Any]) -> None:
        """Mirror an `altercraft_remember_location` write into nodes."""
        if not self._persona:
            return
        try:
            from .world import connect, get_or_create_persona, upsert_node
            conn = connect(self._persona)
            try:
                pid = get_or_create_persona(conn, self._persona)
                upsert_node(
                    conn,
                    uri=f"place:{name}",
                    type="place",
                    name=name,
                    pos=(float(info["x"]), float(info["y"]), float(info["z"])),
                    attrs={
                        k: v for k, v in info.items()
                        if k not in ("x", "y", "z")
                    },
                    observed_by=pid,
                    salience=0.6,
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("sql dual-write upsert_place failed: %s", exc)

    def _sql_record_episode(self, event: Dict[str, Any]) -> None:
        """Mirror an `altercraft_record_event` write into episodes."""
        if not self._persona:
            return
        try:
            from .world import connect, get_or_create_persona
            conn = connect(self._persona)
            try:
                pid = get_or_create_persona(conn, self._persona)
                ts = event.get("ts")
                if not isinstance(ts, (int, float)):
                    import time as _time
                    ts = _time.time()
                kind = str(event.get("type") or event.get("kind") or "event")
                body = str(event.get("description") or event.get("body") or "")
                pos_x = event.get("x")
                pos_y = event.get("y")
                pos_z = event.get("z")
                detail = {
                    k: v for k, v in event.items()
                    if k not in ("ts", "type", "kind", "description", "body",
                                 "x", "y", "z")
                }
                conn.execute(
                    "INSERT INTO episodes(ts, kind, body, detail, persona_id, "
                    "pos_x, pos_y, pos_z) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        float(ts), kind, body,
                        json.dumps(detail, sort_keys=True, default=str),
                        pid, pos_x, pos_y, pos_z,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("sql dual-write record_episode failed: %s", exc)


# ─── Plugin registration ─────────────────────────────────────────────────


def register(ctx) -> None:
    """Plugin entry point — called by plugins/memory discovery."""
    provider = AltercraftMemoryProvider()
    provider._ctx = ctx
    ctx.register_memory_provider(provider)
