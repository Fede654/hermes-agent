#!/usr/bin/env python3
"""AlterCraft Tool Module — embodied Minecraft interaction for Hermes.

Wraps the HTTP API of `~/.hermes/altercraft/server.js` (Mineflayer HTTP bot
server) as LLM-callable tools. The server.js process is spawned out-of-
band by `hermes altercraft spawn`; this tool only talks to it as a client.

Environment:
    MC_API_URL   Base URL of the bot HTTP server (default: http://localhost:3001)
    MC_TOOL_TIMEOUT  Per-request timeout in seconds (default: 30)

All tools return a plain string formatted for LLM consumption — a short
heading + compact body. Errors are returned as strings beginning with
``Error:`` so the model can reason about them rather than the call
raising.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


def _api_base() -> str:
    return os.environ.get("MC_API_URL", "http://localhost:3001").rstrip("/")


def _timeout() -> float:
    try:
        return float(os.environ.get("MC_TOOL_TIMEOUT", "30"))
    except ValueError:
        return 30.0


def _check_server_available() -> bool:
    """Toolset availability check. True iff the bot server /health responds."""
    try:
        with httpx.Client(timeout=2.0) as client:
            r = client.get(f"{_api_base()}/health")
        return r.status_code == 200
    except Exception:
        return False


def _req(method: str, path: str, **kwargs: Any) -> tuple[bool, Any]:
    """Low-level request helper. Returns (ok, body_or_error_message)."""
    url = f"{_api_base()}{path}"
    try:
        with httpx.Client(timeout=_timeout()) as client:
            r = client.request(method, url, **kwargs)
    except httpx.TimeoutException:
        return False, f"Error: timeout after {_timeout()}s on {method} {path}"
    except httpx.HTTPError as e:
        return False, f"Error: {type(e).__name__} on {method} {path}: {e}"
    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        return False, f"Error: HTTP {r.status_code} on {method} {path}: {detail}"
    try:
        return True, r.json()
    except Exception:
        return True, r.text


def _fmt_json(obj: Any, max_chars: int = 4000) -> str:
    s = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    if len(s) > max_chars:
        s = s[:max_chars] + f"\n… [truncated, {len(s)} total chars]"
    return s


# ────────────────────────────────────────────────────────────────────────────
# Tool handlers
# ────────────────────────────────────────────────────────────────────────────

# Handler contract: registry.dispatch calls handler(args_dict, **kwargs).
# Each handler takes a single dict positional arg (the parsed tool-call
# arguments) plus absorbs any extra kwargs the registry may pass.

def _h_status(args, **_: Any) -> str:
    ok, body = _req("GET", "/status")
    if not ok:
        return body
    return "Status:\n" + _fmt_json(body)


def _h_look(args, **_: Any) -> str:
    # server.js takes `range`, not `radius`.
    rng = int((args or {}).get("radius", (args or {}).get("range", 16)))
    ok, body = _req("GET", "/scene", params={"range": rng})
    if not ok:
        ok2, body2 = _req("GET", "/nearby", params={"range": rng})
        if ok2:
            return "Nearby (fallback):\n" + _fmt_json(body2)
        return body
    payload = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(payload, dict) and payload.get("summary"):
        return f"Scene (r={rng}):\n{payload['summary']}"
    return "Scene:\n" + _fmt_json(payload)


def _h_inventory(args, **_: Any) -> str:
    ok, body = _req("GET", "/inventory")
    if not ok:
        return body
    return "Inventory:\n" + _fmt_json(body)


def _h_say(args, **_: Any) -> str:
    a = args or {}
    # Accept common aliases in case the model picks a different arg name.
    message = (a.get("message") or a.get("text") or a.get("content")
               or a.get("msg") or "").strip()
    if not message:
        return f"Error: empty message; got args={list(a.keys())}"
    ok, body = _req("POST", "/action/chat", json={"message": message})
    if not ok:
        return body
    return f'Said: "{message}"'


def _h_whisper(args, **_: Any) -> str:
    a = args or {}
    target = (a.get("target") or a.get("to") or a.get("recipient")
              or a.get("username") or a.get("player") or "").strip()
    message = (a.get("message") or a.get("text") or a.get("content")
               or a.get("msg") or "").strip()
    if not target or not message:
        return (f"Error: target and message are required; "
                f"got args={list(a.keys())}")
    ok, body = _req("POST", "/action/chat_to",
                    json={"player": target, "message": message})
    if not ok:
        return body
    return f'Whispered to {target}: "{message}"'


def _h_listen(args, **_: Any) -> str:
    a = args or {}
    qs: dict[str, Any] = {"limit": int(a.get("limit", 20))}
    if a.get("since_ms") is not None:
        qs["since"] = int(a["since_ms"])
    ok, body = _req("GET", "/chat", params=qs)
    if not ok:
        return body
    events = body
    if isinstance(body, dict):
        data = body.get("data") or {}
        events = data.get("messages") if isinstance(data, dict) else body
        if events is None:
            events = body.get("messages", body)
    return "Chat events:\n" + _fmt_json(events)


def _h_goto(args, **_: Any) -> str:
    a = args or {}
    near = bool(a.get("near", False))
    try:
        x, y, z = float(a["x"]), float(a["y"]), float(a["z"])
    except (KeyError, TypeError, ValueError) as e:
        return f"Error: goto needs x,y,z numbers ({e})"
    endpoint = "/action/goto_near" if near else "/task/goto"
    ok, body = _req("POST", endpoint, json={"x": x, "y": y, "z": z})
    if not ok:
        return body
    return f"Goto{' near' if near else ''} ({x}, {y}, {z}) started:\n" + _fmt_json(body)


def _h_stop(args, **_: Any) -> str:
    # Cancel any running task; also stop synchronous actions.
    _req("POST", "/task/cancel")
    ok, body = _req("POST", "/action/stop", json={})
    if not ok:
        return body
    return "Stopped current task."


# ────────────────────────────────────────────────────────────────────────────
# Schemas
# ────────────────────────────────────────────────────────────────────────────

ALTERCRAFT_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_status",
        "description": (
            "Return this agent's own Minecraft character state: position, "
            "health, hunger, dimension, current task. Use it to ground "
            "yourself before acting."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

ALTERCRAFT_LOOK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_look",
        "description": (
            "Return a natural-language scene summary from the agent's "
            "point of view (line-of-sight filtered; fair-play, no XRay). "
            "Lists visible entities, notable blocks, recent sounds. "
            "Use it before moving or chatting."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "radius": {
                    "type": "integer",
                    "description": "Perception radius in blocks (default 16).",
                    "default": 16,
                }
            },
            "required": [],
        },
    },
}

ALTERCRAFT_INVENTORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_inventory",
        "description": "List the items currently in this agent's inventory.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

ALTERCRAFT_SAY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_say",
        "description": (
            "Send a public chat message. Everyone on the server sees it. "
            "Keep messages short and speak as this character."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message text. No prefixes or self-references; just what you'd actually say.",
                }
            },
            "required": ["message"],
        },
    },
}

ALTERCRAFT_WHISPER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_whisper",
        "description": (
            "Send a private message to a specific player by username. "
            "Uses the name-routed DM convention."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Recipient username."},
                "message": {"type": "string", "description": "Message text."},
            },
            "required": ["target", "message"],
        },
    },
}

ALTERCRAFT_LISTEN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_listen",
        "description": (
            "Return recent chat events (public messages, whispers directed at "
            "this agent, join/leave). Use it to catch up on what happened "
            "between your turns."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "since_ms": {
                    "type": "integer",
                    "description": "Return events with timestamp >= this (ms since epoch). Omit to use the server default window.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max events returned (default 20).",
                    "default": 20,
                },
            },
            "required": [],
        },
    },
}

ALTERCRAFT_GOTO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_goto",
        "description": (
            "Start pathfinding to the given coordinates. Returns once the "
            "task is accepted; movement happens asynchronously. Check "
            "altercraft_status to see arrival. Use near=true to stop a few "
            "blocks before the exact spot (e.g., to avoid standing on someone)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
                "near": {"type": "boolean", "default": False},
            },
            "required": ["x", "y", "z"],
        },
    },
}

ALTERCRAFT_STOP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_stop",
        "description": "Cancel the current movement/task. Use if stuck or interrupted.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


# ────────────────────────────────────────────────────────────────────────────
# Registration
# ────────────────────────────────────────────────────────────────────────────

from tools.registry import registry

# NOTE: Each registration must be a top-level statement — the discovery
# AST check in tools/registry.py only recognizes `registry.register(...)`
# at module scope, not inside loops or conditionals.

registry.register(
    name="altercraft_status",
    toolset="altercraft",
    schema=ALTERCRAFT_STATUS_SCHEMA,
    handler=_h_status,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_STATUS_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_look",
    toolset="altercraft",
    schema=ALTERCRAFT_LOOK_SCHEMA,
    handler=_h_look,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_LOOK_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_inventory",
    toolset="altercraft",
    schema=ALTERCRAFT_INVENTORY_SCHEMA,
    handler=_h_inventory,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_INVENTORY_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_say",
    toolset="altercraft",
    schema=ALTERCRAFT_SAY_SCHEMA,
    handler=_h_say,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_SAY_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_whisper",
    toolset="altercraft",
    schema=ALTERCRAFT_WHISPER_SCHEMA,
    handler=_h_whisper,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_WHISPER_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_listen",
    toolset="altercraft",
    schema=ALTERCRAFT_LISTEN_SCHEMA,
    handler=_h_listen,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_LISTEN_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_goto",
    toolset="altercraft",
    schema=ALTERCRAFT_GOTO_SCHEMA,
    handler=_h_goto,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_GOTO_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_stop",
    toolset="altercraft",
    schema=ALTERCRAFT_STOP_SCHEMA,
    handler=_h_stop,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_STOP_SCHEMA["function"]["description"],
)


# ==================================================================
# MOVEMENT MODULE (merged from researcher deliverable)
# ==================================================================

def _h_follow(args, **_: Any) -> str:
    a = args or {}
    player = (a.get("player") or a.get("target") or "").strip()
    if not player:
        return "Error: follow requires a player name"
    ok, body = _req("POST", "/task/follow", json={"player": player})
    if not ok:
        return body
    return f"Following {player}:\n" + _fmt_json(body)


def _h_look_at(args, **_: Any) -> str:
    a = args or {}
    try:
        x, y, z = float(a["x"]), float(a["y"]), float(a["z"])
    except (KeyError, TypeError, ValueError) as e:
        return f"Error: look_at needs x,y,z numbers ({e})"
    ok, body = _req("POST", "/action/look", json={"x": x, "y": y, "z": z})
    if not ok:
        return body
    return f"Looking at ({x}, {y}, {z}):\n" + _fmt_json(body)


def _h_deathpoint(args, **_: Any) -> str:
    ok, body = _req("POST", "/task/deathpoint")
    if not ok:
        return body
    return "Deathpoint recovery:\n" + _fmt_json(body)


def _h_mark(args, **_: Any) -> str:
    a = args or {}
    name = (a.get("name") or "").strip()
    if not name:
        return "Error: mark requires a name"
    note = (a.get("note") or "").strip()
    payload: dict[str, Any] = {"name": name}
    if note:
        payload["note"] = note
    ok, body = _req("POST", "/action/mark", json=payload)
    if not ok:
        return body
    return f"Marked '{name}':\n" + _fmt_json(body)


def _h_marks(args, **_: Any) -> str:
    ok, body = _req("POST", "/action/marks")
    if not ok:
        return body
    return "Saved marks:\n" + _fmt_json(body)


def _h_go_mark(args, **_: Any) -> str:
    a = args or {}
    name = (a.get("name") or "").strip()
    if not name:
        return "Error: go_mark requires a mark name"
    ok, body = _req("POST", "/task/go_mark", json={"name": name})
    if not ok:
        return body
    return f"Going to mark '{name}':\n" + _fmt_json(body)


def _h_unmark(args, **_: Any) -> str:
    a = args or {}
    name = (a.get("name") or "").strip()
    if not name:
        return "Error: unmark requires a mark name"
    ok, body = _req("POST", "/action/unmark", json={"name": name})
    if not ok:
        return body
    return f"Removed mark '{name}':\n" + _fmt_json(body)


# ── Schemas ─────────────────────────────────────────────────────────────────

ALTERCRAFT_FOLLOW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_follow",
        "description": "Follow a player by username. Stays within 2 blocks. Use /action/stop to cancel.",
        "parameters": {
            "type": "object",
            "properties": {
                "player": {"type": "string", "description": "Username of the player to follow."},
            },
            "required": ["player"],
        },
    },
}

ALTERCRAFT_LOOK_AT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_look_at",
        "description": "Look at specific coordinates. Useful before placing blocks or attacking.",
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
            },
            "required": ["x", "y", "z"],
        },
    },
}

ALTERCRAFT_DEATHPOINT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_deathpoint",
        "description": "Pathfind to your last death location to recover dropped items.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

ALTERCRAFT_MARK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_mark",
        "description": "Save the current coordinates as a named waypoint.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique name for this mark (e.g. 'home', 'mine_entrance')."},
                "note": {"type": "string", "description": "Optional note about this location."},
            },
            "required": ["name"],
        },
    },
}

ALTERCRAFT_MARKS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_marks",
        "description": "List all saved waypoints with coordinates and notes.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

ALTERCRAFT_GO_MARK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_go_mark",
        "description": "Pathfind to a previously saved waypoint by name.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the saved waypoint."},
            },
            "required": ["name"],
        },
    },
}

ALTERCRAFT_UNMARK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_unmark",
        "description": "Delete a saved waypoint by name.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the waypoint to delete."},
            },
            "required": ["name"],
        },
    },
}


# ── Registration ────────────────────────────────────────────────────────────

from tools.registry import registry

registry.register(
    name="altercraft_follow",
    toolset="altercraft",
    schema=ALTERCRAFT_FOLLOW_SCHEMA,
    handler=_h_follow,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_FOLLOW_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_look_at",
    toolset="altercraft",
    schema=ALTERCRAFT_LOOK_AT_SCHEMA,
    handler=_h_look_at,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_LOOK_AT_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_deathpoint",
    toolset="altercraft",
    schema=ALTERCRAFT_DEATHPOINT_SCHEMA,
    handler=_h_deathpoint,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_DEATHPOINT_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_mark",
    toolset="altercraft",
    schema=ALTERCRAFT_MARK_SCHEMA,
    handler=_h_mark,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_MARK_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_marks",
    toolset="altercraft",
    schema=ALTERCRAFT_MARKS_SCHEMA,
    handler=_h_marks,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_MARKS_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_go_mark",
    toolset="altercraft",
    schema=ALTERCRAFT_GO_MARK_SCHEMA,
    handler=_h_go_mark,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_GO_MARK_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_unmark",
    toolset="altercraft",
    schema=ALTERCRAFT_UNMARK_SCHEMA,
    handler=_h_unmark,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_UNMARK_SCHEMA["function"]["description"],
)

# ==================================================================
# MINING MODULE (merged from researcher deliverable)
# ==================================================================

_LAVA_BLOCKS = {"lava", "flowing_lava"}
_FALLING_BLOCKS = {"gravel", "sand", "red_sand"}
_VALUABLE_ITEMS = {
    "diamond", "emerald", "ancient_debris", "netherite_ingot",
    "netherite_scrap", "enchanted_golden_apple", "totem_of_undying",
}


def _is_lava_near(blocks: list[dict], pos: dict, radius: int = 2) -> bool:
    for b in blocks:
        if b.get("name") not in _LAVA_BLOCKS:
            continue
        p = b.get("position") or {}
        dx = abs(p.get("x", 0) - pos.get("x", 0))
        dy = abs(p.get("y", 0) - pos.get("y", 0))
        dz = abs(p.get("z", 0) - pos.get("z", 0))
        if max(dx, dy, dz) <= radius:
            return True
    return False


def _smelt_map() -> dict[str, str]:
    return {
        "raw_iron": "iron_ingot",
        "raw_gold": "gold_ingot",
        "raw_copper": "copper_ingot",
        "ancient_debris": "netherite_scrap",
        "cobblestone": "stone",
        "sand": "glass",
        "clay_ball": "brick",
        "kelp": "dried_kelp",
        "oak_log": "charcoal",
        "birch_log": "charcoal",
        "spruce_log": "charcoal",
    }


# ────────────────────────────────────────────────────────────────────────────
# Handlers
# ────────────────────────────────────────────────────────────────────────────

def _h_collect(args, **_: Any) -> str:
    a = args or {}
    block = (a.get("block") or "").strip()
    if not block:
        return "Error: block name is required"
    count = max(1, min(int(a.get("count", 1)), 20))
    ok, body = _req("POST", "/action/collect", json={"block": block, "count": count})
    if not ok:
        return body
    result = body.get("result") or body.get("data", {}).get("result", "")
    return f"Collect ({block} x{count}):\n{result}"


def _h_dig(args, **_: Any) -> str:
    a = args or {}
    try:
        x, y, z = float(a["x"]), float(a["y"]), float(a["z"])
    except (KeyError, TypeError, ValueError) as e:
        return f"Error: dig needs x,y,z numbers ({e})"
    ok, body = _req("POST", "/action/dig", json={"x": x, "y": y, "z": z})
    if not ok:
        return body
    result = body.get("result") or body.get("data", {}).get("result", "")
    return f"Dig ({x}, {y}, {z}):\n{result}"


def _h_pickup(args, **_: Any) -> str:
    ok, body = _req("POST", "/action/pickup", json={})
    if not ok:
        return body
    result = body.get("result") or body.get("data", {}).get("result", "")
    return f"Pickup:\n{result}"


def _h_find_blocks(args, **_: Any) -> str:
    a = args or {}
    block = (a.get("block") or "").strip()
    if not block:
        return "Error: block name is required"
    radius = max(8, min(int(a.get("radius", 32)), 64))
    count = max(1, min(int(a.get("count", 10)), 50))
    ok, body = _req("POST", "/action/find_blocks", json={"block": block, "radius": radius, "count": count})
    if not ok:
        return body
    payload = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(payload, dict) and payload.get("locations"):
        locs = payload["locations"]
        lines = [f"Found {len(locs)} {block} within {radius} blocks:"]
        for loc in locs[:20]:
            lines.append(
                f"  ({loc.get('x')}, {loc.get('y')}, {loc.get('z')})  "
                f"dist={loc.get('distance')}m  bearing={loc.get('bearing')}  sector={loc.get('sector')}"
            )
        if len(locs) > 20:
            lines.append(f"  … and {len(locs) - 20} more")
        return "\n".join(lines)
    return "Find blocks:\n" + _fmt_json(payload)


def _h_smelt_raw(args, **_: Any) -> str:
    a = args or {}
    item = (a.get("item") or "").strip()
    if not item:
        return "Error: item is required (e.g. raw_iron, raw_gold, sand)"
    mapping = _smelt_map()
    if item not in mapping:
        return (
            f"Error: '{item}' is not in the smelt map. "
            f"Supported: {', '.join(sorted(mapping.keys()))}"
        )
    count = max(1, int(a.get("count", 1)))
    fuel = (a.get("fuel") or "").strip() or None
    ok, body = _req("POST", "/action/smelt", json={"input": item, "fuel": fuel, "count": count})
    if not ok:
        return body
    result = body.get("result") or body.get("data", {}).get("result", "")
    return f"Smelt ({item} -> {mapping[item]} x{count}):\n{result}"


def _h_sort_inventory(args, **_: Any) -> str:
    ok, body = _req("GET", "/inventory")
    if not ok:
        return body
    payload = body.get("data", body) if isinstance(body, dict) else body
    items = payload if isinstance(payload, list) else payload.get("items", [])
    if not items:
        return "Inventory is empty."

    categories: dict[str, list[str]] = {
        "Tools": [],
        "Ores / Raw": [],
        "Building": [],
        "Food": [],
        "Combat": [],
        "Misc": [],
    }

    for it in items:
        name = it.get("name", "unknown")
        count = it.get("count", 1)
        label = f"{name} x{count}"
        if any(name.endswith(s) for s in ("_pickaxe", "_axe", "_shovel", "_hoe", "_sword")) or name in ("shield", "bow", "crossbow"):
            categories["Tools"].append(label)
        elif any(name.endswith(s) for s in ("_ore", "deepslate_", "raw_", "ancient_debris")) or name in ("coal", "redstone", "lapis_lazuli", "diamond", "emerald"):
            categories["Ores / Raw"].append(label)
        elif any(name.endswith(s) for s in ("_log", "_planks", "_wood", "cobblestone", "stone", "dirt", "grass_block", "sand", "gravel")):
            categories["Building"].append(label)
        elif any(name.endswith(s) for s in ("_beef", "_porkchop", "_chicken", "_mutton", "_rabbit", "_cod", "_salmon", "bread", "apple", "potato", "carrot")) or name in ("cooked_beef", "cooked_porkchop", "golden_apple"):
            categories["Food"].append(label)
        elif name.endswith("_helmet") or name.endswith("_chestplate") or name.endswith("_leggings") or name.endswith("_boots") or name in ("arrow", "totem_of_undying", "enchanted_golden_apple"):
            categories["Combat"].append(label)
        else:
            categories["Misc"].append(label)

    lines = ["Inventory summary:"]
    for cat, vals in categories.items():
        if vals:
            lines.append(f"  {cat}: {', '.join(vals)}")
    return "\n".join(lines)


def _h_dump_excess(args, **_: Any) -> str:
    a = args or {}
    keep = set((a.get("keep") or []) if isinstance(a.get("keep"), list) else [a.get("keep")] if a.get("keep") else [])
    keep_tools = bool(a.get("keep_tools", True))
    keep_food = bool(a.get("keep_food", True))
    drop_list = [s.strip() for s in (a.get("drop_list") or []) if isinstance(a.get("drop_list"), list)]
    max_keep = max(0, int(a.get("max_keep", 64)))

    ok, body = _req("GET", "/inventory")
    if not ok:
        return body
    payload = body.get("data", body) if isinstance(body, dict) else body
    items = payload if isinstance(payload, list) else payload.get("items", [])
    if not items:
        return "Inventory is empty — nothing to drop."

    candidates = []
    for it in items:
        name = it.get("name", "")
        count = it.get("count", 1)

        if name in keep:
            continue
        if keep_tools and (name.endswith("_pickaxe") or name.endswith("_axe") or name.endswith("_shovel") or name.endswith("_hoe") or name.endswith("_sword") or name in ("shield", "bow", "crossbow")):
            continue
        if keep_food and (name.endswith("_beef") or name.endswith("_porkchop") or name.endswith("_chicken") or name.endswith("_mutton") or name.endswith("_rabbit") or name.endswith("_cod") or name.endswith("_salmon") or name.endswith("_bread") or name in ("apple", "potato", "carrot", "cooked_beef", "cooked_porkchop", "golden_apple", "enchanted_golden_apple")):
            continue
        if name in _VALUABLE_ITEMS and name not in drop_list:
            continue

        if drop_list and name not in drop_list:
            continue

        if count <= max_keep and not drop_list:
            continue

        drop_count = count if drop_list else max(0, count - max_keep)
        if drop_count > 0:
            candidates.append({"name": name, "count": drop_count})

    if not candidates:
        return "No excess items to drop based on current rules."

    # Attempt to drop via server endpoint if available.
    dropped = []
    failed = []
    for cand in candidates:
        ok2, _ = _req("POST", "/action/toss", json={"item": cand["name"], "count": cand["count"]})
        if ok2:
            dropped.append(f"{cand['name']} x{cand['count']}")
        else:
            failed.append(f"{cand['name']} x{cand['count']}")

    if dropped and not failed:
        return f"Dropped: {', '.join(dropped)}"
    if dropped and failed:
        return (
            f"Dropped: {', '.join(dropped)}\n"
            f"Failed (server may lack /action/toss): {', '.join(failed)}"
        )
    return (
        f"Failed to drop items. Server endpoint /action/toss may be missing.\n"
        f"Candidates: {', '.join(f['name'] + ' x' + str(f['count']) for f in failed)}"
    )


# ────────────────────────────────────────────────────────────────────────────
# Schemas
# ────────────────────────────────────────────────────────────────────────────

ALTERCRAFT_COLLECT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_collect",
        "description": (
            "Find and mine up to N blocks of a specific type. Automatically equips the best tool, "
            "walks to each block, mines it, and attempts to pick up drops. Capped at 20 per call. "
            "Use altercraft_find_blocks first if you want to see locations before mining. "
            "Never digs straight down (safety enforced server-side)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "block": {
                    "type": "string",
                    "description": "Minecraft block name, e.g. oak_log, iron_ore, cobblestone.",
                },
                "count": {
                    "type": "integer",
                    "description": "How many to collect (1-20). Default 1.",
                    "default": 1,
                },
            },
            "required": ["block"],
        },
    },
}

ALTERCRAFT_DIG_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_dig",
        "description": (
            "Mine the block at exact coordinates (x, y, z). Equips the correct tool and walks "
            "within range if needed. Safer than collect when you already know the block location. "
            "Does not pick up drops automatically — call altercraft_pickup after."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
            },
            "required": ["x", "y", "z"],
        },
    },
}

ALTERCRAFT_PICKUP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_pickup",
        "description": (
            "Collect nearby dropped items on the ground. The bot will pathfind to each drop "
            "within ~16 blocks and walk over it. Call after mining or fighting."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

ALTERCRAFT_FIND_BLOCKS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_find_blocks",
        "description": (
            "Locate blocks of a specific type within a radius. In fair-play mode, only visible "
            "blocks are returned (no XRay). Use this before altercraft_collect to plan a route. "
            "Results include coordinates, distance, bearing, and sector (left/center/right)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "block": {
                    "type": "string",
                    "description": "Block name to search for, e.g. diamond_ore.",
                },
                "radius": {
                    "type": "integer",
                    "description": "Search radius in blocks (8-64). Default 32. Fair-play caps at 24.",
                    "default": 32,
                },
                "count": {
                    "type": "integer",
                    "description": "Max results (1-50). Default 10.",
                    "default": 10,
                },
            },
            "required": ["block"],
        },
    },
}

ALTERCRAFT_SMELT_RAW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_smelt_raw",
        "description": (
            "Smelt raw ores or materials in a nearby furnace. Automatically finds fuel if not "
            "specified. Supported inputs: raw_iron, raw_gold, raw_copper, ancient_debris, "
            "cobblestone, sand, clay_ball, kelp, oak_log, birch_log, spruce_log. "
            "Requires a furnace within 4 blocks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "item": {
                    "type": "string",
                    "description": "Input item name, e.g. raw_iron.",
                },
                "count": {
                    "type": "integer",
                    "description": "How many to smelt. Default 1.",
                    "default": 1,
                },
                "fuel": {
                    "type": "string",
                    "description": "Optional fuel name (coal, charcoal, oak_planks, etc.). Auto-detected if omitted.",
                },
            },
            "required": ["item"],
        },
    },
}

ALTERCRAFT_SORT_INVENTORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_sort_inventory",
        "description": (
            "Return a categorized summary of the inventory (tools, ores, building blocks, food, "
            "combat, misc). This is a report-only sort; actual slot reordering requires a future "
            "server update. Use it after mining to audit what you collected."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

ALTERCRAFT_DUMP_EXCESS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_dump_excess",
        "description": (
            "Drop unwanted items to free inventory space. By default preserves tools, food, and "
            "valuable items (diamonds, ancient_debris, etc.). Use drop_list to force-drop specific "
            "items. Requires server endpoint /action/toss (add to server.js if missing)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keep": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Item names to always keep, e.g. ['iron_ingot', 'coal'].",
                },
                "keep_tools": {
                    "type": "boolean",
                    "description": "Never drop pickaxes, axes, swords, etc. Default true.",
                    "default": True,
                },
                "keep_food": {
                    "type": "boolean",
                    "description": "Never drop food items. Default true.",
                    "default": True,
                },
                "drop_list": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "If provided, drop ONLY these items regardless of other rules.",
                },
                "max_keep": {
                    "type": "integer",
                    "description": "If not using drop_list, only drop stacks larger than this. Default 64.",
                    "default": 64,
                },
            },
            "required": [],
        },
    },
}


# ────────────────────────────────────────────────────────────────────────────
# Registration
# ────────────────────────────────────────────────────────────────────────────

from tools.registry import registry  # noqa: E402

registry.register(
    name="altercraft_collect",
    toolset="altercraft",
    schema=ALTERCRAFT_COLLECT_SCHEMA,
    handler=_h_collect,
    check_fn=_check_server_available,
    emoji="⛏️",
    description=ALTERCRAFT_COLLECT_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_dig",
    toolset="altercraft",
    schema=ALTERCRAFT_DIG_SCHEMA,
    handler=_h_dig,
    check_fn=_check_server_available,
    emoji="⛏️",
    description=ALTERCRAFT_DIG_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_pickup",
    toolset="altercraft",
    schema=ALTERCRAFT_PICKUP_SCHEMA,
    handler=_h_pickup,
    check_fn=_check_server_available,
    emoji="🎒",
    description=ALTERCRAFT_PICKUP_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_find_blocks",
    toolset="altercraft",
    schema=ALTERCRAFT_FIND_BLOCKS_SCHEMA,
    handler=_h_find_blocks,
    check_fn=_check_server_available,
    emoji="🔍",
    description=ALTERCRAFT_FIND_BLOCKS_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_smelt_raw",
    toolset="altercraft",
    schema=ALTERCRAFT_SMELT_RAW_SCHEMA,
    handler=_h_smelt_raw,
    check_fn=_check_server_available,
    emoji="🔥",
    description=ALTERCRAFT_SMELT_RAW_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_sort_inventory",
    toolset="altercraft",
    schema=ALTERCRAFT_SORT_INVENTORY_SCHEMA,
    handler=_h_sort_inventory,
    check_fn=_check_server_available,
    emoji="📦",
    description=ALTERCRAFT_SORT_INVENTORY_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_dump_excess",
    toolset="altercraft",
    schema=ALTERCRAFT_DUMP_EXCESS_SCHEMA,
    handler=_h_dump_excess,
    check_fn=_check_server_available,
    emoji="🗑️",
    description=ALTERCRAFT_DUMP_EXCESS_SCHEMA["function"]["description"],
)

# ==================================================================
# CONTAINERS MODULE (merged from researcher deliverable)
# ==================================================================

def _parse_xyz(args: dict) -> tuple[float, float, float] | str:
    """Validate and return x,y,z or an error string."""
    try:
        return float(args["x"]), float(args["y"]), float(args["z"])
    except (KeyError, TypeError, ValueError) as e:
        return f"Error: container operations need x,y,z numbers ({e})"


def _h_list_container(args: Optional[dict], **_: Any) -> str:
    a = args or {}
    xyz = _parse_xyz(a)
    if isinstance(xyz, str):
        return xyz
    x, y, z = xyz
    ok, body = _req("POST", "/action/list_container", json={"x": x, "y": y, "z": z})
    if not ok:
        return body
    payload = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(payload, dict):
        ctype = payload.get("type", "container")
        slots = payload.get("slots", [])
        cap = payload.get("capacity", "?")
        lines = [f"{ctype} at ({x},{y},{z}) — {len(slots)}/{cap} slots:"]
        for s in slots:
            lines.append(f"  slot {s.get('slot','?')}: {s.get('name','?')} x{s.get('count',0)}")
        if not slots:
            lines.append("  (empty)")
        return "\n".join(lines)
    return "Container:\n" + _fmt_json(payload)


def _h_deposit(args: Optional[dict], **_: Any) -> str:
    a = args or {}
    xyz = _parse_xyz(a)
    if isinstance(xyz, str):
        return xyz
    x, y, z = xyz
    item = (a.get("item") or a.get("name") or "").strip()
    if not item:
        return "Error: deposit needs 'item' name"
    count = a.get("count")
    payload: dict[str, Any] = {"x": x, "y": y, "z": z, "item": item}
    if count is not None:
        payload["count"] = int(count)
    ok, body = _req("POST", "/action/deposit", json=payload)
    if not ok:
        return body
    data = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(data, dict):
        moved = data.get("moved", "?")
        req = data.get("requested", "?")
        rem = data.get("remaining_in_inventory", "?")
        return f"Deposited {moved}/{req} {item} into ({x},{y},{z}). Remaining in inventory: {rem}."
    return "Deposit result:\n" + _fmt_json(data)


def _h_withdraw(args: Optional[dict], **_: Any) -> str:
    a = args or {}
    xyz = _parse_xyz(a)
    if isinstance(xyz, str):
        return xyz
    x, y, z = xyz
    item = (a.get("item") or a.get("name") or "").strip()
    if not item:
        return "Error: withdraw needs 'item' name"
    count = a.get("count")
    payload: dict[str, Any] = {"x": x, "y": y, "z": z, "item": item}
    if count is not None:
        payload["count"] = int(count)
    ok, body = _req("POST", "/action/withdraw", json=payload)
    if not ok:
        return body
    data = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(data, dict):
        moved = data.get("moved", "?")
        req = data.get("requested", "?")
        rem = data.get("remaining_in_chest", "?")
        return f"Withdrew {moved}/{req} {item} from ({x},{y},{z}). Remaining in chest: {rem}."
    return "Withdraw result:\n" + _fmt_json(data)


def _h_furnace_check(args: Optional[dict], **_: Any) -> str:
    a = args or {}
    xyz = _parse_xyz(a)
    if isinstance(xyz, str):
        return xyz
    x, y, z = xyz
    ok, body = _req("POST", "/action/furnace_check", json={"x": x, "y": y, "z": z})
    if not ok:
        return body
    data = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(data, dict):
        inp = data.get("input") or {}
        fuel = data.get("fuel") or {}
        out = data.get("output") or {}
        prog = data.get("progress", "?")
        flvl = data.get("fuel_level", "?")
        lines = [
            f"Furnace at ({x},{y},{z}):",
            f"  input:  {inp.get('name','—')} x{inp.get('count',0)}",
            f"  fuel:   {fuel.get('name','—')} x{fuel.get('count',0)}",
            f"  output: {out.get('name','—')} x{out.get('count',0)}",
            f"  progress: {prog} | fuel_level: {flvl}",
        ]
        return "\n".join(lines)
    return "Furnace:\n" + _fmt_json(data)


def _h_furnace_take(args: Optional[dict], **_: Any) -> str:
    a = args or {}
    xyz = _parse_xyz(a)
    if isinstance(xyz, str):
        return xyz
    x, y, z = xyz
    count = a.get("count")
    payload: dict[str, Any] = {"x": x, "y": y, "z": z}
    if count is not None:
        payload["count"] = int(count)
    ok, body = _req("POST", "/action/furnace_take", json=payload)
    if not ok:
        return body
    data = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(data, dict):
        moved = data.get("moved", "?")
        item = data.get("item", "?")
        return f"Took {moved} {item} from furnace at ({x},{y},{z})."
    return "Furnace take:\n" + _fmt_json(data)


# ────────────────────────────────────────────────────────────────────────────
# Schemas
# ────────────────────────────────────────────────────────────────────────────

ALTERCRAFT_LIST_CONTAINER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_list_container",
        "description": (
            "Open a chest, furnace, dispenser, or other container at the given "
            "coordinates and list its contents. The agent must be within ~4 blocks. "
            "Use this before depositing or withdrawing to verify what is inside."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "Container X coordinate."},
                "y": {"type": "number", "description": "Container Y coordinate."},
                "z": {"type": "number", "description": "Container Z coordinate."},
            },
            "required": ["x", "y", "z"],
        },
    },
}

ALTERCRAFT_DEPOSIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_deposit",
        "description": (
            "Move items from the bot's inventory into the container at (x,y,z). "
            "Fuzzy item name matching is supported. Omit count to move all available. "
            "Returns how many items were actually moved."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
                "item": {
                    "type": "string",
                    "description": "Item name, e.g. 'oak_log'. Fuzzy match supported.",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of items to deposit. Omit for all.",
                },
            },
            "required": ["x", "y", "z", "item"],
        },
    },
}

ALTERCRAFT_WITHDRAW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_withdraw",
        "description": (
            "Move items from the container at (x,y,z) into the bot's inventory. "
            "Fuzzy item name matching is supported. Omit count to withdraw all available. "
            "Will fail gracefully if the bot's inventory is full."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
                "item": {
                    "type": "string",
                    "description": "Item name, e.g. 'iron_ingot'. Fuzzy match supported.",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of items to withdraw. Omit for all.",
                },
            },
            "required": ["x", "y", "z", "item"],
        },
    },
}

ALTERCRAFT_FURNACE_CHECK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_furnace_check",
        "description": (
            "Open a furnace at (x,y,z) and report input, fuel, and output slots, "
            "plus smelting progress (0-1) and fuel level (0-1). Use before adding "
            "fuel or taking output."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
            },
            "required": ["x", "y", "z"],
        },
    },
}

ALTERCRAFT_FURNACE_TAKE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_furnace_take",
        "description": (
            "Take the output item from a furnace at (x,y,z). Optionally limit the count. "
            "Use altercraft_furnace_check first to see what is ready."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
                "count": {
                    "type": "integer",
                    "description": "Max items to take. Omit for all.",
                },
            },
            "required": ["x", "y", "z"],
        },
    },
}


# ────────────────────────────────────────────────────────────────────────────
# Registration
# ────────────────────────────────────────────────────────────────────────────

from tools.registry import registry

registry.register(
    name="altercraft_list_container",
    toolset="altercraft",
    schema=ALTERCRAFT_LIST_CONTAINER_SCHEMA,
    handler=_h_list_container,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_LIST_CONTAINER_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_deposit",
    toolset="altercraft",
    schema=ALTERCRAFT_DEPOSIT_SCHEMA,
    handler=_h_deposit,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_DEPOSIT_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_withdraw",
    toolset="altercraft",
    schema=ALTERCRAFT_WITHDRAW_SCHEMA,
    handler=_h_withdraw,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_WITHDRAW_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_furnace_check",
    toolset="altercraft",
    schema=ALTERCRAFT_FURNACE_CHECK_SCHEMA,
    handler=_h_furnace_check,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_FURNACE_CHECK_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_furnace_take",
    toolset="altercraft",
    schema=ALTERCRAFT_FURNACE_TAKE_SCHEMA,
    handler=_h_furnace_take,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_FURNACE_TAKE_SCHEMA["function"]["description"],
)

# ==================================================================
# PERCEPTION MODULE (merged from researcher deliverable)
# ==================================================================

def _h_map(args, **_: Any) -> str:
    a = args or {}
    radius = int(a.get("radius", 16))
    ok, body = _req("GET", "/map", params={"radius": radius})
    if not ok:
        return body
    return f"Map (r={radius}):\n" + _fmt_json(body)


def _h_scene(args, **_: Any) -> str:
    a = args or {}
    rng = int(a.get("radius", a.get("range", 16)))
    ok, body = _req("GET", "/scene", params={"range": rng})
    if not ok:
        return body
    payload = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(payload, dict) and payload.get("summary"):
        return f"Scene (r={rng}):\n{payload['summary']}"
    return "Scene:\n" + _fmt_json(payload)


def _h_social(args, **_: Any) -> str:
    ok, body = _req("GET", "/social")
    if not ok:
        return body
    return "Social graph:\n" + _fmt_json(body)


def _h_overhear(args, **_: Any) -> str:
    a = args or {}
    limit = int(a.get("limit", 20))
    ok, body = _req("GET", "/overhear", params={"count": limit})
    if not ok:
        return body
    return "Overheard:\n" + _fmt_json(body)


def _h_sounds(args, **_: Any) -> str:
    ok, body = _req("GET", "/sounds")
    if not ok:
        return body
    return "Sounds:\n" + _fmt_json(body)


def _h_commands(args, **_: Any) -> str:
    ok, body = _req("GET", "/commands")
    if not ok:
        return body
    return "Pending commands:\n" + _fmt_json(body)


# ── Schemas ─────────────────────────────────────────────────────────────────

ALTERCRAFT_MAP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_map",
        "description": (
            "Retrieve a 2-D ASCII grid map of the surroundings. "
            "Best for navigation, path planning, and locating landmarks, water, or terrain precisely. "
            "Token cost grows as O(r^2); default radius 16, max 24."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "radius": {
                    "type": "integer",
                    "description": "Perception radius in blocks. Default 16, max 24.",
                    "default": 16,
                },
            },
            "required": [],
        },
    },
}

ALTERCRAFT_SCENE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_scene",
        "description": (
            "Retrieve a narrative scene description of the immediate surroundings. "
            "Best for tactical awareness, combat assessment, identifying threats, and block identification. "
            "Line-of-sight filtered; no XRay. Defaults to radius 16."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "radius": {
                    "type": "integer",
                    "description": "Perception radius in blocks. Default 16, max 24.",
                    "default": 16,
                },
            },
            "required": [],
        },
    },
}

ALTERCRAFT_SOCIAL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_social",
        "description": (
            "Retrieve the social graph of nearby players. Returns counts of public/private messages, "
            "commands given/completed, last channel, last message, and last seen timestamps. "
            "Use before responding to unfamiliar players to understand relationship history."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

ALTERCRAFT_OVERHEAR_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_overhear",
        "description": (
            "Retrieve recently overheard messages (public chat and whispers within earshot). "
            "Server keeps a FIFO buffer of the last ~100 messages. "
            "Use to catch context you were not directly addressed in."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of overheard messages to return.",
                    "default": 20,
                },
            },
            "required": [],
        },
    },
}

ALTERCRAFT_SOUNDS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_sounds",
        "description": (
            "Retrieve recent entity-caused sound events (mining, sprinting, walking). "
            "Sounds expire after ~30 seconds. Use when you have no current task and want to be proactive."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

ALTERCRAFT_COMMANDS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_commands",
        "description": (
            "Retrieve the pending command queue from other players. "
            "Commands persist until acknowledged. Poll regularly to auto-trigger responses to direct orders."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


# ── Registration ────────────────────────────────────────────────────────────

from tools.registry import registry

registry.register(
    name="altercraft_map",
    toolset="altercraft",
    schema=ALTERCRAFT_MAP_SCHEMA,
    handler=_h_map,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_MAP_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_scene",
    toolset="altercraft",
    schema=ALTERCRAFT_SCENE_SCHEMA,
    handler=_h_scene,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_SCENE_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_social",
    toolset="altercraft",
    schema=ALTERCRAFT_SOCIAL_SCHEMA,
    handler=_h_social,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_SOCIAL_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_overhear",
    toolset="altercraft",
    schema=ALTERCRAFT_OVERHEAR_SCHEMA,
    handler=_h_overhear,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_OVERHEAR_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_sounds",
    toolset="altercraft",
    schema=ALTERCRAFT_SOUNDS_SCHEMA,
    handler=_h_sounds,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_SOUNDS_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_commands",
    toolset="altercraft",
    schema=ALTERCRAFT_COMMANDS_SCHEMA,
    handler=_h_commands,
    check_fn=_check_server_available,
    emoji="🟩",
    description=ALTERCRAFT_COMMANDS_SCHEMA["function"]["description"],
)

# ==================================================================
# BUILDING MODULE (merged from researcher deliverable)
# ==================================================================

def _h_place(args, **_: Any) -> str:
    a = args or {}
    block = (a.get("block") or "").strip()
    try:
        x, y, z = float(a["x"]), float(a["y"]), float(a["z"])
    except (KeyError, TypeError, ValueError) as e:
        return f"Error: place needs block name and x,y,z numbers ({e})"
    if not block:
        return "Error: block name is required"
    ok, body = _req("POST", "/action/place", json={"block": block, "x": x, "y": y, "z": z})
    if not ok:
        return body
    return f"Placed {block} at ({x}, {y}, {z}):\n" + _fmt_json(body)


def _h_place_fill(args, **_: Any) -> str:
    a = args or {}
    block = (a.get("block") or "").strip()
    if not block:
        return "Error: block name is required"
    try:
        x1, y1, z1 = float(a["x1"]), float(a["y1"]), float(a["z1"])
        x2, y2, z2 = float(a["x2"]), float(a["y2"]), float(a["z2"])
    except (KeyError, TypeError, ValueError) as e:
        return f"Error: place_fill needs block and x1,y1,z1,x2,y2,z2 numbers ({e})"
    hollow = bool(a.get("hollow", False))
    total = abs(int(x2 - x1) + 1) * abs(int(y2 - y1) + 1) * abs(int(z2 - z1) + 1)
    if total > 500:
        return f"Error: volume {total} exceeds server limit of 500 blocks. Split into smaller fills."
    ok, body = _req(
        "POST", "/action/place_fill",
        build=True,
        json={"block": block, "x1": x1, "y1": y1, "z1": z1,
              "x2": x2, "y2": y2, "z2": z2, "hollow": hollow},
    )
    if not ok:
        return body
    return f"Fill {block} ({x1},{y1},{z1}) to ({x2},{y2},{z2}) hollow={hollow}:\n" + _fmt_json(body)


def _h_interact(args, **_: Any) -> str:
    a = args or {}
    try:
        x, y, z = float(a["x"]), float(a["y"]), float(a["z"])
    except (KeyError, TypeError, ValueError) as e:
        return f"Error: interact needs x,y,z numbers ({e})"
    ok, body = _req("POST", "/action/interact", json={"x": x, "y": y, "z": z})
    if not ok:
        return body
    return f"Interacted with block at ({x}, {y}, {z}):\n" + _fmt_json(body)


def _h_close_screen(args, **_: Any) -> str:
    ok, body = _req("POST", "/action/close_screen", json={})
    if not ok:
        return body
    return "Closed screen.\n" + _fmt_json(body)


# ──────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────

ALTERCRAFT_PLACE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_place",
        "description": (
            "Place a single block at exact coordinates. "
            "The bot must have the block in inventory. "
            "The server finds a solid adjacent face to place against; "
            "if none exists the call fails. "
            "Call altercraft_stop first if pathfinding is active, "
            "otherwise the bot may walk away mid-placement."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "block": {"type": "string", "description": "Block name, e.g. 'oak_planks' or 'dirt'."},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
            },
            "required": ["block", "x", "y", "z"],
        },
    },
}

ALTERCRAFT_PLACE_FILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_place_fill",
        "description": (
            "Fill a rectangular volume with a block. "
            "Max 500 blocks (server hard limit). "
            "Set hollow=true to place only the outer shell. "
            "The bot must have enough blocks in inventory. "
            "Call altercraft_stop before filling to avoid pathfinder interference."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "block": {"type": "string", "description": "Block name, e.g. 'cobblestone'."},
                "x1": {"type": "number"},
                "y1": {"type": "number"},
                "z1": {"type": "number"},
                "x2": {"type": "number"},
                "y2": {"type": "number"},
                "z2": {"type": "number"},
                "hollow": {"type": "boolean", "default": False, "description": "If true, only place the outer shell."},
            },
            "required": ["block", "x1", "y1", "z1", "x2", "y2", "z2"],
        },
    },
}

ALTERCRAFT_INTERACT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_interact",
        "description": (
            "Right-click a block at the given coordinates. "
            "Used to open doors, press buttons, flip levers, open trapdoors, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
            },
            "required": ["x", "y", "z"],
        },
    },
}

ALTERCRAFT_CLOSE_SCREEN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_close_screen",
        "description": (
            "Close any open GUI (chest, crafting table, villager trade, etc.). "
            "Use after altercraft_interact if a screen opens and blocks further actions."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


# ──────────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────────

from tools.registry import registry

registry.register(
    name="altercraft_place",
    toolset="altercraft",
    schema=ALTERCRAFT_PLACE_SCHEMA,
    handler=_h_place,
    check_fn=_check_server_available,
    emoji="🏗️",
    description=ALTERCRAFT_PLACE_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_place_fill",
    toolset="altercraft",
    schema=ALTERCRAFT_PLACE_FILL_SCHEMA,
    handler=_h_place_fill,
    check_fn=_check_server_available,
    emoji="🏗️",
    description=ALTERCRAFT_PLACE_FILL_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_interact",
    toolset="altercraft",
    schema=ALTERCRAFT_INTERACT_SCHEMA,
    handler=_h_interact,
    check_fn=_check_server_available,
    emoji="🧱",
    description=ALTERCRAFT_INTERACT_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_close_screen",
    toolset="altercraft",
    schema=ALTERCRAFT_CLOSE_SCREEN_SCHEMA,
    handler=_h_close_screen,
    check_fn=_check_server_available,
    emoji="❌",
    description=ALTERCRAFT_CLOSE_SCREEN_SCHEMA["function"]["description"],
)

# ==================================================================
# CRAFTING MODULE (merged from researcher deliverable)
# ==================================================================

from dataclasses import dataclass, field


def _position_from_status(status: dict) -> Optional[tuple[float, float, float]]:
    pos = status.get("position") or status.get("pos")
    if isinstance(pos, dict):
        try:
            return float(pos["x"]), float(pos["y"]), float(pos["z"])
        except Exception:
            return None
    if isinstance(pos, (list, tuple)) and len(pos) == 3:
        return tuple(float(x) for x in pos)
    return None


def _distance3(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _assert_near_block(
    block_pos: tuple[float, float, float],
    label: str = "block",
    max_dist: float = 4.0,
) -> Optional[str]:
    """Pre-flight proximity check. Returns error string if too far."""
    ok, body = _req("GET", "/status")
    if not ok:
        return body
    pos = _position_from_status(body.get("data", body) if isinstance(body, dict) else body)
    if pos is None:
        return None
    if _distance3(pos, block_pos) > max_dist:
        return (
            f"Error: too far from {label} at ({block_pos[0]}, {block_pos[1]}, {block_pos[2]}). "
            f"You are at ({pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f}). "
            f"Use altercraft_goto to get within {max_dist} blocks first."
        )
    return None


def _h_craft(args, **_: Any) -> str:
    a = args or {}
    item = (a.get("item") or "").strip()
    count = int(a.get("count", 1))
    if not item:
        return "Error: item is required"
    if count < 1:
        return "Error: count must be >= 1"
    ok, body = _req("POST", "/action/craft", json={"item": item, "count": count})
    if not ok:
        return body
    return f"Craft ({item} x{count}):\n" + _fmt_json(body)


def _h_recipes(args, **_: Any) -> str:
    a = args or {}
    item = (a.get("item") or "").strip()
    if not item:
        return "Error: item is required"
    ok, body = _req("POST", "/action/recipes", json={"item": item})
    if not ok:
        return body
    payload = body.get("data", body) if isinstance(body, dict) else body
    return f"Recipes for {item}:\n" + _fmt_json(payload)


def _h_smelt(args, **_: Any) -> str:
    a = args or {}
    inp = (a.get("input") or "").strip()
    fuel = (a.get("fuel") or "").strip()
    count = int(a.get("count", 1))
    x = a.get("x")
    y = a.get("y")
    z = a.get("z")
    if not inp or not fuel:
        return "Error: input and fuel are required"
    if count < 1:
        return "Error: count must be >= 1"
    payload: dict[str, Any] = {"input": inp, "fuel": fuel, "count": count}
    if x is not None and y is not None and z is not None:
        payload["x"] = float(x)
        payload["y"] = float(y)
        payload["z"] = float(z)
        err = _assert_near_block((payload["x"], payload["y"], payload["z"]), "furnace")
        if err:
            return err
    ok, body = _req("POST", "/action/smelt", json=payload)
    if not ok:
        return body
    return f"Smelt ({inp} x{count} with {fuel}):\n" + _fmt_json(body)


def _h_smelt_start(args, **_: Any) -> str:
    a = args or {}
    inp = (a.get("input") or "").strip()
    fuel = (a.get("fuel") or "").strip()
    count = int(a.get("count", 1))
    x = a.get("x")
    y = a.get("y")
    z = a.get("z")
    if not inp or not fuel:
        return "Error: input and fuel are required"
    if count < 1:
        return "Error: count must be >= 1"
    payload: dict[str, Any] = {"input": inp, "fuel": fuel, "count": count}
    if x is not None and y is not None and z is not None:
        payload["x"] = float(x)
        payload["y"] = float(y)
        payload["z"] = float(z)
        err = _assert_near_block((payload["x"], payload["y"], payload["z"]), "furnace")
        if err:
            return err
    ok, body = _req("POST", "/action/smelt_start", json=payload)
    if not ok:
        return body
    return f"Smelt started ({inp} x{count} with {fuel}):\n" + _fmt_json(body)


# ─────────────────────────────────────────────────────────────────────
# Client-side production planner
# ─────────────────────────────────────────────────────────────────────

@dataclass
class PlanStep:
    item: str
    count: int
    requires_table: bool = False
    recipe_type: str = "craft"


class RecipeGraph:
    """Lightweight client-side recipe graph built from /action/recipes JSON."""

    def __init__(self, recipes_json: list[dict]) -> None:
        self.nodes: dict[str, dict] = {}
        for r in recipes_json:
            out = r.get("result", {})
            item = out.get("id") or out.get("name")
            if not item:
                continue
            self.nodes[item] = {
                "count": out.get("count", 1),
                "ingredients": r.get("ingredients", {}),
                "requires_table": r.get("requiresTable", False),
                "recipe_type": r.get("type", "craft"),
            }

    def plan(
        self,
        goal: str,
        count: int,
        inventory: dict[str, int],
    ) -> list[PlanStep]:
        """Return topological-order list of crafts to reach goal.

        This is a depth-first, greedy decomposition.  It does not attempt
        optimal batching — that is left to the LLM or a future optimiser.
        """
        steps: list[PlanStep] = []
        need: dict[str, int] = {goal: count}
        # Simple fixed-point expansion until only raw materials remain
        for _ in range(50):  # safety bound
            if not need:
                break
            item = next(iter(need))
            qty = need.pop(item)
            have = inventory.get(item, 0)
            if have >= qty:
                inventory[item] = have - qty
                continue
            qty -= have
            inventory[item] = 0
            recipe = self.nodes.get(item)
            if recipe is None:
                # Raw material — can't craft further
                need[item] = need.get(item, 0) + qty
                continue
            out_count = recipe["count"]
            crafts = (qty + out_count - 1) // out_count
            steps.append(
                PlanStep(
                    item=item,
                    count=crafts * out_count,
                    requires_table=recipe["requires_table"],
                    recipe_type=recipe["recipe_type"],
                )
            )
            for ing, per in recipe["ingredients"].items():
                need[ing] = need.get(ing, 0) + per * crafts
        return steps


def plan_production(goal: str, count: int, recipes_json: list[dict], inventory: dict[str, int]) -> list[PlanStep]:
    """Convenience wrapper around RecipeGraph.plan."""
    graph = RecipeGraph(recipes_json)
    return graph.plan(goal, count, inventory)


# ─────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────

ALTERCRAFT_CRAFT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_craft",
        "description": (
            "Craft items in the agent's inventory or at a nearby crafting table. "
            "Fails if ingredients are missing or no table is within 4 blocks for "
            "3x3 recipes. Use altercraft_recipes first to verify ingredients."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "Minecraft item ID, e.g. 'oak_planks' or 'stone_pickaxe'."},
                "count": {"type": "integer", "description": "How many to craft (default 1).", "default": 1},
            },
            "required": ["item"],
        },
    },
}

ALTERCRAFT_RECIPES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_recipes",
        "description": (
            "Look up the recipe for an item. Returns ingredients, whether a "
            "crafting table is required, and the recipe type. Use before crafting "
            "to avoid missing-ingredient failures."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "Minecraft item ID to look up."},
            },
            "required": ["item"],
        },
    },
}

ALTERCRAFT_SMELT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_smelt",
        "description": (
            "Smelt items at a furnace (blocking, max 30 s). Provide furnace "
            "coordinates if known. The agent must be within 4 blocks. For large "
            "batches use altercraft_smelt_start instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Item ID to smelt, e.g. 'iron_ore'."},
                "fuel": {"type": "string", "description": "Fuel item ID, e.g. 'coal'."},
                "count": {"type": "integer", "description": "Number of items to smelt.", "default": 1},
                "x": {"type": "number", "description": "Furnace X coordinate (optional but recommended)."},
                "y": {"type": "number", "description": "Furnace Y coordinate."},
                "z": {"type": "number", "description": "Furnace Z coordinate."},
            },
            "required": ["input", "fuel"],
        },
    },
}

ALTERCRAFT_SMELT_START_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_smelt_start",
        "description": (
            "Start smelting at a furnace and return immediately (fire-and-forget). "
            "Use only when you will remain nearby or will check back with "
            "altercraft_furnace_check within a few minutes. Risk of resource loss "
            "if you disconnect. Furnace coordinates required."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Item ID to smelt."},
                "fuel": {"type": "string", "description": "Fuel item ID."},
                "count": {"type": "integer", "description": "Number of items to smelt.", "default": 1},
                "x": {"type": "number", "description": "Furnace X coordinate."},
                "y": {"type": "number", "description": "Furnace Y coordinate."},
                "z": {"type": "number", "description": "Furnace Z coordinate."},
            },
            "required": ["input", "fuel"],
        },
    },
}

registry.register(
    name="altercraft_craft",
    toolset="altercraft",
    schema=ALTERCRAFT_CRAFT_SCHEMA,
    handler=_h_craft,
    check_fn=_check_server_available,
    emoji="🔨",
    description=ALTERCRAFT_CRAFT_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_recipes",
    toolset="altercraft",
    schema=ALTERCRAFT_RECIPES_SCHEMA,
    handler=_h_recipes,
    check_fn=_check_server_available,
    emoji="📚",
    description=ALTERCRAFT_RECIPES_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_smelt",
    toolset="altercraft",
    schema=ALTERCRAFT_SMELT_SCHEMA,
    handler=_h_smelt,
    check_fn=_check_server_available,
    emoji="🔥",
    description=ALTERCRAFT_SMELT_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_smelt_start",
    toolset="altercraft",
    schema=ALTERCRAFT_SMELT_START_SCHEMA,
    handler=_h_smelt_start,
    check_fn=_check_server_available,
    emoji="⚡",
    description=ALTERCRAFT_SMELT_START_SCHEMA["function"]["description"],
)

# COMBAT MODULE (merged from researcher deliverable)
# ==================================================================


def _resolve_target(a: dict) -> str | None:
    """Extract target name from common arg aliases."""
    return (
        a.get("target")
        or a.get("entity")
        or a.get("mob")
        or a.get("player")
        or a.get("name")
        or None
    )


def _h_attack(args, **_: Any) -> str:
    """Single hit against a target entity."""
    target = _resolve_target(args or {})
    if not target:
        return "Error: attack requires a target (entity name or uuid)."
    ok, body = _req("POST", "/action/attack", json={"target": target})
    if not ok:
        return body
    return f"Attack on {target}:\n" + _fmt_json(body)


def _h_eat(args, **_: Any) -> str:
    """Consume the best available food from inventory."""
    ok, body = _req("POST", "/action/eat", json={})
    if not ok:
        return body
    item = body.get("item") if isinstance(body, dict) else None
    return f"Ate {item or 'food'}.\n" + _fmt_json(body)


def _h_fight(args, **_: Any) -> str:
    """Sustained combat with optional retreat threshold and duration cap."""
    a = args or {}
    target = _resolve_target(a)
    if not target:
        return "Error: fight requires a target."
    payload: dict[str, Any] = {"target": target}
    if "retreat_health" in a:
        payload["retreat_health"] = float(a["retreat_health"])
    if "duration" in a:
        payload["duration"] = min(float(a["duration"]), 30.0)
    ok, body = _req("POST", "/task/fight", json=payload)
    if not ok:
        return body
    return f"Fight vs {target} started:\n" + _fmt_json(body)


def _h_flee(args, **_: Any) -> str:
    """Run away from a threat or to a safe distance."""
    a = args or {}
    payload: dict[str, Any] = {}
    if "distance" in a:
        payload["distance"] = float(a["distance"])
    if "from" in a:
        payload["from"] = str(a["from"])
    ok, body = _req("POST", "/action/flee", json=payload)
    if not ok:
        return body
    return "Fleeing:\n" + _fmt_json(body)


def _h_sneak(args, **_: Any) -> str:
    """Toggle sneak mode."""
    a = args or {}
    enable = bool(a.get("enable", True))
    ok, body = _req("POST", "/action/sneak", json={"enable": enable})
    if not ok:
        return body
    return f"Sneak {'enabled' if enable else 'disabled'}.\n" + _fmt_json(body)


def _h_shield_block(args, **_: Any) -> str:
    """Raise shield for a duration (seconds). 0 = indefinite until cancelled."""
    a = args or {}
    payload: dict[str, Any] = {}
    if "duration" in a:
        payload["duration"] = float(a["duration"])
    ok, body = _req("POST", "/action/shield_block", json=payload)
    if not ok:
        return body
    return "Shield block:\n" + _fmt_json(body)


def _h_shoot(args, **_: Any) -> str:
    """Fire bow/crossbow at target with optional leading prediction."""
    a = args or {}
    target = _resolve_target(a)
    if not target:
        return "Error: shoot requires a target."
    payload: dict[str, Any] = {"target": target}
    if "predict" in a:
        payload["predict"] = bool(a["predict"])
    ok, body = _req("POST", "/action/shoot", json=payload)
    if not ok:
        return body
    return f"Shot at {target}:\n" + _fmt_json(body)


def _h_sprint_attack(args, **_: Any) -> str:
    """Sprint-hit for extra knockback."""
    a = args or {}
    target = _resolve_target(a)
    if not target:
        return "Error: sprint_attack requires a target."
    ok, body = _req("POST", "/action/sprint_attack", json={"target": target})
    if not ok:
        return body
    return f"Sprint-attack on {target}:\n" + _fmt_json(body)


def _h_critical_hit(args, **_: Any) -> str:
    """Jump-crit for ~150% damage."""
    a = args or {}
    target = _resolve_target(a)
    if not target:
        return "Error: critical_hit requires a target."
    ok, body = _req("POST", "/action/critical_hit", json={"target": target})
    if not ok:
        return body
    return f"Critical hit on {target}:\n" + _fmt_json(body)


def _h_strafe(args, **_: Any) -> str:
    """Lateral movement while fighting (async task)."""
    a = args or {}
    target = _resolve_target(a)
    if not target:
        return "Error: strafe requires a target."
    payload: dict[str, Any] = {"target": target}
    if "direction" in a:
        payload["direction"] = str(a["direction"])
    if "duration" in a:
        payload["duration"] = float(a["duration"])
    ok, body = _req("POST", "/task/strafe", json=payload)
    if not ok:
        return body
    return f"Strafe vs {target} started:\n" + _fmt_json(body)


def _h_combo(args, **_: Any) -> str:
    """Chained attack sequence (aggressive/defensive/ranged/berserker)."""
    a = args or {}
    target = _resolve_target(a)
    style = str(a.get("style", "aggressive")).lower()
    if style not in {"aggressive", "defensive", "ranged", "berserker"}:
        return (
            f"Error: combo style '{style}' not supported. "
            "Choose: aggressive, defensive, ranged, berserker."
        )
    if not target:
        return "Error: combo requires a target."
    ok, body = _req("POST", "/task/combo", json={"target": target, "style": style})
    if not ok:
        return body
    return f"Combo ({style}) on {target} started:\n" + _fmt_json(body)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

ALTERCRAFT_ATTACK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_attack",
        "description": (
            "Deliver a single melee hit to a visible hostile entity. "
            "Use for finishing blows or when you only need one strike."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Name or UUID of the entity to attack.",
                }
            },
            "required": ["target"],
        },
    },
}

ALTERCRAFT_EAT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_eat",
        "description": (
            "Consume the best food item currently in inventory. "
            "Use during or after combat to restore hunger/health."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

ALTERCRAFT_FIGHT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_fight",
        "description": (
            "Engage a target in sustained melee combat. The bot pathfinds to the "
            "entity, attacks continuously, and automatically retreats if health "
            "drops below retreat_health. Max duration 30s to prevent infinite loops."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Name or UUID of the entity to fight.",
                },
                "retreat_health": {
                    "type": "number",
                    "description": "HP threshold to trigger retreat (default 6).",
                    "default": 6,
                },
                "duration": {
                    "type": "number",
                    "description": "Max seconds to stay in combat (default 30, hard cap 30).",
                    "default": 30,
                },
            },
            "required": ["target"],
        },
    },
}

ALTERCRAFT_FLEE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_flee",
        "description": (
            "Run away from a threat. Optionally specify distance (blocks) or "
            "the entity to flee from. Use when low on health or overwhelmed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "distance": {
                    "type": "number",
                    "description": "Minimum blocks to put between bot and threat (default 32).",
                    "default": 32,
                },
                "from": {
                    "type": "string",
                    "description": "Entity name/UUID to flee from (optional).",
                },
            },
            "required": [],
        },
    },
}

ALTERCRAFT_SNEAK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_sneak",
        "description": (
            "Toggle sneak mode. Sneaking reduces detection range and prevents "
            "falling off edges. Use before ambush or while navigating cliffs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "enable": {
                    "type": "boolean",
                    "description": "True to start sneaking, False to stop.",
                    "default": True,
                }
            },
            "required": [],
        },
    },
}

ALTERCRAFT_SHIELD_BLOCK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_shield_block",
        "description": (
            "Raise the shield (off-hand) to block incoming damage. "
            "Essential vs skeletons and melee brawlers. Pass duration in seconds; "
            "omit for indefinite block until cancelled."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "duration": {
                    "type": "number",
                    "description": "Seconds to hold block. 0 or omit = until cancelled.",
                }
            },
            "required": [],
        },
    },
}

ALTERCRAFT_SHOOT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_shoot",
        "description": (
            "Fire a bow or crossbow at a target. Requires arrows in inventory. "
            "Set predict=true to lead moving targets. Best for creepers and "
            "unreachable mobs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Name or UUID of the entity to shoot.",
                },
                "predict": {
                    "type": "boolean",
                    "description": "Lead the target based on velocity (default true).",
                    "default": True,
                },
            },
            "required": ["target"],
        },
    },
}

ALTERCRAFT_SPRINT_ATTACK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_sprint_attack",
        "description": (
            "Sprint toward a target and strike for extra knockback. "
            "Resets zombie/vindicator spacing. Good opener before a combo."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Name or UUID of the entity to charge.",
                }
            },
            "required": ["target"],
        },
    },
}

ALTERCRAFT_CRITICAL_HIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_critical_hit",
        "description": (
            "Jump and strike for ~150% damage (critical hit). Slightly harder to "
            "land vs ranged enemies, but devastating against slow melee mobs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Name or UUID of the entity to crit.",
                }
            },
            "required": ["target"],
        },
    },
}

ALTERCRAFT_STRAFE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_strafe",
        "description": (
            "Circle-strafe around a target while attacking (async task). "
            "Reduces hit-taken vs melee. Pass direction='left' or 'right'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Name or UUID of the entity to strafe around.",
                },
                "direction": {
                    "type": "string",
                    "description": "'left' (default) or 'right'.",
                    "default": "left",
                },
                "duration": {
                    "type": "number",
                    "description": "Seconds to strafe (default 10).",
                    "default": 10,
                },
            },
            "required": ["target"],
        },
    },
}

ALTERCRAFT_COMBO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_combo",
        "description": (
            "Execute a chained combat sequence against a target (async task). "
            "Styles: aggressive (DPS focus), defensive (shield-weave), ranged "
            "(kite with bow), berserker (no retreat, max damage)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Name or UUID of the entity to combo.",
                },
                "style": {
                    "type": "string",
                    "enum": ["aggressive", "defensive", "ranged", "berserker"],
                    "description": "Combo style (default aggressive).",
                    "default": "aggressive",
                },
            },
            "required": ["target"],
        },
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

# NOTE: Each registration must be a top-level statement — the discovery
# AST check in tools/registry.py only recognises ``registry.register(...)``
# at module scope, not inside loops or conditionals.

registry.register(
    name="altercraft_attack",
    toolset="altercraft",
    schema=ALTERCRAFT_ATTACK_SCHEMA,
    handler=_h_attack,
    check_fn=_check_server_available,
    emoji="⚔️",
    description=ALTERCRAFT_ATTACK_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_eat",
    toolset="altercraft",
    schema=ALTERCRAFT_EAT_SCHEMA,
    handler=_h_eat,
    check_fn=_check_server_available,
    emoji="🍖",
    description=ALTERCRAFT_EAT_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_fight",
    toolset="altercraft",
    schema=ALTERCRAFT_FIGHT_SCHEMA,
    handler=_h_fight,
    check_fn=_check_server_available,
    emoji="🛡️",
    description=ALTERCRAFT_FIGHT_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_flee",
    toolset="altercraft",
    schema=ALTERCRAFT_FLEE_SCHEMA,
    handler=_h_flee,
    check_fn=_check_server_available,
    emoji="🏃",
    description=ALTERCRAFT_FLEE_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_sneak",
    toolset="altercraft",
    schema=ALTERCRAFT_SNEAK_SCHEMA,
    handler=_h_sneak,
    check_fn=_check_server_available,
    emoji="🥷",
    description=ALTERCRAFT_SNEAK_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_shield_block",
    toolset="altercraft",
    schema=ALTERCRAFT_SHIELD_BLOCK_SCHEMA,
    handler=_h_shield_block,
    check_fn=_check_server_available,
    emoji="🛡️",
    description=ALTERCRAFT_SHIELD_BLOCK_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_shoot",
    toolset="altercraft",
    schema=ALTERCRAFT_SHOOT_SCHEMA,
    handler=_h_shoot,
    check_fn=_check_server_available,
    emoji="🏹",
    description=ALTERCRAFT_SHOOT_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_sprint_attack",
    toolset="altercraft",
    schema=ALTERCRAFT_SPRINT_ATTACK_SCHEMA,
    handler=_h_sprint_attack,
    check_fn=_check_server_available,
    emoji="💨",
    description=ALTERCRAFT_SPRINT_ATTACK_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_critical_hit",
    toolset="altercraft",
    schema=ALTERCRAFT_CRITICAL_HIT_SCHEMA,
    handler=_h_critical_hit,
    check_fn=_check_server_available,
    emoji="⭕",
    description=ALTERCRAFT_CRITICAL_HIT_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_strafe",
    toolset="altercraft",
    schema=ALTERCRAFT_STRAFE_SCHEMA,
    handler=_h_strafe,
    check_fn=_check_server_available,
    emoji="🏃‍♂️",
    description=ALTERCRAFT_STRAFE_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_combo",
    toolset="altercraft",
    schema=ALTERCRAFT_COMBO_SCHEMA,
    handler=_h_combo,
    check_fn=_check_server_available,
    emoji="🔥",
    description=ALTERCRAFT_COMBO_SCHEMA["function"]["description"],
)

# ==================================================================
# MISSING MODULE — fills gaps vs server.js ACTIONS (iterated)
# ==================================================================

# ── Handlers ────────────────────────────────────────────────────────────────


def _h_equip(args, **_: Any) -> str:
    a = args or {}
    item = (a.get("item") or "").strip()
    if not item:
        return "Error: item is required"
    slot = (a.get("slot") or "hand").strip()
    ok, body = _req("POST", "/action/equip", json={"item": item, "slot": slot})
    if not ok:
        return body
    return f"Equipped {item} to {slot}:\n" + _fmt_json(body)


def _h_toss(args, **_: Any) -> str:
    a = args or {}
    item = (a.get("item") or "").strip()
    if not item:
        return "Error: item is required"
    count = a.get("count")
    payload: dict[str, Any] = {"item": item}
    if count is not None:
        payload["count"] = int(count)
    ok, body = _req("POST", "/action/toss", json=payload)
    if not ok:
        return body
    return f"Tossed {item}:\n" + _fmt_json(body)


def _h_wait(args, **_: Any) -> str:
    a = args or {}
    seconds = float(a.get("seconds", 5))
    ok, body = _req("POST", "/action/wait", json={"seconds": seconds})
    if not ok:
        return body
    return f"Waited {seconds}s:\n" + _fmt_json(body)


def _h_use(args, **_: Any) -> str:
    ok, body = _req("POST", "/action/use")
    if not ok:
        return body
    return "Use item:\n" + _fmt_json(body)


def _h_sleep_bed(args, **_: Any) -> str:
    ok, body = _req("POST", "/action/sleep_bed")
    if not ok:
        return body
    return "Sleep:\n" + _fmt_json(body)


def _h_complete_command(args, **_: Any) -> str:
    a = args or {}
    index = int(a.get("index", 0))
    ok, body = _req("POST", "/action/complete_command", json={"index": index})
    if not ok:
        return body
    return f"Command #{index} completed:\n" + _fmt_json(body)


def _h_find_entities(args, **_: Any) -> str:
    a = args or {}
    payload: dict[str, Any] = {"radius": int(a.get("radius", 32))}
    if "type" in a:
        payload["type"] = str(a["type"])
    ok, body = _req("POST", "/action/find_entities", json=payload)
    if not ok:
        return body
    return f"Entities found:\n" + _fmt_json(body)


def _h_team_chat(args, **_: Any) -> str:
    a = args or {}
    message = (a.get("message") or "").strip()
    if not message:
        return "Error: message is required"
    ok, body = _req("POST", "/action/team_chat", json={"message": message})
    if not ok:
        return body
    return f"Team chat:\n" + _fmt_json(body)


def _h_team_status(args, **_: Any) -> str:
    ok, body = _req("POST", "/action/team_status")
    if not ok:
        return body
    return "Team status:\n" + _fmt_json(body)


def _h_rally(args, **_: Any) -> str:
    a = args or {}
    try:
        x, y, z = float(a["x"]), float(a["y"]), float(a["z"])
    except (KeyError, TypeError, ValueError) as e:
        return f"Error: rally needs x,y,z numbers ({e})"
    payload: dict[str, Any] = {"x": x, "y": y, "z": z}
    msg = (a.get("message") or "").strip()
    if msg:
        payload["message"] = msg
    ok, body = _req("POST", "/action/rally", json=payload)
    if not ok:
        return body
    return f"Rally set at ({x}, {y}, {z}):\n" + _fmt_json(body)


def _h_report(args, **_: Any) -> str:
    a = args or {}
    message = (a.get("message") or "").strip()
    if not message:
        return "Error: message is required"
    ok, body = _req("POST", "/action/report", json={"message": message})
    if not ok:
        return body
    return f"Report sent:\n" + _fmt_json(body)


def _h_set_team(args, **_: Any) -> str:
    a = args or {}
    team = (a.get("team") or "").strip()
    if not team:
        return "Error: team is required"
    payload: dict[str, Any] = {"team": team}
    role = (a.get("role") or "").strip()
    if role:
        payload["role"] = role
    teammates = a.get("teammates")
    if teammates is not None:
        payload["teammates"] = teammates
    ok, body = _req("POST", "/action/set_team", json=payload)
    if not ok:
        return body
    return f"Team set to {team}:\n" + _fmt_json(body)


def _h_set_fair_play(args, **_: Any) -> str:
    a = args or {}
    enabled = bool(a.get("enabled", True))
    ok, body = _req("POST", "/action/set_fair_play", json={"enabled": enabled})
    if not ok:
        return body
    return f"Fair play: {enabled}:\n" + _fmt_json(body)


# ── GET observation handlers ────────────────────────────────────────────────


def _h_deaths(args, **_: Any) -> str:
    ok, body = _req("GET", "/deaths")
    if not ok:
        return body
    return "Deaths:\n" + _fmt_json(body)


def _h_team(args, **_: Any) -> str:
    ok, body = _req("GET", "/team")
    if not ok:
        return body
    return "Team config:\n" + _fmt_json(body)


def _h_stats(args, **_: Any) -> str:
    ok, body = _req("GET", "/stats")
    if not ok:
        return body
    return "Combat stats:\n" + _fmt_json(body)


def _h_furnaces(args, **_: Any) -> str:
    ok, body = _req("GET", "/furnaces")
    if not ok:
        return body
    return "Active furnaces:\n" + _fmt_json(body)


def _h_task_status(args, **_: Any) -> str:
    ok, body = _req("GET", "/task")
    if not ok:
        return body
    return "Task status:\n" + _fmt_json(body)


# ── Schemas ─────────────────────────────────────────────────────────────────

ALTERCRAFT_EQUIP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_equip",
        "description": "Equip an item from inventory to a specific slot (hand, head, chest, legs, feet, off-hand).",
        "parameters": {
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "Item name, e.g. 'iron_sword' or 'diamond_helmet'."},
                "slot": {"type": "string", "description": "Slot to equip to. Default: hand.", "default": "hand"},
            },
            "required": ["item"],
        },
    },
}

ALTERCRAFT_TOSS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_toss",
        "description": "Drop/toss items from inventory onto the ground. Omit count to drop the whole stack.",
        "parameters": {
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "Item name to drop."},
                "count": {"type": "integer", "description": "Number to drop. Omit for full stack."},
            },
            "required": ["item"],
        },
    },
}

ALTERCRAFT_WAIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_wait",
        "description": "Wait for a number of seconds. Max 60. Useful for cooldowns, furnace smelting, or letting mobs pass.",
        "parameters": {
            "type": "object",
            "properties": {
                "seconds": {"type": "number", "description": "Seconds to wait (default 5, max 60).", "default": 5},
            },
            "required": [],
        },
    },
}

ALTERCRAFT_USE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_use",
        "description": "Use/activate the currently held item (right-click). E.g. place a boat, throw an eye of ender, eat food, drink a potion.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

ALTERCRAFT_SLEEP_BED_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_sleep_bed",
        "description": "Find and sleep in the nearest bed within 4 blocks. Sets spawn point and skips night.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

ALTERCRAFT_COMPLETE_COMMAND_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_complete_command",
        "description": "Mark a pending command from the in-game command queue as completed. Use after fulfilling a player's request.",
        "parameters": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "Index of the pending command to mark done (default 0).", "default": 0},
            },
            "required": [],
        },
    },
}

ALTERCRAFT_FIND_ENTITIES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_find_entities",
        "description": "Find nearby entities (mobs, players, animals) with optional type filter and radius. Fair-play LOS filtering applies.",
        "parameters": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "Entity type filter, e.g. 'zombie', 'player', 'cow'. Omit for all."},
                "radius": {"type": "integer", "description": "Search radius in blocks (default 32, max 64).", "default": 32},
            },
            "required": [],
        },
    },
}

ALTERCRAFT_TEAM_CHAT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_team_chat",
        "description": "Send a private message to all teammates. Requires being assigned to a team via set_team first.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message text to broadcast to teammates."},
            },
            "required": ["message"],
        },
    },
}

ALTERCRAFT_TEAM_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_team_status",
        "description": "Get team assignment, role, rally point, and live positions/health of all teammates.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

ALTERCRAFT_RALLY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_rally",
        "description": "Set a rally point and broadcast it to all teammates. Teammates can then use goto to reach it.",
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
                "message": {"type": "string", "description": "Optional custom rally message."},
            },
            "required": ["x", "y", "z"],
        },
    },
}

ALTERCRAFT_REPORT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_report",
        "description": "Send an intel report to teammates with current position. Use to share discoveries, threats, or resource locations.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Intel message text."},
            },
            "required": ["message"],
        },
    },
}

ALTERCRAFT_SET_TEAM_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_set_team",
        "description": "Assign this bot to a team with a role and teammate list. Required before using team_chat, rally, or report.",
        "parameters": {
            "type": "object",
            "properties": {
                "team": {"type": "string", "description": "Team name or color, e.g. 'red', 'blue', 'alpha'."},
                "role": {"type": "string", "description": "Role: commander, warrior, ranger, support. Default: warrior.", "default": "warrior"},
                "teammates": {"type": "array", "items": {"type": "string"}, "description": "List of teammate usernames."},
            },
            "required": ["team"],
        },
    },
}

ALTERCRAFT_SET_FAIR_PLAY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_set_fair_play",
        "description": "Toggle fair-play mode. ON = LOS filtering, sound events, reaction delays. OFF = god-mode perception.",
        "parameters": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "description": "True to enable fair play, False to disable.", "default": True},
            },
            "required": [],
        },
    },
}

ALTERCRAFT_DEATHS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_deaths",
        "description": "Get death log: total deaths, last death location, items lost, and seconds since death.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

ALTERCRAFT_TEAM_OBS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_team_obs",
        "description": "Get raw team configuration data (team name, role, teammates, rally point, chat history).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

ALTERCRAFT_STATS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_stats",
        "description": "Get combat statistics: kills, deaths, assists, damage dealt, damage taken.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

ALTERCRAFT_FURNACES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_furnaces",
        "description": "List all active furnaces with their coordinates, input item, ETA, and remaining seconds.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

ALTERCRAFT_TASK_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "altercraft_task_status",
        "description": "Check the status of the current background task (running, done, error, stuck) and elapsed time.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


# ── Registration ────────────────────────────────────────────────────────────

registry.register(
    name="altercraft_equip",
    toolset="altercraft",
    schema=ALTERCRAFT_EQUIP_SCHEMA,
    handler=_h_equip,
    check_fn=_check_server_available,
    emoji="🛡️",
    description=ALTERCRAFT_EQUIP_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_toss",
    toolset="altercraft",
    schema=ALTERCRAFT_TOSS_SCHEMA,
    handler=_h_toss,
    check_fn=_check_server_available,
    emoji="🗑️",
    description=ALTERCRAFT_TOSS_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_wait",
    toolset="altercraft",
    schema=ALTERCRAFT_WAIT_SCHEMA,
    handler=_h_wait,
    check_fn=_check_server_available,
    emoji="⏳",
    description=ALTERCRAFT_WAIT_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_use",
    toolset="altercraft",
    schema=ALTERCRAFT_USE_SCHEMA,
    handler=_h_use,
    check_fn=_check_server_available,
    emoji="👆",
    description=ALTERCRAFT_USE_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_sleep_bed",
    toolset="altercraft",
    schema=ALTERCRAFT_SLEEP_BED_SCHEMA,
    handler=_h_sleep_bed,
    check_fn=_check_server_available,
    emoji="🛏️",
    description=ALTERCRAFT_SLEEP_BED_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_complete_command",
    toolset="altercraft",
    schema=ALTERCRAFT_COMPLETE_COMMAND_SCHEMA,
    handler=_h_complete_command,
    check_fn=_check_server_available,
    emoji="✅",
    description=ALTERCRAFT_COMPLETE_COMMAND_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_find_entities",
    toolset="altercraft",
    schema=ALTERCRAFT_FIND_ENTITIES_SCHEMA,
    handler=_h_find_entities,
    check_fn=_check_server_available,
    emoji="👁️",
    description=ALTERCRAFT_FIND_ENTITIES_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_team_chat",
    toolset="altercraft",
    schema=ALTERCRAFT_TEAM_CHAT_SCHEMA,
    handler=_h_team_chat,
    check_fn=_check_server_available,
    emoji="📢",
    description=ALTERCRAFT_TEAM_CHAT_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_team_status",
    toolset="altercraft",
    schema=ALTERCRAFT_TEAM_STATUS_SCHEMA,
    handler=_h_team_status,
    check_fn=_check_server_available,
    emoji="👥",
    description=ALTERCRAFT_TEAM_STATUS_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_rally",
    toolset="altercraft",
    schema=ALTERCRAFT_RALLY_SCHEMA,
    handler=_h_rally,
    check_fn=_check_server_available,
    emoji="🚩",
    description=ALTERCRAFT_RALLY_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_report",
    toolset="altercraft",
    schema=ALTERCRAFT_REPORT_SCHEMA,
    handler=_h_report,
    check_fn=_check_server_available,
    emoji="📡",
    description=ALTERCRAFT_REPORT_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_set_team",
    toolset="altercraft",
    schema=ALTERCRAFT_SET_TEAM_SCHEMA,
    handler=_h_set_team,
    check_fn=_check_server_available,
    emoji="🏷️",
    description=ALTERCRAFT_SET_TEAM_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_set_fair_play",
    toolset="altercraft",
    schema=ALTERCRAFT_SET_FAIR_PLAY_SCHEMA,
    handler=_h_set_fair_play,
    check_fn=_check_server_available,
    emoji="⚖️",
    description=ALTERCRAFT_SET_FAIR_PLAY_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_deaths",
    toolset="altercraft",
    schema=ALTERCRAFT_DEATHS_SCHEMA,
    handler=_h_deaths,
    check_fn=_check_server_available,
    emoji="💀",
    description=ALTERCRAFT_DEATHS_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_team_obs",
    toolset="altercraft",
    schema=ALTERCRAFT_TEAM_OBS_SCHEMA,
    handler=_h_team,
    check_fn=_check_server_available,
    emoji="👥",
    description=ALTERCRAFT_TEAM_OBS_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_stats",
    toolset="altercraft",
    schema=ALTERCRAFT_STATS_SCHEMA,
    handler=_h_stats,
    check_fn=_check_server_available,
    emoji="📊",
    description=ALTERCRAFT_STATS_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_furnaces",
    toolset="altercraft",
    schema=ALTERCRAFT_FURNACES_SCHEMA,
    handler=_h_furnaces,
    check_fn=_check_server_available,
    emoji="🔥",
    description=ALTERCRAFT_FURNACES_SCHEMA["function"]["description"],
)

registry.register(
    name="altercraft_task_status",
    toolset="altercraft",
    schema=ALTERCRAFT_TASK_STATUS_SCHEMA,
    handler=_h_task_status,
    check_fn=_check_server_available,
    emoji="📝",
    description=ALTERCRAFT_TASK_STATUS_SCHEMA["function"]["description"],
)
