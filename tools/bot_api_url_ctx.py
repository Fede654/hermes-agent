"""Session-scoped bot API URL routing.

The DaemonCraft (and previously AlterCraft) gateway adapter receives chat
messages from a Minecraft world over WebSocket, and needs the tool layer
to dispatch HTTP back to the *same* bot that sent the message — not just
to whatever the process-wide env var says. This contextvar is the
mechanism: the gateway sets it inside `handle_message`, tools read it
through `get_bot_api_url`, and the gateway resets it on exit.

This module replaces the same-named contextvar that lived inside
`tools/minecraft_tools.py` (retired 2026-05-09 along with the rest of
the mc_*/altercraft_* toolset stack — see legacy/altercraft-toolsets
branch). Keeping the contextvar in a neutral module decouples the
gateway from any specific toolset implementation.

Today only the embodied service path (POST → embodied service → bot)
needs this. Future tools that hit a Mineflayer bot directly should
import from here rather than reintroducing a per-toolset contextvar.
"""
from __future__ import annotations

import contextvars
import os
from typing import Optional


_bot_api_url_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "bot_api_url", default=None
)


def get_bot_api_url() -> str:
    """Resolve the active bot HTTP API URL for the current call context.

    Priority:
      1. Context variable (set by the gateway adapter for the lifetime
         of one inbound message)
      2. ``MC_API_URL`` environment variable (CLI / legacy fallback)
      3. Default ``http://localhost:3001``
    """
    url = _bot_api_url_ctx.get()
    if url:
        return url
    return os.getenv("MC_API_URL", "http://localhost:3001")


def set_bot_api_url(url: str) -> contextvars.Token:
    """Set the contextvar and return the token. Caller MUST `reset` it."""
    return _bot_api_url_ctx.set(url)


def reset_bot_api_url(token: contextvars.Token) -> None:
    _bot_api_url_ctx.reset(token)
