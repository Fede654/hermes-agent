"""Provider-neutral read snapshots and optional transactional correction batches."""
from contextlib import contextmanager
import json

from agent.memory_provider import MemoryProvider, is_core_memory_provider


def atomic_capable(provider) -> bool:
    if provider is None:
        return False
    return all(callable(getattr(provider, method, None)) and getattr(type(provider), method, None) is not getattr(MemoryProvider, method)
               for method in ('audit_snapshot', 'apply_audit_batch', 'audit_batch_status'))


@contextmanager
def audit_provider(config: dict, home, session: dict, workspace):
    name = (config.get('memory') or {}).get('provider')
    if is_core_memory_provider(name):
        yield None
        return
    from plugins.memory import load_memory_provider
    provider = load_memory_provider(name, register_skills=False)
    if provider is None or not provider.is_available():
        raise RuntimeError(f'Configured memory provider {name!r} is unavailable')
    try:
        provider.initialize(
            session['id'], hermes_home=str(home), agent_context='subagent',
            platform=session['source'], agent_workspace=str(workspace),
            **{key: session.get(key) or '' for key in ('user_id', 'chat_id', 'thread_id')},
        )
        yield provider
    finally:
        provider.shutdown()


def provider_snapshot(provider, session_id: str, query: str) -> dict:
    if provider is None:
        return {'facts': [], 'revision': '', 'complete': False, 'provider': 'builtin', 'note': 'No external episodic fact store is configured'}
    try:
        snapshot = provider.audit_snapshot(session_id=session_id, query=query)
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('facts'), list):
            raise ValueError('Provider audit_snapshot must return an object with a facts list')
        return {**snapshot, 'atomic_apply': snapshot.get('atomic_apply') is True and atomic_capable(provider)}
    except NotImplementedError:
        pass
    names = {schema.get('name', schema.get('function', {}).get('name')) for schema in provider.get_tool_schemas()}
    if 'fact_store' not in names:
        raise RuntimeError('Provider needs audit_snapshot or a read-only fact_store tool for auditing')
    reads, facts = [], {}
    # Do not infer absence from these bounded reads: the model must report coverage limits.
    for args in ({'action': 'list', 'limit': 1000}, {'action': 'search', 'query': query[:2000], 'limit': 100}):
        value = json.loads(provider.handle_tool_call('fact_store', args))
        if isinstance(value, dict) and (value.get('error') or value.get('success') is False):
            raise RuntimeError(f'Provider audit read failed: {value}')
        reads.append({'request': args, 'coverage': 'bounded; provider may clamp the requested limit'})
        entries = value.get('facts', value.get('results', [])) if isinstance(value, dict) else value
        for fact in entries if isinstance(entries, list) else []:
            if isinstance(fact, dict) and 'fact_id' in fact:
                facts[fact['fact_id']] = fact
    return {'reads': reads, 'facts': list(facts.values()), 'revision': '', 'complete': False,
            'atomic_apply': False, 'note': 'Bounded fact_store reads; absence is not proof a fact is missing.'}


@contextmanager
def audit_scope(home):
    """Bind all library callers, plugin discovery and credentials to the owning profile."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import (build_profile_secret_scope, set_secret_scope, reset_secret_scope,
                                    set_multiplex_context, reset_multiplex_context)
    home_token = set_hermes_home_override(str(home))
    multiplex_token = set_multiplex_context(True)
    scope_token = None
    try:
        scope_token = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
        yield
    finally:
        if scope_token is not None:
            reset_secret_scope(scope_token)
        reset_multiplex_context(multiplex_token)
        reset_hermes_home_override(home_token)
