"""Idle audit orchestration; the model process cannot mutate knowledge or deliver messages."""
from __future__ import annotations

from contextlib import contextmanager, closing
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

from agent.memory_audit_backend import atomic_capable, audit_provider, audit_scope
from agent.memory_audit_config import AuditSettings
from agent.memory_audit_context import evidence_unchanged
from agent.memory_audit_reports import publish_report, record_usage
from agent.memory_audit_store import AuditStore
from agent.memory_audit_window import prepare_window, session_busy, session_generation


@contextmanager
def source_connection(home):
    conn = sqlite3.connect((home / 'state.db').resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def still_idle(home: Path, window, now: float) -> bool:
    with source_connection(home) as conn:
        return not session_busy(conn, window.session_id, now) and session_generation(conn, window.session_id) == tuple(window.generation)


def run_reviewer(request: dict, home: Path, workspace: Path, settings, window) -> dict:
    """Cancellation kills only the isolated reviewer; no tool writes ever ran."""
    worker = Path(__file__).resolve().parents[1] / 'scripts/memory_audit_worker.py'
    with tempfile.TemporaryDirectory(prefix='review-', dir=home / 'memory-audit') as temp:
        request_path, output_path = Path(temp) / 'request.json', Path(temp) / 'result.json'
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        env = dict(os.environ, HERMES_HOME=str(home))
        env.pop('PYTHONSTARTUP', None)
        process = subprocess.Popen([sys.executable, str(worker), str(request_path), str(output_path)],
                                   cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + settings.timeout
        def failed_result(error, *, cancelled=False):
            usage_path = Path(str(output_path) + '.usage')
            accounting = json.loads(usage_path.read_text(encoding='utf-8-sig')) if usage_path.exists() else {}
            return {'error': error, 'cancelled': cancelled, 'accounting': accounting,
                    'usage_unknown': bool(accounting.get('usage_unknown'))}
        try:
            while True:
                if not still_idle(home, window, time.time()):
                    return failed_result('User activity or background work invalidated the audit', cancelled=True)
                if time.monotonic() >= deadline:
                    return failed_result('Audit reviewer timeout')
                try:
                    process.wait(timeout=min(0.5, max(0.001, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode != 0 or not output_path.is_file():
                return failed_result('Audit reviewer exited without a result')
            return json.loads(output_path.read_text(encoding="utf-8-sig"))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def _apply(home, workspace, config, session, settings, store, run_id, window, result):
    operations = result['plan']['operations']
    if not operations:
        return None
    if not result.get('atomic_apply') or not result.get('revision'):
        raise RuntimeError('Provider lacks atomic audit batches; use dry_run or upgrade the external provider')
    with audit_provider(config, home, session, workspace) as provider:
        if not atomic_capable(provider):
            raise RuntimeError('Provider lost its atomic audit capability')
        # The durable source fence serializes turn admission with this short commit.
        # A provider batch must be local, atomic and revision-checked; no model/network here.
        with closing(sqlite3.connect(home / 'state.db', timeout=5)) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            if session_busy(conn, window.session_id, time.time()) or session_generation(conn, window.session_id) != tuple(window.generation):
                raise RuntimeError('Session changed before audit commit')
            if not evidence_unchanged(result['fingerprints'], home, workspace):
                raise RuntimeError('Knowledge files changed before audit commit')
            store.save(run_id, session['id'], window.through_id, settings.mode, 'committing', result)
            receipt = provider.apply_audit_batch(batch_id=run_id, expected_revision=result['revision'], operations=operations)
            if not isinstance(receipt, dict) or receipt.get('batch_id') != run_id or not isinstance(receipt.get('writes'), list):
                raise RuntimeError('Provider returned an invalid durable batch receipt; recovery required')
            return receipt


def recover_pending(home, workspace, config, settings, store):
    for run_id, session_id, through_id, mode, raw in store.pending():
        with source_connection(home) as conn:
            row = conn.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
        if row is None:
            raise RuntimeError(f'Cannot recover audit {run_id}: source session is missing')
        report = json.loads(raw)
        with audit_provider(config, home, dict(row), workspace) as provider:
            if not atomic_capable(provider):
                raise RuntimeError(f'Audit {run_id} requires provider receipt recovery before further apply runs')
            receipt = provider.audit_batch_status(run_id)
        report['receipt'] = receipt
        report['status'] = 'complete' if receipt else 'cancelled'
        publish_report(store, workspace, settings, run_id, report)
        store.save(run_id, session_id, through_id, mode, report['status'], report)


def _audit_session(home: Path, config: dict, session: dict, *, reviewer=run_reviewer, now=None) -> dict:
    settings = AuditSettings.from_config(config)
    if not settings.enabled:
        return {'status': 'disabled'}
    workspace = settings.workdir(config)
    store = AuditStore(home)
    with store.claim(settings.timeout) as owner:
        if not owner:
            return {'status': 'busy'}
        recover_pending(home, workspace, config, settings, store)
        window = prepare_window(home / 'state.db', session['id'], store.watermark(session['id']),
                                now=time.time() if now is None else now, idle_hours=settings.idle_hours,
                                window_days=settings.window_days, max_messages=settings.max_messages, overlap=settings.overlap)
        if window is None:
            return {'status': 'not_due'}
        run_id = uuid.uuid4().hex
        request = {'config': config, 'home': str(home), 'session': session, 'window': asdict(window)}
        try:
            result = reviewer(request, home, workspace, settings, window)
        except Exception as exc:
            logging.getLogger(__name__).exception("Knowledge reviewer failed")
            result = {'error': f'{type(exc).__name__}: {exc}'}
        report = {**result, 'run_id': run_id, 'session_id': session['id'], 'watermark': window.watermark,
                  'through_id': window.through_id, 'omitted_messages': window.omitted_messages, 'mode': settings.mode}
        record_usage(home, session['id'], report)
        try:
            if result.get('cancelled') or not still_idle(home, window, time.time()):
                report['status'] = 'cancelled'
            elif result.get('error') or result.get('plan', {}).get('error'):
                report['status'] = 'failed'
            elif not evidence_unchanged(result['fingerprints'], home, workspace):
                report.update(status='cancelled', error='Knowledge files changed while reviewing')
            else:
                if settings.mode == 'apply':
                    report['receipt'] = _apply(home, workspace, config, session, settings, store, run_id, window, report)
                report['status'] = 'complete'
        except Exception as exc:
            logging.getLogger(__name__).exception("Knowledge audit failed")
            # Keep a committing row for crash/receipt recovery, never retry blindly.
            if any(row[0] == run_id for row in store.pending()):
                return {'status': 'recovery_required', 'run_id': run_id, 'error': str(exc)}
            report.update(status='failed', error=str(exc))
        publish_report(store, workspace, settings, run_id, report)
        store.save(run_id, session['id'], window.through_id, settings.mode, report['status'], report)
        return {'status': report['status'], 'run_id': run_id, 'session_id': session['id']}


def tick(home: Path, config: dict, *, session_id: str | None = None, reviewer=run_reviewer) -> list[dict]:
    settings = AuditSettings.from_config(config)
    if not settings.enabled or not (home / 'state.db').exists():
        return []
    with source_connection(home) as conn:
        rows = conn.execute('SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY started_at').fetchall()
    sessions = [dict(row) for row in rows if row['source'] in settings.platforms and (session_id is None or row['id'] == session_id)]
    return [audit_session(home, config, session, reviewer=reviewer) for session in sessions]


def audit_session(home: Path, config: dict, session: dict, *, reviewer=run_reviewer, now=None) -> dict:
    home = home.resolve()
    settings = AuditSettings.from_config(config)
    if not settings.enabled:
        return {'status': 'disabled'}
    store = AuditStore(home)
    if not store.pending() and prepare_window(
        home / 'state.db', session['id'], store.watermark(session['id']),
        now=time.time() if now is None else now, idle_hours=settings.idle_hours,
        window_days=settings.window_days, max_messages=settings.max_messages, overlap=settings.overlap,
    ) is None:
        return {'status': 'not_due'}
    # Credential resolution and provider imports happen only after the cheap SQL gate.
    with audit_scope(home):
        return _audit_session(home, config, session, reviewer=reviewer, now=now)
