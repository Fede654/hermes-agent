"""Read-only, bounded evidence windows for doctrine-based knowledge audits.

This module neither calls a model nor writes memories. The consumer supplies the
persisted watermark only after an audit finishes successfully. Rows discarded by
window caps are explicitly reported, never silently represented as audited.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_MEMORY_TOOLS = frozenset({'fact_store', 'memory', 'skill_manage'})


@dataclass(frozen=True)
class AuditWindow:
    session_id: str
    watermark: int
    through_id: int
    rows: tuple[dict, ...]
    omitted_messages: int


def session_busy(conn: sqlite3.Connection, session_id: str, now: float) -> bool:
    """Respect durable foreground, compression and delegation ownership."""
    for table, key in (('session_turn_leases', 'conversation_id'), ('compression_locks', 'session_id')):
        if conn.execute(
            f'SELECT 1 FROM {table} WHERE {key}=? AND expires_at>?', (session_id, now)
        ).fetchone():
            return True
    return conn.execute(
        """SELECT 1 FROM async_delegations
           WHERE (origin_session=? OR origin_session_id=? OR parent_session_id=?)
             AND (state IN ('running','finalizing') OR delivery_state IN ('pending','claimed'))""",
        (session_id, session_id, session_id),
    ).fetchone() is not None


def _evidence(row: sqlite3.Row) -> dict:
    result = {key: row[key] for key in ('id', 'role', 'content', 'tool_name', 'tool_call_id')}
    if row['tool_calls']:
        calls = json.loads(row['tool_calls'])
        result['tool_calls'] = [
            call if call.get('function', {}).get('name') in _MEMORY_TOOLS else {
                'id': call.get('id'),
                'function': {
                    'name': call.get('function', {}).get('name'),
                    'arguments': str(call.get('function', {}).get('arguments', ''))[:240],
                },
            }
            for call in calls
        ]
    if row['role'] == 'tool' and row['tool_name'] not in _MEMORY_TOOLS:
        # Arbitrary tool bodies are unbounded and can contain binary or private data.
        result['content'] = str(row['content'] or '')[:240]
        result['summary_only'] = True
    return result


def prepare_window(
    db_path: Path, session_id: str, watermark: int, *, now: float,
    idle_hours: float = 6, window_days: float = 2, max_messages: int = 120,
    overlap: int = 10,
) -> AuditWindow | None:
    """Return eligible evidence from one consistent SQLite read snapshot.

    Archived/compacted rows remain evidence: their active flag only describes
    model context. Synthetic compression summaries are excluded from raw history.
    Missing coordination tables fail closed with SQLite's error (old schema).
    """
    if idle_hours <= 0 or window_days <= 0 or max_messages <= 0 or overlap < 0 or watermark < 0:
        raise ValueError('Audit limits must be positive; watermark and overlap must be nonnegative')
    conn = sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN')
        if session_busy(conn, session_id, now):
            return None
        latest = conn.execute(
            "SELECT MAX(timestamp), MAX(id) FROM messages WHERE session_id=? AND role='user' AND observed=0",
            (session_id,),
        ).fetchone()
        if latest[0] is None or now - latest[0] < idle_hours * 3600:
            return None
        new = conn.execute(
            """SELECT id FROM messages WHERE session_id=? AND id>? AND timestamp>=?
               AND role IN ('user','assistant') AND content IS NOT NULL AND content!=''
               AND _compressed_summary=0 ORDER BY id DESC""",
            (session_id, watermark, now - window_days * 86400),
        ).fetchall()
        if not new:
            return None
        chosen = new[:max_messages]
        first_id, through_id = chosen[-1][0], chosen[0][0]
        prior = conn.execute(
            """SELECT id FROM messages WHERE session_id=? AND id<=? AND timestamp>=?
               AND role IN ('user','assistant') AND content IS NOT NULL AND content!=''
               AND _compressed_summary=0 ORDER BY id DESC LIMIT ?""",
            (session_id, watermark, now - window_days * 86400, min(overlap, max_messages - len(chosen))),
        ).fetchall()
        if prior and len(new) <= max_messages:
            first_id = min(first_id, prior[-1][0])
        rows = conn.execute(
            """SELECT id,role,content,tool_name,tool_call_id,tool_calls FROM messages
               WHERE session_id=? AND id BETWEEN ? AND ? AND _compressed_summary=0
               ORDER BY id""", (session_id, first_id, through_id),
        ).fetchall()
        return AuditWindow(session_id, watermark, through_id, tuple(map(_evidence, rows)), len(new) - len(chosen))
    finally:
        conn.close()
