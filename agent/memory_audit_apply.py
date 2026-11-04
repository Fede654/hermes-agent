"""Best-effort, individually journalled fact corrections for ordinary providers.

Inactivity reduces contention, but checks are NOT a compare-and-swap transaction.
An arrival or process crash can leave earlier writes committed. Never replay an
ambiguous in-flight write: report it and let a new audit inspect current state.
"""
from __future__ import annotations

import json
import logging

from agent.memory_audit_backend import audit_provider

KNOWN_LIMIT = ('Individual fact writes are not an atomic batch. Activity/source checks have a race before each '
               'write; interruption can leave partial corrections, and a crash after a provider commit can leave '
               'its outcome unknown. No rollback or exactly-once guarantee is claimed.')


class ProviderRefused(ValueError):
    """An explicit provider error response; no successful write was reported."""


def fact_call(provider, args: dict) -> dict:
    value = json.loads(provider.handle_tool_call('fact_store', args))
    if not isinstance(value, dict):
        raise ValueError('fact_store must return a JSON object')
    if value.get('error') or value.get('success') is False:
        raise ProviderRefused(f'fact_store refused {args["action"]}: {value}')
    return value


def current_fact(provider, fact_id: int) -> dict | None:
    history = fact_call(provider, {'action': 'history', 'fact_id': fact_id})
    versions = [item for item in history.get('versions', []) if item.get('status') == 'current']
    if len(versions) > 1:
        raise ValueError('Provider returned several current versions of a fact')
    return versions[0] if versions else None


def _add_preflight(provider, operation: dict, snapshot: dict) -> dict:
    result = fact_call(provider, {'action': 'search', 'query': operation['content'], 'limit': 100})
    facts = result.get('facts', result.get('results'))
    if not isinstance(facts, list):
        raise ValueError('Provider returned no recognized search result list; cannot check duplicates')
    folded = ' '.join(operation['content'].casefold().split())
    for fact in facts:
        if ' '.join(str(fact.get('content', '')).casefold().split()) == folded:
            return {'status': 'already_current', 'before': fact['content'], 'fact_id': fact['fact_id']}
    known = {fact['fact_id'] for fact in snapshot.get('facts', [])}
    if any(fact.get('fact_id') not in known for fact in facts):
        raise ValueError('Targeted add search found unreviewed candidates; defer to a fresh audit')
    return {'status': 'ready', 'before': None, 'fact_id': None}


def preflight(provider, operation: dict, snapshot: dict) -> dict:
    """Reread the target; the provider may still change after this check (known limit)."""
    if operation['action'] == 'add':
        return _add_preflight(provider, operation, snapshot)
    fact = current_fact(provider, operation['fact_id'])
    before = fact.get('content') if fact else None
    if before == operation['content'].strip():
        state = 'already_current'
    elif before != operation['expected_before']:
        raise ValueError('Fact changed or ended after review; deferred for a fresh audit')
    else:
        state = 'ready'
    return {'status': state, 'before': before, 'fact_id': operation['fact_id']}


def _write_one(provider, operation: dict, record: dict, persist) -> bool:
    record['status'] = 'in_flight'
    persist()
    try:
        args = {key: operation[key] for key in ('action', 'content', 'origin')}
        if 'keywords' in operation:
            args['keywords'] = operation['keywords']
        if operation['action'] == 'change':
            args['fact_id'] = operation['fact_id']
        result = fact_call(provider, args)
        record['result'] = result
        if result.get('stored') is False:
            record.update(status='deferred', error='Provider did not store the proposed fact')
            return False
        fact_id = result.get('fact_id')
        if type(fact_id) is not int or result.get('status') not in ('changed', 'exists', 'added'):
            record.update(status='unknown', error='Provider returned no recognized write receipt')
            return False
        record['fact_id'] = fact_id
        record['after'] = operation['content'].strip()
        record['status'] = 'applied'
        return True
    except ProviderRefused as exc:
        record.update(status='unknown', error=f'Provider reported an error; commit outcome not guaranteed: {exc}')
        return False
    except Exception as exc:
        logging.getLogger(__name__).exception('Individual audit write outcome is unknown')
        record.update(status='unknown', error=f'{type(exc).__name__}: {exc}')
        return False
    finally:
        persist()


def receipt_status(receipt: dict, fallback: str) -> str:
    records = receipt['operations']
    if all(item['status'] in ('applied', 'already_current') for item in records):
        return 'complete'
    if any(item['status'] in ('applied', 'unknown', 'in_flight') for item in records):
        return 'partial'
    return fallback


def apply_individual(home, workspace, config, session, settings, store, run_id, window, report, admit) -> dict:
    receipt = {'batch_id': run_id, 'strategy': 'individual', 'atomic': False,
               'known_limit': KNOWN_LIMIT, 'writes': [], 'operations': [
                   {'index': i, 'operation': operation, 'status': 'planned'}
                   for i, operation in enumerate(report['plan']['operations'])]}
    report.update(apply_strategy='individual', receipt=receipt)

    def persist():
        receipt['writes'] = [dict(
            {key: item.get(key) for key in ('fact_id', 'before', 'after')},
            target_fact_id=item['operation'].get('fact_id'),
        ) for item in receipt['operations'] if item['status'] == 'applied']
        store.save(run_id, session['id'], window.through_id, settings.mode, 'applying', report)

    persist()
    fallback = 'failed'
    with audit_provider(config, home, session, workspace, writable=True) as provider:
        for record in receipt['operations']:
            if not admit():
                record.update(status='deferred', error='User activity or changed evidence stopped remaining writes')
                fallback = 'cancelled'
                break
            try:
                record.update(preflight(provider, record['operation'], report['snapshot']))
            except (ValueError, TypeError, KeyError) as exc:
                record.update(status='deferred', error=str(exc))
                break
            if record['status'] == 'already_current':
                persist()
                continue
            if not admit():
                record.update(status='deferred', error='Activity changed between preflight and write')
                fallback = 'cancelled'
                break
            if not _write_one(provider, record['operation'], record, persist):
                break
    for record in receipt['operations']:
        if record['status'] == 'planned':
            record.update(status='deferred', error='Not attempted after an earlier operation stopped')
    receipt['audit_status'] = receipt_status(receipt, fallback)
    persist()
    return receipt


def recover_individual(report: dict) -> str:
    """Recover reporting only; never issue another memory mutation from an old plan."""
    receipt = report['receipt']
    for item in receipt['operations']:
        if item['status'] == 'in_flight':
            item.update(status='unknown', error='Process stopped around provider call; inspect current fact/history')
        elif item['status'] in ('planned', 'ready'):
            item.update(status='deferred', error='Interrupted audit; requires a fresh review')
    status = receipt_status(receipt, 'cancelled')
    receipt['audit_status'] = status
    return status
