"""Read-only, bounded evidence windows for doctrine-based knowledge audits.

This module neither calls a model nor writes memories. The consumer supplies the
persisted watermark only after an audit finishes successfully. Rows discarded by
window caps are explicitly reported, never silently represented as audited.
"""
from __future__ import annotations

import json
import re
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
    generation: tuple = ()


def session_busy(conn: sqlite3.Connection, session_id: str, now: float) -> bool:
    """Shared knowledge is profile-wide: any foreground/background writer defers audit."""
    for table in ('session_turn_leases', 'compression_locks'):
        if conn.execute(
            f'SELECT 1 FROM {table} WHERE expires_at>?', (now,)
        ).fetchone():
            return True
    return conn.execute(
        """SELECT 1 FROM async_delegations
           WHERE state IN ('running','finalizing') OR delivery_state IN ('pending','claimed')""",
    ).fetchone() is not None


def _evidence(row: sqlite3.Row, call_names: dict) -> dict:
    result = {key: row[key] for key in ('id', 'role', 'content', 'tool_name', 'tool_call_id', 'timestamp')}
    if row['role'] == 'user' and row['content'] and row['api_content'] and row['api_content'].startswith(row['content']):
        suffix = row['api_content'][len(row['content']):]
        blocks = re.findall(r'<memory-context>([\s\S]*?)</memory-context>', suffix)
        if blocks:
            result['injected_memory'] = {'source': 'messages.api_content', 'blocks': blocks,
                                         'meaning': 'Exposed to the model; not proof of use'}
    if not result['tool_name'] and row['tool_call_id']:
        result['tool_name'] = call_names.get(row['tool_call_id'])
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
    if row['role'] == 'tool' and result['tool_name'] not in _MEMORY_TOOLS:
        # Arbitrary tool bodies are unbounded and can contain binary or private data.
        result['content'] = str(row['content'] or '')[:240]
        result['summary_only'] = True
        try:
            payload = json.loads(row['content'] or '')
            result['outcome'] = ('error' if payload.get('error') or payload.get('success') is False else 'success') if isinstance(payload, dict) else 'unknown'
        except (ValueError, TypeError):
            result['outcome'] = 'unknown'
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
            "SELECT MAX(timestamp), MAX(id) FROM messages WHERE session_id=? AND role='user'",
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
        first_id = chosen[-1][0]
        generation = session_generation(conn, session_id)
        through_id = generation[0]
        prior = conn.execute(
            """SELECT id FROM messages WHERE session_id=? AND id<=? AND timestamp>=?
               AND role IN ('user','assistant') AND content IS NOT NULL AND content!=''
               AND _compressed_summary=0 ORDER BY id DESC LIMIT ?""",
            (session_id, watermark, now - window_days * 86400, min(overlap, max_messages - len(chosen))),
        ).fetchall()
        if prior and len(new) <= max_messages:
            first_id = min(first_id, prior[-1][0])
        rows = conn.execute(
            """SELECT id,role,content,tool_name,tool_call_id,tool_calls,timestamp,api_content FROM messages
               WHERE session_id=? AND id BETWEEN ? AND ? AND _compressed_summary=0
               ORDER BY id""", (session_id, first_id, through_id),
        ).fetchall()
        call_names = {}
        for row in rows:
            for call in json.loads(row['tool_calls'] or '[]'):
                call_names[call.get('id')] = call.get('function', {}).get('name')
        evidence = tuple(_evidence(row, call_names) for row in rows)
        return AuditWindow(session_id, watermark, through_id, evidence, len(new) - len(chosen), generation)
    finally:
        conn.close()


def session_generation(conn: sqlite3.Connection, session_id: str) -> tuple:
    """Detect arrivals, deletion/rewind and in-place transcript mutations."""
    messages = conn.execute('SELECT COALESCE(MAX(id),0),COUNT(*) FROM messages WHERE session_id=?', (session_id,)).fetchone()
    session = conn.execute('SELECT transcript_revision FROM sessions WHERE id=?', (session_id,)).fetchone()
    return (messages[0], messages[1], session[0] if session else None)
