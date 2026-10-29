"""Best-effort, profile-scoped audit of outbound ``send_message`` calls.

A SQLite log records the destination, outcome, and source session of each send.
It also stores a short content preview, so the database is created private.

The database lives at ``<HERMES_HOME>/audit.db``, resolved per call via
:func:`get_hermes_home`. Lazy resolution follows the context-local owning
profile; capturing the home at import time would mix audit records across
profile boundaries.

Every public function is best-effort: audit logging must never be the reason a
message fails to send, so all failures degrade to a debug log.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def _default_db_path() -> Path:
    """Resolve ``<HERMES_HOME>/audit.db`` at call time."""
    return get_hermes_home() / "audit.db"


def _now() -> float:
    return datetime.now(UTC).timestamp()


def _init_db(db_path: Path) -> None:
    """Create a private audit database and its send table (idempotent)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
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
        logger.debug("Failed to read send audit session context", exc_info=True)
        return ("", "", "", "")


def log_send_result(result: Any, **event: Any) -> None:
    """Record structured sender outcomes; ignore non-result responses."""
    if isinstance(result, dict):
        log_send_event(
            **event, success=bool(result.get("success")),
            error=result.get("error"), message_id=result.get("message_id"),
        )


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
        logger.debug("Failed to write send audit log: %s", e, exc_info=True)
