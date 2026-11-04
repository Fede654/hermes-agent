"""Durable audit claims, checkpoints and recoverable local reports."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


class AuditStore:
    def __init__(self, home: Path):
        self.root = home / 'memory-audit'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'audit.db'
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        with self.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS claim (id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT, expires REAL);
                CREATE TABLE IF NOT EXISTS checkpoints (session_id TEXT PRIMARY KEY, through_id INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, through_id INTEGER NOT NULL,
                    mode TEXT NOT NULL, status TEXT NOT NULL, report TEXT, created REAL NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def claim(self, timeout: float):
        owner, now = uuid.uuid4().hex, time.time()
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM claim WHERE expires < ?', (now,))
            acquired = conn.execute('INSERT OR IGNORE INTO claim VALUES (1,?,?)', (owner, now + timeout + 60)).rowcount
        try:
            yield owner if acquired else None
        finally:
            if acquired:
                with self.connect() as conn:
                    conn.execute('DELETE FROM claim WHERE owner=?', (owner,))

    def watermark(self, session_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute('SELECT through_id FROM checkpoints WHERE session_id=?', (session_id,)).fetchone()
            return row[0] if row else 0

    def save(self, run_id: str, session_id: str, through_id: int, mode: str, status: str, report: dict):
        with self.connect() as conn:
            conn.execute('INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?)', (
                run_id, session_id, through_id, mode, status, json.dumps(report, ensure_ascii=False), time.time()))
            if status == 'complete':
                conn.execute('''INSERT INTO checkpoints VALUES (?,?) ON CONFLICT(session_id)
                    DO UPDATE SET through_id=MAX(through_id,excluded.through_id)''', (session_id, through_id))

    def pending(self):
        with self.connect() as conn:
            return conn.execute("SELECT id,session_id,through_id,mode,report FROM runs WHERE status IN ('committing','applying')").fetchall()

    def status(self):
        with self.connect() as conn:
            return [dict(zip(('id', 'session_id', 'through_id', 'mode', 'status', 'created'), row)) for row in
                    conn.execute('SELECT id,session_id,through_id,mode,status,created FROM runs ORDER BY created DESC LIMIT 20')]

    def report(self, run_id: str) -> dict:
        with self.connect() as conn:
            row = conn.execute('SELECT status,report FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise ValueError(f'No audit run {run_id}')
        return {**json.loads(row[1]), 'status': row[0]}
