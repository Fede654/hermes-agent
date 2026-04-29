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


# ─── Provider ──────────────────────────────────────────────────────────────


class AltercraftMemoryProvider(MemoryProvider):
    """MemoryProvider wrapping agent/altercraft_memory.py JSON store."""

    @property
    def name(self) -> str:
        return "altercraft"

    def __init__(self) -> None:
        self._persona: Optional[str] = None
        self._initialized: bool = False

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

    def shutdown(self) -> None:
        # Nothing to flush — every write is atomic on its own.
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
                return json.dumps({"ok": True, "recorded": event["type"]})

            if tool_name == "altercraft_recall_events":
                n = int(args.get("n") or 10)
                mem = load_memory(self._persona)
                events = (mem.get("events") or [])[-n:]
                return json.dumps({"ok": True, "events": events})

            return json.dumps({"ok": False, "error": f"unknown tool: {tool_name}"})

        except Exception as e:
            logger.exception("altercraft tool %s failed", tool_name)
            return json.dumps({"ok": False, "error": str(e)})


# ─── Plugin registration ─────────────────────────────────────────────────


def register(ctx) -> None:
    """Plugin entry point — called by plugins/memory discovery."""
    ctx.register_memory_provider(AltercraftMemoryProvider())
