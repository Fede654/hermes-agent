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
