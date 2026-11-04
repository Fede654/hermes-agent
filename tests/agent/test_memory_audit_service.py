"""Real-import audit service contracts against isolated profile and provider state."""
import json
from types import SimpleNamespace
import time

import pytest

from agent.memory_audit_config import AuditSettings
from agent.memory_audit_runner import audit_session, tick
from agent.memory_audit_store import AuditStore
from agent.memory_audit_worker import execute
from hermes_state import SessionDB

PROVIDER = '''
from agent.memory_provider import MemoryProvider
from pathlib import Path
import json
from utils import atomic_write_text

class Provider(MemoryProvider):
    name = 'audit-test'
    def is_available(self): return True
    def get_tool_schemas(self): return []
    def initialize(self, session_id, **kwargs):
        assert kwargs['agent_context'] == 'subagent'
        self.path = Path(kwargs['hermes_home']) / 'facts.json'
    def audit_snapshot(self, **kwargs):
        state = json.loads(self.path.read_text(encoding='utf-8'))
        return {'facts': state['facts'], 'revision': str(state['revision']), 'complete': True, 'atomic_apply': True}
    def audit_batch_status(self, batch_id):
        return json.loads(self.path.read_text(encoding='utf-8'))['receipts'].get(batch_id)
    def apply_audit_batch(self, *, batch_id, expected_revision, operations):
        state = json.loads(self.path.read_text(encoding='utf-8'))
        if batch_id in state['receipts']: return state['receipts'][batch_id]
        if str(state['revision']) != expected_revision: raise ValueError('revision conflict')
        writes = []
        for op in operations:
            if op['content'] == 'FAIL': raise ValueError('simulated rejection')
            old = next(f for f in state['facts'] if f['fact_id'] == op['fact_id'])
            if old['content'] != op['expected_before']: raise ValueError('fact conflict')
            writes.append({'fact_id': old['fact_id'], 'before': old['content'], 'after': op['content']})
            old['content'] = op['content']
        receipt = {'batch_id': batch_id, 'writes': writes}
        state['receipts'][batch_id] = receipt
        state['revision'] += 1
        atomic_write_text(self.path, json.dumps(state))
        return receipt
'''


@pytest.fixture
def profile(tmp_path, monkeypatch):
    home, workspace = tmp_path / 'profile', tmp_path / 'workspace'
    home.mkdir(); workspace.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    for relative in AuditSettings().doctrine_files:
        path = workspace / relative
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text('Library stores sources; wiki stores concepts; episodic memory stores dated decisions.', encoding='utf-8')
    plugin = home / 'plugins' / 'audit-test'
    plugin.mkdir(parents=True)
    (plugin / '__init__.py').write_text(PROVIDER, encoding='utf-8')
    (home / 'facts.json').write_text(json.dumps({'revision': 1, 'receipts': {}, 'facts': [
        {'fact_id': 109, 'content': '2026-11-03: reel not yet published'},
        {'fact_id': 110, 'content': 'duplicate symptom'},
    ]}), encoding='utf-8')
    db = SessionDB(home / 'state.db')
    db.create_session('topic', 'telegram', chat_id='-100', thread_id='7')
    message_id = db.append_message('topic', 'user', 'Confirmed: reel was published today.', timestamp=time.time() - 7 * 3600)
    db.append_message('topic', 'assistant', 'Updated the project.', timestamp=time.time() - 7 * 3600)
    config = {'audit': {'enabled': True, 'workspace': str(workspace)}, 'memory': {'provider': 'audit-test'}}
    with __import__('sqlite3').connect(home / 'state.db') as conn:
        conn.row_factory = __import__('sqlite3').Row
        session = dict(conn.execute('SELECT * FROM sessions WHERE id="topic"').fetchone())
    yield home, workspace, db, config, session, message_id
    db.close()


def install_model(monkeypatch, message_id, *, extra=None):
    calls = []
    plan = {'findings': [], 'summary': 'One outdated decision.', 'operations': [{
        'action': 'change', 'fact_id': 109, 'expected_before': '2026-11-03: reel not yet published',
        'content': '2026-11-04: reel published', 'reason': 'Explicit user confirmation',
        'origin': 'said', 'evidence_ids': [message_id],
    }]}
    if extra:
        plan['operations'].append(extra)
    def call(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(plan)))],
                               usage=SimpleNamespace(model_dump=lambda: {'prompt_tokens': 200, 'completion_tokens': 50}))
    monkeypatch.setattr('agent.auxiliary_client.call_llm', call)
    return calls


def inline(request, *_):
    return execute(request)


def test_dry_run_real_provider_does_not_write_and_second_tick_is_free(profile, monkeypatch):
    home, workspace, db, config, _session, mid = profile
    calls = install_model(monkeypatch, mid)
    before = (home / 'facts.json').read_bytes()
    assert tick(home, config, reviewer=inline)[0]['status'] == 'complete'
    assert tick(home, config, reviewer=inline)[0]['status'] == 'not_due'
    assert len(calls) == 1 and calls[0]['tools'] is None
    assert 'episodic memory' in calls[0]['messages'][0]['content']
    assert (home / 'facts.json').read_bytes() == before
    assert 'Propuesta:' in (workspace / 'bitacora.md').read_text(encoding='utf-8')
    assert db.auxiliary_usage_by_task('topic')['memory_audit']['input_tokens'] == 200


def test_new_user_message_cancels_all_corrections(profile, monkeypatch):
    home, _workspace, db, config, session, mid = profile
    install_model(monkeypatch, mid)
    config['audit']['mode'] = 'apply'
    before = (home / 'facts.json').read_bytes()
    def interrupted(request, *_):
        result = execute(request)
        db.append_message('topic', 'user', 'Wait, another update')
        return result
    result = audit_session(home, config, session, reviewer=interrupted)
    assert result['status'] == 'cancelled'
    assert (home / 'facts.json').read_bytes() == before
    assert AuditStore(home).watermark('topic') == 0


def test_apply_commits_exact_batch_and_recovery_never_replays(profile, monkeypatch):
    home, workspace, _db, config, session, mid = profile
    install_model(monkeypatch, mid)
    config['audit']['mode'] = 'apply'
    from agent import memory_audit_runner as runner
    real_publish = runner.publish_report
    def crash(*args):
        raise OSError('simulated report publication crash')
    monkeypatch.setattr(runner, 'publish_report', crash)
    with pytest.raises(OSError, match='publication crash'):
        audit_session(home, config, session, reviewer=inline)
    state = json.loads((home / 'facts.json').read_text(encoding='utf-8'))
    assert state['revision'] == 2
    pending = AuditStore(home).pending()
    assert AuditStore(home).report(pending[0][0])['status'] == 'committing'
    assert state['facts'][0]['content'] == '2026-11-04: reel published'
    monkeypatch.setattr(runner, 'publish_report', real_publish)
    assert audit_session(home, config, session, reviewer=inline)['status'] == 'not_due'
    assert json.loads((home / 'facts.json').read_text(encoding='utf-8'))['revision'] == 2
    assert not AuditStore(home).pending()
    assert (workspace / 'bitacora.md').read_text(encoding='utf-8').count('<!-- memory-audit:') == 1


def test_failed_batch_leaves_every_fact_unchanged(profile, monkeypatch):
    home, _workspace, _db, config, session, mid = profile
    install_model(monkeypatch, mid, extra={
        'action': 'change', 'fact_id': 110, 'expected_before': 'duplicate symptom',
        'content': 'FAIL', 'reason': 'Simulate a failed second write', 'origin': 'inferred', 'evidence_ids': [mid],
    })
    config['audit']['mode'] = 'apply'
    before = (home / 'facts.json').read_bytes()
    assert audit_session(home, config, session, reviewer=inline)['status'] == 'recovery_required'
    assert (home / 'facts.json').read_bytes() == before
    assert AuditStore(home).watermark('topic') == 0


def test_shared_profile_claim_and_changed_sources_prevent_writes(profile, monkeypatch):
    home, workspace, _db, config, session, mid = profile
    install_model(monkeypatch, mid)
    def overlapping(request, *_):
        assert audit_session(home, config, session, reviewer=inline)['status'] == 'busy'
        result = execute(request)
        (workspace / 'AGENTS.md').write_text('A newer doctrine', encoding='utf-8')
        return result
    assert audit_session(home, config, session, reviewer=overlapping)['status'] == 'cancelled'
    assert AuditStore(home).watermark('topic') == 0


def test_budget_fails_before_model_and_unsupported_apply_stays_read_only(profile, monkeypatch):
    home, _workspace, _db, config, session, mid = profile
    calls = install_model(monkeypatch, mid)
    config['audit']['max_input_tokens'] = 10
    with pytest.raises(ValueError, match='exceeds max_input_tokens'):
        execute({'home': str(home), 'config': config, 'session': session, 'window': {
            'session_id': 'topic', 'watermark': 0, 'through_id': mid, 'rows': (), 'omitted_messages': 0,
        }})
    assert not calls


def test_real_worker_fails_budget_without_writing_provider_state(profile):
    home, _workspace, _db, config, session, _mid = profile
    config['audit']['max_input_tokens'] = 10
    before = (home / 'facts.json').read_bytes()
    result = audit_session(home, config, session)
    assert result['status'] == 'failed'
    report = json.loads((home / 'memory-audit' / f"{result['run_id']}.json").read_text(encoding='utf-8'))
    assert 'exceeds max_input_tokens' in report['error']
    assert (home / 'facts.json').read_bytes() == before
    assert AuditStore(home).watermark('topic') == 0


def test_provider_is_bound_to_explicit_home_even_under_other_launch_profile(profile, monkeypatch, tmp_path):
    home, _workspace, _db, config, session, mid = profile
    other = tmp_path / 'launch-profile'
    other.mkdir()
    (other / 'facts.json').write_text('do not touch', encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(other))
    install_model(monkeypatch, mid)
    config['audit']['mode'] = 'apply'
    assert audit_session(home, config, session, reviewer=inline)['status'] == 'complete'
    assert (other / 'facts.json').read_text(encoding='utf-8') == 'do not touch'
    assert not (other / 'memory-audit').exists()
    assert json.loads((home / 'facts.json').read_text(encoding='utf-8'))['revision'] == 2


def test_worker_is_killed_when_user_arrives(profile, monkeypatch):
    import subprocess
    from agent.memory_audit_runner import run_reviewer
    home, _workspace, db, config, session, _mid = profile
    processes = []
    class Worker:
        returncode = None
        def __init__(self, args, **kwargs):
            assert args[1].endswith('scripts/memory_audit_worker.py')
            assert kwargs['env']['HERMES_HOME'] == str(home)
            self.killed = False
            processes.append(self)
        def wait(self, timeout=None):
            if self.killed:
                self.returncode = -9
                return -9
            db.append_message('topic', 'user', 'I am back')
            raise subprocess.TimeoutExpired('worker', timeout)
        def poll(self): return self.returncode
        def kill(self): self.killed = True
    monkeypatch.setattr('agent.memory_audit_runner.subprocess.Popen', Worker)
    assert audit_session(home, config, session, reviewer=run_reviewer)['status'] == 'cancelled'
    assert processes[0].killed
    assert AuditStore(home).watermark('topic') == 0
