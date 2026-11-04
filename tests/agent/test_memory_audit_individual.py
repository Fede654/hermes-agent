"""Default fact_store providers apply individually, with explicit partial-write limits."""
import json

import pytest

from agent.memory_audit_config import AuditSettings
from agent.memory_audit_runner import audit_session, recover_pending
from agent.memory_audit_store import AuditStore
from tests.agent import test_memory_audit_service as service
from tests.agent.test_memory_audit_service import install_model, inline

profile = service.profile

LEGACY = '''
from pathlib import Path
import json
from agent.memory_provider import MemoryProvider
from utils import atomic_write_text

class Provider(MemoryProvider):
    name = 'audit-test'
    def is_available(self): return True
    def initialize(self, session_id, **kwargs):
        self.writable = kwargs['agent_context'] == 'cron'
        self.path = Path(kwargs['hermes_home']) / 'facts.json'
    def get_tool_schemas(self): return [{'name':'fact_store'}]
    def handle_tool_call(self, name, args, **kwargs):
        assert name == 'fact_store'
        state = json.loads(self.path.read_text(encoding='utf-8'))
        if args['action'] in ('list', 'search'):
            return json.dumps({'facts':state['facts']})
        if args['action'] == 'add':
            assert self.writable
            fact = {'fact_id':max(f['fact_id'] for f in state['facts']) + 1, 'content':args['content']}
            state['facts'].append(fact)
            state['revision'] += 1
            atomic_write_text(self.path, json.dumps(state))
            return json.dumps({'status':'added', 'fact_id':fact['fact_id']})
        fact = next(f for f in state['facts'] if f['fact_id'] == args['fact_id'])
        if args['action'] == 'history':
            return json.dumps({'fact_id': fact['fact_id'], 'versions':[{**fact, 'status':'current'}]})
        assert args['action'] == 'change' and self.writable
        if args['content'] == 'FAIL': return json.dumps({'error':'Rejected'})
        fact['content'] = args['content']
        state['revision'] += 1
        atomic_write_text(self.path, json.dumps(state))
        return json.dumps({'status':'changed', 'fact_id':fact['fact_id']})
'''


@pytest.fixture
def legacy_profile(profile):
    home, *_ = profile
    (home / 'plugins/audit-test/__init__.py').write_text(LEGACY, encoding='utf-8')
    return profile


def state(home):
    return json.loads((home / 'facts.json').read_text(encoding='utf-8'))


def second_operation(mid, content='Second corrected fact'):
    return {'action': 'change', 'fact_id': 110, 'expected_before': 'duplicate symptom',
            'content': content, 'reason': 'Confirmed in conversation', 'origin': 'said', 'evidence_ids': [mid]}


def test_default_provider_apply_and_idempotence(legacy_profile, monkeypatch):
    home, _workspace, _db, config, session, mid = legacy_profile
    calls = install_model(monkeypatch, mid)
    config['audit']['mode'] = 'apply'
    result = audit_session(home, config, session, reviewer=inline)
    assert result['status'] == 'complete'
    report = AuditStore(home).report(result['run_id'])
    assert report['receipt']['atomic'] is False
    assert report['receipt']['writes'][0]['before'] == '2026-11-03: reel not yet published'
    assert state(home)['facts'][0]['content'] == '2026-11-04: reel published'
    assert audit_session(home, config, session, reviewer=inline)['status'] == 'not_due'
    assert len(calls) == 1 and state(home)['revision'] == 2


@pytest.mark.parametrize('interruption', ['user_arrival', 'provider_refusal'])
def test_earlier_write_survives_interruption_and_rest_are_reported(legacy_profile, monkeypatch, interruption):
    home, workspace, db, config, session, mid = legacy_profile
    config['audit']['mode'] = 'apply'
    install_model(monkeypatch, mid, extra=second_operation(mid, 'FAIL' if interruption == 'provider_refusal' else 'new'))
    if interruption == 'user_arrival':
        from agent import memory_audit_apply as applier
        real_call = applier.fact_call
        def with_arrival(provider, args):
            result = real_call(provider, args)
            if args['action'] == 'change':
                db.append_message('topic', 'user', 'I am back')
            return result
        monkeypatch.setattr(applier, 'fact_call', with_arrival)
    result = audit_session(home, config, session, reviewer=inline)
    assert result['status'] == 'partial'
    assert state(home)['revision'] == 2
    assert state(home)['facts'][0]['content'] == '2026-11-04: reel published'
    assert state(home)['facts'][1]['content'] == 'duplicate symptom'
    report = AuditStore(home).report(result['run_id'])
    assert report['receipt']['operations'][0]['status'] == 'applied'
    assert report['receipt']['operations'][1]['status'] in ('deferred', 'unknown')
    assert report['receipt']['known_limit']
    assert AuditStore(home).watermark('topic') == 0
    assert 'individual' in (workspace / 'bitacora.md').read_text(encoding='utf-8')


def test_crash_after_individual_commit_is_unknown_never_replayed(legacy_profile, monkeypatch):
    home, workspace, _db, config, session, mid = legacy_profile
    config['audit']['mode'] = 'apply'
    install_model(monkeypatch, mid)
    real_save = AuditStore.save
    def interrupted_save(self, run_id, session_id, through_id, mode, status, report):
        operations = (report.get('receipt') or {}).get('operations') or []
        if operations and operations[0]['status'] == 'applied':
            raise OSError('process stopped after provider commit')
        return real_save(self, run_id, session_id, through_id, mode, status, report)
    monkeypatch.setattr(AuditStore, 'save', interrupted_save)
    result = audit_session(home, config, session, reviewer=inline)
    assert result['status'] == 'recovery_required'
    assert state(home)['revision'] == 2
    monkeypatch.setattr(AuditStore, 'save', real_save)
    store = AuditStore(home)
    recover_pending(home, workspace, config, AuditSettings.from_config(config), store)
    report = store.report(result['run_id'])
    assert report['status'] == 'partial'
    assert report['receipt']['operations'][0]['status'] == 'unknown'
    assert state(home)['revision'] == 2
    assert store.watermark('topic') == 0
    assert not store.pending()


@pytest.mark.parametrize('duplicate', [False, True])
def test_add_searches_first_and_skips_existing_fact(legacy_profile, monkeypatch, duplicate):
    home, _workspace, _db, config, session, mid = legacy_profile
    config['audit']['mode'] = 'apply'
    addition = {'action': 'add', 'content': 'duplicate symptom' if duplicate else '2026-11-04: user approved a new project',
                'reason': 'An explicit confirmed decision', 'origin': 'said', 'evidence_ids': [mid]}
    install_model(monkeypatch, mid, extra=addition)
    result = audit_session(home, config, session, reviewer=inline)
    assert result['status'] == 'complete'
    receipt = AuditStore(home).report(result['run_id'])['receipt']
    if duplicate:
        assert receipt['operations'][1]['status'] == 'already_current'
        assert len(state(home)['facts']) == 2
    else:
        assert receipt['operations'][1]['status'] == 'applied'
        assert receipt['writes'][1]['fact_id'] == 111
        assert len(state(home)['facts']) == 3


def test_current_fact_reread_prevents_overwriting_an_operator_change(legacy_profile, monkeypatch):
    home, _workspace, _db, config, session, mid = legacy_profile
    config['audit']['mode'] = 'apply'
    install_model(monkeypatch, mid)
    def operator_edit(request, *_):
        result = inline(request)
        data = state(home)
        data['facts'][0]['content'] = 'A newer operator decision'
        (home / 'facts.json').write_text(json.dumps(data), encoding='utf-8')
        return result
    result = audit_session(home, config, session, reviewer=operator_edit)
    assert result['status'] == 'failed'
    assert state(home)['facts'][0]['content'] == 'A newer operator decision'
    assert state(home)['revision'] == 1
    receipt = AuditStore(home).report(result['run_id'])['receipt']
    assert receipt['writes'] == []
    assert receipt['operations'][0]['status'] == 'deferred'
