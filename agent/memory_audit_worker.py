"""Disposable, tool-free audit reviewer process (no durable writes)."""
from dataclasses import asdict
import json
import logging
from pathlib import Path

from agent.memory_audit_backend import audit_provider, provider_snapshot
from agent.memory_audit_config import AuditSettings
from agent.memory_audit_context import evidence_fingerprints, workspace_snapshot
from agent.memory_audit_review import review
from agent.memory_audit_window import AuditWindow


def execute(request: dict, progress=None) -> dict:
    config, home = request['config'], Path(request['home'])
    settings = AuditSettings.from_config(config)
    workspace = settings.workdir(config)
    window = AuditWindow(**request['window'])
    context = workspace_snapshot(home, workspace, settings, window)
    query = '\n'.join(str(row.get('content') or '') for row in window.rows if row['role'] == 'user')[-8000:]
    with audit_provider(config, home, request['session'], workspace) as provider:
        snapshot = provider_snapshot(provider, window.session_id, query)
        if settings.mode == 'apply' and not snapshot.get('atomic_apply'):
            raise RuntimeError('Provider does not support atomic audit batches; use audit.mode: dry_run')
    model_config = config.get('model')
    runtime = ({**model_config, 'model': model_config.get('default', '')} if isinstance(model_config, dict)
               else {'model': model_config or ''})
    # The auxiliary router resolves credentials through the owning profile's secret scope.
    plan, accounting = review(window, context, snapshot, settings, runtime, progress)
    return {'plan': plan, 'accounting': accounting, 'revision': snapshot.get('revision'),
            'atomic_apply': snapshot.get('atomic_apply', False),
            'fingerprints': evidence_fingerprints(context), 'window': asdict(window),
            'coverage_gaps': accounting.get('coverage_gaps', [])}


def main(input_path: str, output_path: str):
    request = json.loads(Path(input_path).read_text(encoding="utf-8-sig"))
    from agent.memory_audit_backend import audit_scope
    usage_path = Path(output_path + '.usage')
    def progress(value):
        from utils import atomic_write_text
        atomic_write_text(usage_path, json.dumps(value))
    with audit_scope(Path(request['home'])):
        try:
            result = execute(request, progress)
        except Exception as exc:
            logging.getLogger(__name__).exception("Knowledge audit failed")
            result = {'error': f'{type(exc).__name__}: {exc}'}
            if usage_path.exists():
                result['accounting'] = json.loads(usage_path.read_text(encoding='utf-8-sig'))
                result['usage_unknown'] = result['accounting'].get('usage_unknown', False)
        Path(output_path).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
