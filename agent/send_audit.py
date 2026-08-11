"""Append-only audit log for messaging, for cross-channel forensics.

A lightweight SQLite log of outbound sends (and, optionally, inbound messages)
so that "why did that message land in the wrong chat?" is answerable after the
fact.  Written outside the ephemeral gateway log and enriched with the actual
content, the target resolution, and the session context that produced it.

Path resolution is deliberately **lazy**: the database lives at
``<HERMES_HOME>/audit.db``, resolved per call via :func:`get_hermes_home`, not
captured at import time.  Import-time capture would bake one deployment's
layout into the module (the original of this code hardcoded a single agent's
``/srv/…`` path, which silently followed the wrong home once the agent was
re-homed) and would also ignore the context-local HERMES_HOME override used by
per-task profiles.

Every public function is best-effort: audit logging must never be the reason a
message fails to send, so all failures degrade to a debug log.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def _default_db_path() -> Path:
    """Resolve ``<HERMES_HOME>/audit.db`` at call time."""
    return get_hermes_home() / "audit.db"


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _init_db(db_path: Path) -> None:
    """Create the audit tables if they don't exist (idempotent)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS send_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                session_id TEXT,
                source_platform TEXT,
                source_chat TEXT,
                source_user TEXT,
                platform TEXT NOT NULL,
                target_chat TEXT,
                thread_id TEXT,
                target_specified INTEGER NOT NULL,
                used_home_channel INTEGER NOT NULL,
                content_preview TEXT,
                content_length INTEGER,
                media_count INTEGER,
                success INTEGER,
                error TEXT,
                message_id TEXT,
                extra_json TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_send_events_timestamp ON send_events(timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_send_events_target ON send_events(platform, target_chat)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_send_events_session ON send_events(session_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inbound_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                platform TEXT NOT NULL,
                chat_id TEXT,
                user_id TEXT,
                user_name TEXT,
                message_id TEXT,
                content TEXT,
                content_length INTEGER,
                session_id TEXT,
                extra_json TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_inbound_timestamp ON inbound_messages(timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_inbound_chat ON inbound_messages(platform, chat_id)"
        )
        conn.commit()
    finally:
        conn.close()


def _session_fields() -> tuple[str, str, str, str]:
    """(session_id, source_platform, source_chat, source_user), best effort."""
    try:
        from gateway.session_context import get_session_env

        return (
            get_session_env("HERMES_SESSION_ID", ""),
            get_session_env("HERMES_SESSION_PLATFORM", "cli"),
            get_session_env("HERMES_SESSION_CHAT_ID", ""),
            get_session_env("HERMES_SESSION_USER_ID", ""),
        )
    except Exception:
        return ("", "", "", "")


def log_send_event(
    *,
    platform: str,
    chat_id: str,
    thread_id: str | None,
    content: str,
    media_files: list | None,
    success: bool,
    error: str | None,
    message_id: str | None,
    used_home_channel: bool,
    target_specified: bool,
    extra: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> None:
    """Log one outbound send attempt.  Never raises."""
    try:
        path = db_path or _default_db_path()
        _init_db(path)
        session_id, source_platform, source_chat, source_user = _session_fields()

        preview = (content or "").strip()
        preview = preview[:2000] if preview else "[media-only]"

        conn = sqlite3.connect(path)
        try:
            conn.execute(
                """
                INSERT INTO send_events (
                    timestamp, session_id, source_platform, source_chat, source_user,
                    platform, target_chat, thread_id, target_specified, used_home_channel,
                    content_preview, content_length, media_count, success, error,
                    message_id, extra_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now(),
                    session_id,
                    source_platform,
                    source_chat,
                    source_user,
                    platform,
                    chat_id,
                    thread_id,
                    int(bool(target_specified)),
                    int(bool(used_home_channel)),
                    preview,
                    len(content) if content else 0,
                    len(media_files) if media_files else 0,
                    int(bool(success)),
                    error,
                    message_id,
                    json.dumps(extra) if extra else None,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # never let auditing break a send
        logger.debug("Failed to write send audit log: %s", e)


def log_inbound_message(
    *,
    platform: str,
    chat_id: str,
    user_id: str | None,
    user_name: str | None,
    message_id: str | None,
    content: str,
    timestamp: float | None = None,
    session_id: str | None = None,
    extra: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> None:
    """Log one inbound message.  Never raises."""
    try:
        path = db_path or _default_db_path()
        _init_db(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                """
                INSERT INTO inbound_messages (
                    timestamp, platform, chat_id, user_id, user_name, message_id,
                    content, content_length, session_id, extra_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp if timestamp is not None else _now(),
                    platform,
                    chat_id,
                    user_id,
                    user_name,
                    message_id,
                    content,
                    len(content) if content else 0,
                    session_id,
                    json.dumps(extra) if extra else None,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug("Failed to write inbound audit log: %s", e)
