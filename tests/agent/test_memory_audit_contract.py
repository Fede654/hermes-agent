"""Knowledge-layer safety and backward-compatible provider audit capabilities."""
import json

import pytest

from agent.memory_audit_backend import provider_snapshot, atomic_capable
from agent.memory_audit_config import AuditSettings
from agent.memory_audit_context import workspace_snapshot
from agent.memory_audit_review import validate_plan
from agent.memory_audit_window import AuditWindow


def test_legacy_fact_store_is_read_only_and_cannot_apply():
    class Legacy:
        def get_tool_schemas(self): return [{'name': 'fact_store'}]
        def handle_tool_call(self, name, args):
            assert name == 'fact_store'
            assert args['action'] in ('list', 'search')
            return json.dumps({'facts': [{'fact_id': 109, 'content': 'old decision'}]})
        def audit_snapshot(self, **kwargs): raise NotImplementedError
    provider = Legacy()
    snapshot = provider_snapshot(provider, 'topic', 'project')
    assert snapshot['facts'] == [{'fact_id': 109, 'content': 'old decision'}]
    assert snapshot['complete'] is False
    assert not atomic_capable(provider)


@pytest.mark.parametrize('operation', [
    {'action': 'delete_file', 'path': 'skills/important/SKILL.md'},
    {'action': 'add', 'content': 'duplicated decision', 'reason': 'uncertain', 'origin': 'inferred', 'evidence_ids': [1]},
    {'action': 'change', 'fact_id': 109, 'content': 'new', 'reason': 'x', 'origin': 'said', 'evidence_ids': [999], 'expected_before': 'old'},
])
def test_invalid_or_unverified_model_operations_are_rejected(operation):
    window = AuditWindow('topic', 0, 1, ({'id': 1},), 0)
    with pytest.raises(ValueError):
        validate_plan(json.dumps({'findings': [], 'summary': '', 'operations': [operation]}),
                      window, {'facts': [{'fact_id': 109, 'content': 'old'}], 'complete': False}, 8)


def test_context_exposes_layers_source_contradictions_and_skill_size(tmp_path):
    home, workspace = tmp_path / 'home', tmp_path / 'workspace'
    home.mkdir(); workspace.mkdir()
    settings = AuditSettings()
    for name in settings.doctrine_files:
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Three-layer doctrine', encoding='utf-8')
    (workspace / 'wiki').mkdir()
    (workspace / 'biblioteca').mkdir()
    (workspace / 'wiki/index.md').write_text('[Teresa](../biblioteca/teresa.md) [missing](missing.md)', encoding='utf-8')
    (workspace / 'biblioteca/teresa.md').write_text('Teresa was born in 1954.', encoding='utf-8')
    skill = home / 'skills/keruza-guion'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('Teresa was born in 1944.\n' + 'canon\n' * 18000, encoding='utf-8')
    window = AuditWindow('topic', 0, 1, ({'id': 1, 'role': 'user', 'content': 'keruza Teresa'},), 0)
    result = workspace_snapshot(home, workspace, settings, window)
    assert result['skills'][0]['bytes'] > 100000
    assert '1944' in result['skills'][0]['text']
    assert any('1954' in source['text'] for source in result['source_files'])
    assert any(pointer.get('exists') is False for pointer in result['source_pointers'])


@pytest.mark.platforms('posix')
def test_symlink_outside_workspace_never_becomes_source_evidence(tmp_path):
    from agent.memory_audit_context import read_evidence
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    outside = tmp_path / 'private.txt'
    outside.write_text('private', encoding='utf-8')
    (workspace / 'link.md').symlink_to(outside)
    with pytest.raises(ValueError, match='escapes'):
        read_evidence(workspace, 'link.md')


def test_idle_audit_replaces_legacy_background_review(monkeypatch):
    from agent.background_review import load_background_review_settings
    monkeypatch.setattr('hermes_cli.config.load_config_readonly', lambda: {
        'audit': {'enabled': True}, 'auxiliary': {'background_review': {'enabled': True}},
    })
    assert load_background_review_settings()[0] is False


def test_budget_omissions_keep_complete_memory_operations():
    from dataclasses import replace
    from agent.memory_audit_review import review_messages
    memory_text = 'A confirmed dated decision: ' + 'x' * 1000
    window = AuditWindow('topic', 0, 2, (
        {'id': 1, 'role': 'user', 'content': 'long conversation ' * 2000},
        {'id': 2, 'role': 'tool', 'tool_name': 'fact_store', 'content': memory_text},
    ), 0)
    context = {key: [] for key in ('wiki_pages', 'source_files', 'skills')}
    context.update({key: {'paths': []} for key in ('wiki', 'library', 'skill_inventory')})
    context.update({key: {} for key in ('skill_ledger', 'journal')})
    messages = review_messages(window, context, {'facts': []}, replace(AuditSettings(), max_input_tokens=6000))
    packet = json.loads(messages[1]['content'])
    assert packet['window']['rows'][0]['budget_omitted']
    assert packet['window']['rows'][1]['content'] == memory_text
    assert packet['coverage_gaps']
    assert sum(len(message['content'].encode()) + 16 for message in messages) <= 6000


def test_correction_must_set_retrieval_terms_when_provider_exposes_keywords():
    window = AuditWindow('topic', 0, 1, ({'id': 1},), 0)
    snapshot = {'facts': [{'fact_id': 109, 'content': 'old'}], 'complete': False, 'write_fields': ['keywords']}
    op = {'action': 'change', 'fact_id': 109, 'expected_before': 'old', 'content': 'new',
          'reason': 'Confirmed decision', 'origin': 'said', 'evidence_ids': [1]}
    plan = {'findings': [], 'summary': 'One correction', 'operations': [op]}
    with pytest.raises(ValueError, match='retrieval keywords'):
        validate_plan(json.dumps(plan), window, snapshot, 8)
    op['keywords'] = ['project', 'decision']
    assert validate_plan(json.dumps(plan), window, snapshot, 8)['operations'][0]['keywords'] == op['keywords']
