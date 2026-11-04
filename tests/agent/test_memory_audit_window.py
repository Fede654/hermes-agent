"""Audit evidence uses durable history without replaying the entire conversation."""
import sqlite3

from agent.memory_audit_window import prepare_window
from hermes_state import SessionDB


def test_window_is_bounded_and_watermarked(tmp_path):
    path = tmp_path / 'state.db'
    db = SessionDB(path)
    db.create_session('topic', 'telegram')
    ids = [db.append_message('topic', 'user', f'decision {i}', timestamp=100 + i) for i in range(130)]
    fact = '{"success": true, "content": "' + 'x' * 1000 + '"}'
    db.append_message('topic', 'tool', fact, tool_name='fact_store', timestamp=230)
    db.append_message('topic', 'tool', 'y' * 10000, tool_name='terminal', timestamp=231)
    final_id = db.append_message('topic', 'assistant', 'saved', timestamp=232)
    window = prepare_window(path, 'topic', ids[0], now=30000, max_messages=120)
    assert window.through_id == final_id
    assert window.omitted_messages == 10
    assert len([r for r in window.rows if r['role'] in ('user', 'assistant')]) == 120
    assert next(r for r in window.rows if r['tool_name'] == 'fact_store')['content'] == fact
    assert len(next(r for r in window.rows if r['tool_name'] == 'terminal')['content']) == 240
    assert prepare_window(path, 'topic', final_id, now=30000) is None
    db.close()


def test_foreground_and_new_user_activity_defer_audit(tmp_path):
    path = tmp_path / 'state.db'
    db = SessionDB(path)
    db.create_session('topic', 'telegram')
    db.append_message('topic', 'user', 'old decision', timestamp=100)
    assert prepare_window(path, 'topic', 0, now=30000) is not None
    with sqlite3.connect(path) as conn:
        conn.execute('INSERT INTO session_turn_leases VALUES (?,?,?,?)', ('another-topic', 'owner', 29000, 31000))
    assert prepare_window(path, 'topic', 0, now=30000) is None
    with sqlite3.connect(path) as conn:
        conn.execute('DELETE FROM session_turn_leases')
    db.append_message('topic', 'user', 'new decision', timestamp=29999)
    assert prepare_window(path, 'topic', 0, now=30000) is None
    db.close()
